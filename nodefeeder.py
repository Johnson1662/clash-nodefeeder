#!/usr/bin/env python3
"""nodefeeder -- keep a spare, live-tested node list fed into the Clash you already run.

Why this exists, and how it differs from the many "free node subscription" repos:

  * It never starts a second proxy core for your traffic. It reuses the Clash you
    already have, rewrites one provider file, then asks Clash to reload that file.
  * It never breaks your network. Source outages, an empty pool, every node dead --
    none of them can wipe the list. The Clash-side template decides what happens
    when the last node dies (see examples/clash-router.yaml: fall back to DIRECT).
  * It hands you a handful of nodes that were really probed through a real HTTP
    request, not ten thousand you have to test yourself.

Three stages, each cheap enough to repeat every few minutes:

  fetch   merge many public node sources into one de-duplicated pool     (pool.txt)
  filter  keep the ones whose TCP port actually answers                  (alive.txt)
  pick    probe a random sample through a throwaway core, keep the       (list.txt)
          winners plus whatever in the previous list is still healthy

Deliberate invariants -- do not "optimise" these away:

  1. Threshold-guarded writes. Below `min_merged` / `min_tcp_alive` / zero winners
     the stage keeps the previous file. A flaky upstream must never empty the pool.
  2. The previous list is probed first and kept while it answers ("only grows").
  3. The throwaway core is killed and its directory removed on every exit path.
  4. Our own HTTP traffic bypasses the proxy we feed (otherwise we would loop
     through the very nodes we are testing).
  5. Alignment by file order, never by node name: names collide (the core appends
     -01) and vmess names live inside base64.

Requires CPython 3.11+ and nothing else: standard library only.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import http.client
import http.server
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

VERSION = "0.2.0"
USER_AGENT = f"nodefeeder/{VERSION} (+https://github.com/Johnson1662/clash-nodefeeder)"

# Public node sources. Every one of these is a third party that can rename a repo,
# move a file, or vanish without notice -- exactly what happened on 2026-09-30 to
# four of the twelve entries this list started with. `nodefeeder doctor` tells you
# which ones are still answering, so a dead entry never goes unnoticed for long.
DEFAULT_SOURCES = [
    "Pawdroid/Free-servers/main/sub",
    "ermaozi/get_subscribe/main/subscribe/v2ray.txt",
    "ts-sf/fly/main/v2",
    "ripaojiedian/freenode/main/sub",
    "mfuu/FreeProxies/main/sub",
    "ALIILAPRO/v2rayNG-Config/main/server.txt",
    "Epodonios/v2ray-configs/main/All_Configs_Sub.txt",
    "Leon406/SubCrawler/main/sub/subs.txt",
    "mahdibland/V2RayAggregator/master/sub/sub_merge_base64.txt",
    "peasoft/NoMoreWalls/master/list.txt",
    "free-nodes/v2rayfree/main/sub",
]

# Mirrors that turn a raw.githubusercontent.com path into something reachable where
# GitHub itself is blocked. ${path} is replaced by the source path above.
MIRRORS = [
    "https://ghproxy.net/https://raw.githubusercontent.com/${path}",
    "https://gh-proxy.com/https://raw.githubusercontent.com/${path}",
    "https://raw.githubusercontent.com/${path}",
]

SCHEMES = ("vmess://", "vless://", "trojan://", "ss://", "hysteria2://", "hy2://", "tuic://")

DEFAULTS = {
    "sources": DEFAULT_SOURCES,
    "mirrors": MIRRORS,
    "state_dir": "~/.local/state/nodefeeder",
    "schedule": {"interval": 600, "refetch_every": 3},
    "limits": {
        # Below these counts a stage keeps its previous output instead of writing.
        "min_merged": 200,
        "min_tcp_alive": 50,
        # How many candidates to probe per round, and how many winners to keep.
        "sample": 800,
        "keep": 8,
    },
    "probe": {
        # A URL that answers 204 without a body: minimal traffic through the node.
        "url": "http://www.gstatic.com/generate_204",
        "timeout_ms": 4000,
        "concurrency": 24,
        "tcp_timeout": 3,
        "tcp_concurrency": 600,
        "fetch_timeout": 40,
    },
    "core": {
        # Empty = auto-detect (PATH, then Clash Verge's bundled core).
        "binary": "",
        "log_level": "silent",
    },
    "clash": {
        # Empty = auto-detect the Clash Verge profile directory.
        "data_dir": "",
        # File Clash Verge reads as a proxy-provider, and the provider name to reload.
        "list_file": "nodefeeder-nodes.txt",
        "provider": "nodefeeder",
        # auto | unix | http | none
        "control": "auto",
        "address": "",
        "secret": "",
        # Optional: a local proxy port to sanity-check in `doctor`.
        "verify_port": 0,
        "verify_url": "https://ipinfo.io/ip",
    },
    "serve": {
        # Off by default. Turn it on to let other devices (a phone, another laptop)
        # subscribe to the same list over HTTP instead of copying files around.
        "enable": False,
        # Addresses to bind. Keep 127.0.0.1 for a tunnel; add the machine's private
        # or tailnet address for phones. Avoid 0.0.0.0 on a shared network: anyone
        # on it could then fetch your list.
        "bind": ["127.0.0.1"],
        "port": 7691,
        # Optional random path segment: every request must start with /<token>/.
        "token": "",
        # What clients should use to reach the server (baked into the profile we
        # generate). e.g. http://100.90.54.43:7691 or https://nodes.example.com
        "public_url": "",
    },
}

VERGE_DIR_CANDIDATES = [
    "~/.local/share/io.github.clash-verge-rev.clash-verge-rev",           # Linux (deb/rpm)
    "~/.var/app/io.github.clash-verge-rev.clash-verge-rev/config",        # Linux (flatpak)
    "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev",  # macOS
]

CORE_CANDIDATES = ["mihomo", "verge-mihomo", "clash-meta", "clash", "mihomo-alpha"]


# --------------------------------------------------------------------------- util


def expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path or ""))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except FileNotFoundError:
        return []


def write_lines(path: str, lines: list[str], min_keep: int) -> bool:
    """Write only when the result is trustworthy. Returns True if the file changed."""
    if len(lines) < min_keep:
        log(f"keep previous {os.path.basename(path)}: only {len(lines)} lines (< {min_keep})")
        return False
    if lines == read_lines(path):
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return True


def http_get(url: str, timeout: int = 40) -> bytes:
    """GET without any proxy: we must not send our own traffic through the proxy we feed."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def b64decode_loose(text: str) -> str:
    """Decode base64, tolerating missing padding and the URL-safe alphabet."""
    raw = text.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")


def dedupe_key(uri: str) -> str:
    """Identity of a node: everything before the '#' label (labels are arbitrary)."""
    return uri.split("#", 1)[0]


def split_host_port(netloc: str) -> tuple[str | None, int]:
    """'host:port' | 'host' | '[v6]:port' -> (host, port). Port 0 means "not specified"."""
    netloc = netloc.rstrip("/")
    if netloc.startswith("["):                      # [2001:db8::1]:443
        host, _, rest = netloc[1:].partition("]")
        port = rest.lstrip(":")
    else:
        host, sep, port = netloc.rpartition(":")
        if not sep:                                 # no colon: rpartition put it all in `port`
            return netloc or None, 0
    return (host or None), int(port) if port.isdigit() else 0


def parse_endpoint(uri: str) -> tuple[str | None, int]:
    """Pull (host, port) out of a share link. Any protocol we cannot read -> (None, 0)."""
    try:
        if uri.startswith("vmess://"):
            data = json.loads(b64decode_loose(uri[8:]))
            return data.get("add") or None, int(data.get("port") or 0)
        if uri.startswith("ss://"):
            body = uri[5:].split("#", 1)[0].split("?", 1)[0]
            netloc = body.split("@", 1)[1] if "@" in body else b64decode_loose(body).split("@", 1)[1]
            return split_host_port(netloc)
        parsed = urllib.parse.urlparse(uri.split("#", 1)[0])
        return parsed.hostname, int(parsed.port or 0)
    except Exception:
        return None, 0


# --------------------------------------------------------------------------- config


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_config_path() -> str:
    return expand("~/.config/nodefeeder/config.json")


def load_config(path: str, need: bool = True) -> dict:
    cfg = deep_merge(DEFAULTS, {})
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg = deep_merge(DEFAULTS, json.load(fh))
    elif need:
        raise SystemExit(f"no config at {path} -- run `nodefeeder init` first")
    return cfg


def state_path(cfg: dict, name: str) -> str:
    return os.path.join(expand(cfg["state_dir"]), name)


def save_status(cfg: dict, patch: dict) -> None:
    path = state_path(cfg, "status.json")
    try:
        with open(path, encoding="utf-8") as fh:
            status = json.load(fh)
    except Exception:
        status = {}
    status.update(patch)
    status["ts"] = int(time.time())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------- stage: fetch


def source_urls(cfg: dict, source: str) -> list[str]:
    return [m.replace("${path}", source) for m in cfg["mirrors"]]


def fetch_source(cfg: dict, source: str) -> tuple[str, int, str]:
    errors = []
    for url in source_urls(cfg, source):
        try:
            text = http_get(url, timeout=int(cfg["probe"]["fetch_timeout"])).decode("utf-8", "ignore")
        except Exception as exc:                       # try the next mirror
            errors.append(f"{urllib.parse.urlparse(url).netloc}: {type(exc).__name__}")
            continue
        if "://" not in text[:2000]:                  # base64-wrapped subscription
            try:
                text = b64decode_loose(text)
            except Exception:
                pass
        found = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(SCHEMES)]
        return source, len(found), "\n".join(found)
    return source, 0, "unreachable (" + "; ".join(errors[:2]) + ")"


def cmd_fetch(cfg: dict) -> int:
    sources = cfg["sources"]
    log(f"fetch: {len(sources)} sources")
    merged: dict[str, str] = {}
    reachable = 0
    with cf.ThreadPoolExecutor(min(12, len(sources) or 1)) as pool:
        for source, count, payload in pool.map(lambda s: fetch_source(cfg, s), sources):
            marker = "" if count else "  <-- no nodes"
            log(f"  {source:60} {count:>6}{marker}")
            if count:
                reachable += 1
            for line in payload.splitlines():
                if line.startswith(SCHEMES):
                    merged.setdefault(dedupe_key(line), line)
    out = sorted(merged.values(), key=lambda u: (u.split("://", 1)[0], dedupe_key(u)))
    log(f"fetch: {reachable}/{len(sources)} sources answered, {len(out)} unique nodes")
    changed = write_lines(state_path(cfg, "pool.txt"), out, int(cfg["limits"]["min_merged"]))
    save_status(cfg, {"pool": len(out), "sources_ok": reachable, "fetch_changed": changed})
    return 0 if out else 1


# ---------------------------------------------------------------------- stage: filter


def tcp_answers(item: tuple[str, str | None, int], timeout: float) -> str | None:
    uri, host, port = item
    if not host or not port:
        return None
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return uri
    except Exception:
        return None
    finally:
        sock.close()


def cmd_filter(cfg: dict) -> int:
    pool = read_lines(state_path(cfg, "pool.txt"))
    log(f"filter: {len(pool)} candidates")
    if not pool:
        log("filter: empty pool -- keeping previous alive list")
        return 1
    items = [(uri,) + parse_endpoint(uri) for uri in pool]
    timeout = float(cfg["probe"]["tcp_timeout"])
    alive: list[str] = []
    with cf.ThreadPoolExecutor(int(cfg["probe"]["tcp_concurrency"])) as pool_exec:
        for result in pool_exec.map(lambda it: tcp_answers(it, timeout), items):
            if result:
                alive.append(result)
    log(f"filter: {len(alive)}/{len(pool)} ports answered TCP")
    write_lines(state_path(cfg, "alive.txt"), alive, int(cfg["limits"]["min_tcp_alive"]))
    save_status(cfg, {"alive": len(alive)})
    return 0 if alive else 1


# ------------------------------------------------------------------------ stage: pick


def choose_sample(alive: list[str], previous: list[str], size: int, rng: random.Random) -> list[str]:
    """Previous list first (so it is re-probed and kept), then random fresh candidates."""
    alive_by_key: dict[str, str] = {}
    for uri in alive:
        alive_by_key.setdefault(dedupe_key(uri), uri)
    sample: list[str] = []
    seen: set[str] = set()
    for uri in previous:                      # only entries that are also in alive can be probed
        key = dedupe_key(uri)
        if key in alive_by_key and key not in seen:
            seen.add(key)
            sample.append(alive_by_key[key])
        if len(sample) >= size:
            break
    if len(sample) < size:
        rest = [u for k, u in alive_by_key.items() if k not in seen]
        sample += rng.sample(rest, min(size - len(sample), len(rest)))
    return sample


def detect_core(cfg: dict) -> str | None:
    explicit = expand(cfg["core"]["binary"])
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    for name in CORE_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in ("/usr/bin/verge-mihomo", "/usr/local/bin/verge-mihomo"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def probe_through_core(cfg: dict, binary: str, candidates: list[str]) -> list[tuple[int, str]]:
    """Start a throwaway core, ask it to health-check every candidate, return (delay, uri)."""
    probe = cfg["probe"]
    root = tempfile.mkdtemp(prefix="nodefeeder-")
    try:
        node_dir = os.path.join(root, "core")
        os.makedirs(node_dir)
        provider = os.path.join(node_dir, "candidates.txt")   # must live under -d
        with open(provider, "w", encoding="utf-8") as fh:
            fh.write("\n".join(candidates) + "\n")
        control = free_port()
        config = os.path.join(node_dir, "core.yaml")
        with open(config, "w", encoding="utf-8") as fh:
            fh.write(
                f"mixed-port: {free_port()}\n"
                f"external-controller: 127.0.0.1:{control}\n"
                f"log-level: {cfg['core']['log_level']}\n"
                "mode: rule\n"
                f"proxy-providers:\n  t:\n    type: file\n    path: {provider}\n"
                "proxy-groups:\n  - name: G\n    type: select\n    use: [t]\n"
                "rules:\n  - MATCH,G\n"
            )
        core = subprocess.Popen(
            [binary, "-d", node_dir, "-f", config],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            names: list[str] = []
            for _ in range(40):
                time.sleep(1)
                try:
                    status, body = api_call(
                        "GET", "/providers/proxies/t", f"127.0.0.1:{control}", timeout=5
                    )
                except Exception:
                    continue
                if status == 200:
                    names = [p["name"] for p in json.loads(body).get("proxies") or []]
                    if names:
                        break
            if not names:
                log("pick: throwaway core never returned its candidate list")
                return []

            query = urllib.parse.quote(probe["url"], safe="")

            def check(name: str) -> int:
                path = (
                    f"/providers/proxies/t/{urllib.parse.quote(name, safe='')}"
                    f"/healthcheck?timeout={int(probe['timeout_ms'])}&url={query}"
                )
                try:
                    status, body = api_call(
                        "GET", path, f"127.0.0.1:{control}", timeout=float(probe["timeout_ms"]) / 1000 + 8
                    )
                    if status == 200:
                        return int(json.loads(body).get("delay") or 0)
                except Exception:
                    pass
                return 0

            with cf.ThreadPoolExecutor(int(probe["concurrency"])) as pool:
                delays = list(pool.map(check, names))
            # Align by file order: the core keeps provider order even for duplicate names
            # and vmess names that we cannot see from the share link.
            return sorted(
                (delay, uri)
                for delay, uri in ((delays[i], candidates[i]) for i in range(min(len(names), len(candidates))))
                if delay
            )
        finally:
            core.terminate()
            try:
                core.wait(timeout=10)
            except Exception:
                core.kill()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def cmd_pick(cfg: dict) -> int:
    alive = read_lines(state_path(cfg, "alive.txt"))
    previous = read_lines(state_path(cfg, "list.txt"))
    if not alive:
        log("pick: no alive candidates -- keeping previous list")
        return 1
    sample = choose_sample(alive, previous, int(cfg["limits"]["sample"]), random.Random())
    binary = detect_core(cfg)
    if not binary:
        log("pick: no mihomo/clash core found -- set core.binary in the config")
        return 1
    log(f"pick: probing {len(sample)} candidates through {binary}")
    winners = probe_through_core(cfg, binary, sample)
    keep = int(cfg["limits"]["keep"])
    kept = [uri for _, uri in winners[:keep]]
    log(f"pick: {len(winners)} candidates answered, keeping {len(kept)}")
    if not kept:
        log("pick: nothing answered -- keeping previous list")
        save_status(cfg, {"probed": len(sample), "winners": 0, "reload": "skipped"})
        return 1
    write_lines(state_path(cfg, "list.txt"), kept, 1)
    write_subscription(cfg, kept)
    result = publish_to_clash(cfg, kept)
    save_status(cfg, {"probed": len(sample), "winners": len(winners), "list": len(kept), "reload": result})
    return 0


# --------------------------------------------------------------- Clash integration


def detect_verge_dir(cfg: dict) -> str | None:
    explicit = expand(cfg["clash"]["data_dir"])
    if explicit:
        return explicit if os.path.isdir(explicit) else None
    for candidate in VERGE_DIR_CANDIDATES:
        if os.path.isdir(expand(candidate)):
            return expand(candidate)
    return None


def control_target(cfg: dict) -> tuple[str, str] | tuple[None, str]:
    """Return ('unix', socket_path) | ('http', host:port) | (None, reason)."""
    mode = cfg["clash"]["control"]
    if mode == "none":
        return None, "disabled in config"
    if mode == "unix" or mode == "auto":
        verge = detect_verge_dir(cfg)
        if verge:
            sock = os.path.join(verge, "verge-mihomo.sock")
            if os.path.exists(sock):
                return "unix", sock
    if mode == "http" or mode == "auto":
        if cfg["clash"]["address"]:
            return "http", cfg["clash"]["address"]
        verge = detect_verge_dir(cfg)
        runtime = os.path.join(verge, "config.yaml") if verge else ""
        if runtime and os.path.exists(runtime):
            for line in read_lines(runtime):
                if line.startswith("external-controller:"):
                    return "http", line.split(":", 1)[1].strip()
    return None, "no Clash control socket or API found (set clash.control/address)"


def api_call(method: str, path: str, target: str, secret: str = "", body: bytes = b"",
             timeout: float = 20) -> tuple[int, bytes]:
    if target.startswith("/"):                                   # unix socket
        conn: http.client.HTTPConnection = UnixHTTPConnection(target, timeout)
        host_header = "localhost"
    else:
        host, _, port = target.rpartition(":")
        conn = http.client.HTTPConnection(host or "127.0.0.1", int(port or 9090), timeout=timeout)
        host_header = target
    headers = {"Host": host_header}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 20):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def publish_to_clash(cfg: dict, lines: list[str]) -> str:
    """Copy the list where Clash reads it and make Clash reload that provider."""
    verge = detect_verge_dir(cfg)
    if not verge:
        log("clash: no data dir found -- list kept in the state dir only")
        return "no-data-dir"
    target_file = os.path.join(verge, cfg["clash"]["list_file"])
    with open(target_file + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(target_file + ".tmp", target_file)
    kind, where = control_target(cfg)
    if not kind:
        log(f"clash: wrote {target_file}, but no control channel ({where})")
        return "no-control"
    path = f"/providers/proxies/{urllib.parse.quote(cfg['clash']['provider'], safe='')}"
    try:
        status, _ = api_call("PUT", path, where, cfg["clash"]["secret"])
    except Exception as exc:
        log(f"clash: reload failed ({type(exc).__name__}: {exc})")
        return "error"
    if status in (200, 204):
        log(f"clash: wrote {target_file} and Clash reloaded the provider")
        return "ok"
    log(
        f"clash: wrote {target_file}, but reload returned HTTP {status} -- "
        f"is the provider `{cfg['clash']['provider']}` in your Clash profile? "
        "run `nodefeeder profile` and import what it writes"
    )
    return f"http-{status}"


# --------------------------------------------------------------- other devices


PHONE_PROFILE = """# nodefeeder -- subscription profile for a phone or a second machine.
#
# The node list is pulled over HTTP (proxy-providers, type: http), so it refreshes
# by itself: nothing to copy around when the list changes.
proxy-providers:
  {provider}:
    type: http
    url: {nodes_url}
    path: ./{provider}-provider.yaml
    interval: {interval}
    health-check:
      enable: true
      url: http://www.gstatic.com/generate_204
      interval: 300

proxy-groups:
  - name: "auto-nodes"
    type: url-test
    use: [{provider}]
    url: http://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50

  - name: "spare-router"
    type: fallback
    proxies: ["auto-nodes", DIRECT]
    url: http://www.gstatic.com/generate_204
    interval: 300

rules:
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - GEOIP,CN,DIRECT
  - MATCH,spare-router
"""


def sub_dir(cfg: dict) -> str:
    return os.path.join(expand(cfg["state_dir"]), "sub")


def sub_base_url(cfg: dict) -> str:
    base = (cfg["serve"]["public_url"] or "").rstrip("/")
    if not base:
        host = (cfg["serve"]["bind"] or ["127.0.0.1"])[0]
        base = f"http://{host}:{cfg['serve']['port']}"
    token = cfg["serve"]["token"].strip("/")
    return f"{base}/{token}" if token else base


def render_phone_profile(cfg: dict, base_url: str) -> str:
    return PHONE_PROFILE.format(
        provider=cfg["clash"]["provider"],
        nodes_url=f"{base_url.rstrip('/')}/nodes.txt",
        interval=int(cfg["schedule"]["interval"]),
    )


def write_subscription(cfg: dict, lines: list[str]) -> None:
    """What other devices subscribe to: share links, a base64 list and a Clash profile."""
    if not lines:
        return
    directory = sub_dir(cfg)
    os.makedirs(directory, exist_ok=True)
    payload = "\n".join(lines) + "\n"
    with open(os.path.join(directory, "nodes.txt"), "w", encoding="utf-8") as fh:
        fh.write(payload)
    with open(os.path.join(directory, "sub.txt"), "w", encoding="utf-8") as fh:
        fh.write(base64.b64encode(payload.encode()).decode() + "\n")
    with open(os.path.join(directory, "profile.yaml"), "w", encoding="utf-8") as fh:
        fh.write(render_phone_profile(cfg, sub_base_url(cfg)))
    log(f"sub: {len(lines)} nodes -> {directory} (nodes.txt, sub.txt, profile.yaml)")


def make_handler(cfg: dict):
    """Read-only handler confined to the sub directory, with an optional path token."""
    directory = sub_dir(cfg)
    token = cfg["serve"]["token"].strip("/")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def _resolve(self) -> str | None:
            """Path with the token removed, or None when the request is not allowed."""
            if not token:
                return self.path
            head, _, rest = self.path.lstrip("/").partition("/")
            if head != token:
                return None
            return "/" + rest

        def _send(self, head_only: bool) -> None:
            path = self._resolve()
            if path is None:
                self.send_error(404)
                return
            if path.rstrip("/").endswith("profile.yaml"):
                # Answer with the host the client actually reached us on: a client
                # that can resolve one name but not the other must still get a
                # profile whose provider URL it can fetch.
                host_header = self.headers.get("Host") or ""
                base = f"https://{host_header}" if host_header else sub_base_url(cfg)
                if token:
                    base = f"{base}/{token}"
                body = render_phone_profile(cfg, base).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)
                return
            self.path = path
            if head_only:
                super().do_HEAD()
            else:
                super().do_GET()

        def do_GET(self):
            self._send(head_only=False)

        def do_HEAD(self):
            self._send(head_only=True)

        def list_directory(self, path):
            # Never expose what else lives in the state directory.
            self.send_error(403, "no directory listing")
            return None

        def log_message(self, fmt, *args):
            log(f"sub: {self.address_string()} {fmt % args}")

    return Handler


def build_servers(cfg: dict) -> list:
    handler = make_handler(cfg)
    servers = []
    for host in cfg["serve"]["bind"] or ["127.0.0.1"]:
        try:
            server = http.server.ThreadingHTTPServer((host, int(cfg["serve"]["port"])), handler)
        except OSError as exc:
            log(f"serve: cannot bind {host}:{cfg['serve']['port']} ({exc})")
            continue
        servers.append(server)
        log(f"serve: {sub_dir(cfg)} on http://{host}:{server.server_address[1]}/")
    return servers


def cmd_serve(cfg: dict) -> int:
    base = sub_base_url(cfg)
    print(f"clash  : {base}/profile.yaml")
    print(f"v2rayNG: {base}/sub.txt")
    servers = build_servers(cfg)
    if not servers:
        return 1
    try:
        for server in servers:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


# ------------------------------------------------------------------------- commands


def cmd_once(cfg: dict, force_fetch: bool = False, round_no: int = 1) -> int:
    if force_fetch or (round_no - 1) % int(cfg["schedule"]["refetch_every"]) == 0:
        cmd_fetch(cfg)
        cmd_filter(cfg)
    return cmd_pick(cfg)


def cmd_run(cfg: dict) -> int:
    interval = int(cfg["schedule"]["interval"])
    for server in build_servers(cfg) if cfg["serve"]["enable"] else []:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    round_no = 0
    while True:
        round_no += 1
        log(f"--- round {round_no} ---")
        try:
            cmd_once(cfg, round_no=round_no)
        except Exception as exc:                        # a bad round must not kill the loop
            log(f"round {round_no} failed: {type(exc).__name__}: {exc}")
        log(f"sleeping {interval}s")
        time.sleep(interval)


def cmd_doctor(cfg: dict) -> int:
    problems = 0
    print(f"nodefeeder {VERSION}")

    print("\nsources")
    with cf.ThreadPoolExecutor(6) as pool:
        for source, count, payload in pool.map(lambda s: fetch_source(cfg, s), cfg["sources"]):
            if count:
                print(f"  ok    {count:>6}  {source}")
            else:
                problems += 1
                print(f"  DEAD  {payload}  {source}")

    print("\nstate")
    try:
        with open(state_path(cfg, "status.json"), encoding="utf-8") as fh:
            status = json.load(fh)
    except Exception:
        status = {}
    for name in ("pool.txt", "alive.txt", "list.txt"):
        path = state_path(cfg, name)
        count = len(read_lines(path))
        age = f"{int(time.time() - os.path.getmtime(path))}s ago" if count else "-"
        print(f"  {name:10} {count:>6} lines   {age}")
        if name == "list.txt" and not count:
            problems += 1
    print(f"  status.json  {status or '(none yet)'}")

    print("\ncore")
    binary = detect_core(cfg)
    print(f"  {binary or 'NOT FOUND -- set core.binary'}")

    print("\nclash")
    verge = detect_verge_dir(cfg)
    print(f"  data dir     {verge or 'NOT FOUND -- set clash.data_dir'}")
    if not verge:
        problems += 1
    else:
        listed = os.path.join(verge, cfg["clash"]["list_file"])
        if os.path.exists(listed):
            print(f"  {cfg['clash']['list_file']:12} {os.path.getsize(listed)} bytes")
        else:
            print(f"  {cfg['clash']['list_file']:12} missing -- import examples/clash-router.yaml")
    kind, where = control_target(cfg)
    print(f"  control      {kind or 'none'} {where}")
    if kind:
        provider = urllib.parse.quote(cfg["clash"]["provider"], safe="")
        try:
            code, body = api_call("GET", f"/providers/proxies/{provider}", where, cfg["clash"]["secret"])
            if code == 200:
                nodes = json.loads(body).get("proxies") or []
                print(f"  provider     {cfg['clash']['provider']}: {len(nodes)} nodes loaded by Clash")
            else:
                problems += 1
                print(f"  provider     HTTP {code} -- Clash has no provider named "
                      f"`{cfg['clash']['provider']}` (fix: nodefeeder profile, then import it)")
        except Exception as exc:
            problems += 1
            print(f"  provider     unreachable ({type(exc).__name__})")

    port = int(cfg["clash"]["verify_port"] or 0)
    if port:
        print(f"\ntraffic through 127.0.0.1:{port}")
        for label, url in (("probe url", cfg["probe"]["url"]), ("exit ip", cfg["clash"]["verify_url"])):
            if not url:
                continue
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}",
                                                 "https": f"http://127.0.0.1:{port}"})
                )
                with opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
                                 timeout=25) as resp:
                    body = resp.read(64).decode("utf-8", "ignore").strip()
                print(f"  {label:9} HTTP {resp.status} {body[:40]}")
            except Exception as exc:
                problems += 1
                print(f"  {label:9} FAILED {type(exc).__name__}")

    print(f"\n{problems} problem(s)" if problems else "\nall good")
    return 1 if problems else 0


SYSTEMD_UNIT = """[Unit]
Description=nodefeeder -- feed a live-tested node list into Clash
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={python} {script} run --config {config}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""

# Handed to the user instead of a static example file, so the provider name and the
# absolute list path can never drift away from the config they were told to edit.
ROUTER_PROFILE = """# nodefeeder -- spare router profile for Clash Verge / mihomo
#
# Import: Clash Verge -> Profiles -> + -> Local file -> pick this file.
# Keep it off in normal times; switch to it when your paid provider dies.
#
# Design, and why it cannot lock you out of the network:
#   * two kinds of traffic only -- China direct, everything else through the router;
#   * the router picks the fastest node from the list nodefeeder keeps probing, and
#     falls back to DIRECT when every node is dead (including when the list is empty);
#   * no port and no DNS settings here: your Clash client owns those, and writing them
#     here fights with the client.
profile:
  store-selected: false

proxy-providers:
  {provider}:
    type: file
    path: {list_path}
    health-check:
      enable: true
      url: http://www.gstatic.com/generate_204
      interval: 300

proxy-groups:
  - name: "auto-nodes"
    type: url-test
    use: [{provider}]
    url: http://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50

  - name: "spare-router"
    type: fallback
    proxies: ["auto-nodes", DIRECT]
    url: http://www.gstatic.com/generate_204
    interval: 300

rules:
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - GEOIP,CN,DIRECT
  - MATCH,spare-router
"""


def cmd_profile(cfg: dict, out: str) -> int:
    verge = detect_verge_dir(cfg)
    if not verge:
        raise SystemExit("set clash.data_dir first: I need to know where Clash reads the list")
    list_path = os.path.join(verge, cfg["clash"]["list_file"])
    text = ROUTER_PROFILE.format(provider=cfg["clash"]["provider"], list_path=list_path)
    out = expand(out) if out else state_path(cfg, "spare-router.yaml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"wrote {out}\nimport it in your Clash client, then run: nodefeeder doctor")
    return 0


def cmd_init(cfg_path: str, force: bool) -> int:
    if os.path.exists(cfg_path) and not force:
        raise SystemExit(f"{cfg_path} exists (use --force to overwrite)")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    example = deep_merge(DEFAULTS, {})
    example["sources"] = DEFAULT_SOURCES
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(example, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"wrote {cfg_path}")
    return 0


def systemd_dir() -> str:
    return expand("~/.config/systemd/user")


def cmd_install(cfg_path: str) -> int:
    unit = os.path.join(systemd_dir(), "nodefeeder.service")
    os.makedirs(systemd_dir(), exist_ok=True)
    with open(unit, "w", encoding="utf-8") as fh:
        fh.write(SYSTEMD_UNIT.format(python=sys.executable, script=os.path.abspath(__file__),
                                     config=cfg_path))
    print(f"wrote {unit}")
    if subprocess.run(["systemctl", "--user", "daemon-reload"], check=False).returncode == 0:
        print("now enable it:  systemctl --user enable --now nodefeeder")
    return 0


def cmd_uninstall() -> int:
    unit = os.path.join(systemd_dir(), "nodefeeder.service")
    subprocess.run(["systemctl", "--user", "disable", "--now", "nodefeeder"], check=False)
    if os.path.exists(unit):
        os.remove(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("removed the systemd unit (config and state left in place)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nodefeeder", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"nodefeeder {VERSION}")
    # SUPPRESS on the subparser copy: otherwise the subparser's default would clobber a
    # --config given before the subcommand. This way both positions work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS, help="config file path")
    parser.add_argument("-c", "--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", parents=[common], help="write a starter config") \
        .add_argument("--force", action="store_true")
    sub.add_parser("fetch", parents=[common], help="merge the public node sources into the pool")
    sub.add_parser("filter", parents=[common], help="keep pool entries whose TCP port answers")
    sub.add_parser("pick", parents=[common], help="probe a sample and feed the winners to Clash")
    once = sub.add_parser("once", parents=[common], help="fetch + filter + pick, then exit")
    once.add_argument("--fetch", action="store_true", help="force a source refetch first")
    sub.add_parser("run", parents=[common], help="loop: `once` every schedule.interval seconds")
    sub.add_parser("doctor", parents=[common], help="check sources, state, core and Clash wiring")
    profile = sub.add_parser("profile", parents=[common],
                             help="write a ready-to-import Clash profile for the list")
    profile.add_argument("--out", default="", help="where to write it (default: the state dir)")
    sub.add_parser("serve", parents=[common],
                   help="serve the subscription files (for a phone or another machine)")
    sub.add_parser("install", parents=[common], help="write and reload a systemd --user unit")
    sub.add_parser("uninstall", parents=[common], help="remove the systemd --user unit")

    args = parser.parse_args(argv)
    cfg_path = expand(getattr(args, "config", "") or default_config_path())

    if args.command == "init":
        return cmd_init(cfg_path, args.force)
    if args.command == "uninstall":
        return cmd_uninstall()

    cfg = load_config(cfg_path)
    os.makedirs(expand(cfg["state_dir"]), exist_ok=True)

    if args.command == "install":
        return cmd_install(cfg_path)
    if args.command == "fetch":
        return cmd_fetch(cfg)
    if args.command == "filter":
        return cmd_filter(cfg)
    if args.command == "pick":
        return cmd_pick(cfg)
    if args.command == "once":
        return cmd_once(cfg, force_fetch=args.fetch)
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "doctor":
        return cmd_doctor(cfg)
    if args.command == "profile":
        return cmd_profile(cfg, args.out)
    if args.command == "serve":
        return cmd_serve(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
