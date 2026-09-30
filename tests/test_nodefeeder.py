"""Tests for the parts that fail silently: share-link parsing, guarded writes, sampling.

Everything else in nodefeeder is I/O against the network or Clash, which the
`doctor` command covers interactively. These are the pure functions where a bug
drops nodes without saying anything.

Run: python -m unittest discover -s tests -v
"""

import base64
import importlib.util
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("nodefeeder", os.path.join(ROOT, "nodefeeder.py"))
nf = importlib.util.module_from_spec(_spec)
sys.modules["nodefeeder"] = nf
_spec.loader.exec_module(nf)


def b64(text, urlsafe=False, pad=True):
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    out = encoder(text.encode()).decode()
    return out if pad else out.rstrip("=")


class ParseEndpoint(unittest.TestCase):
    def test_vmess_standard(self):
        uri = "vmess://" + b64(json.dumps({"add": "1.2.3.4", "port": "443", "ps": "jp"}))
        self.assertEqual(nf.parse_endpoint(uri), ("1.2.3.4", 443))

    def test_vmess_without_padding(self):
        # Share links in the wild routinely strip '='; b64decode refuses those.
        uri = "vmess://" + b64(json.dumps({"add": "5.6.7.8", "port": 8443}), pad=False)
        self.assertEqual(nf.parse_endpoint(uri), ("5.6.7.8", 8443))

    def test_vmess_url_safe_alphabet(self):
        uri = "vmess://" + b64(json.dumps({"add": "9.9.9.9", "port": "2053", "pbk": "????"}),
                               urlsafe=True, pad=False)
        self.assertEqual(nf.parse_endpoint(uri), ("9.9.9.9", 2053))

    def test_ss_legacy_base64(self):
        uri = "ss://" + b64("aes-256-gcm:pw@example.com:8388") + "#jp"
        self.assertEqual(nf.parse_endpoint(uri), ("example.com", 8388))

    def test_ss_plain_with_query(self):
        uri = "ss://aes-256-gcm:pw@1.2.3.4:1080?plugin=obfs-local#name"
        self.assertEqual(nf.parse_endpoint(uri), ("1.2.3.4", 1080))

    def test_url_style_schemes(self):
        cases = {
            "vless://uuid@5.6.7.8:443?encryption=none#jp": ("5.6.7.8", 443),
            "trojan://pw@example.org:8443?allowInsecure=1#x": ("example.org", 8443),
            "hysteria2://pw@9.9.9.9:8443?insecure=1#h": ("9.9.9.9", 8443),
            "hy2://pw@8.8.8.8:443#h": ("8.8.8.8", 443),
            "tuic://uuid:pw@1.1.1.1:443#t": ("1.1.1.1", 443),
        }
        for uri, want in cases.items():
            with self.subTest(uri=uri):
                self.assertEqual(nf.parse_endpoint(uri), want)

    def test_ipv6_literal(self):
        uri = "trojan://pw@[2001:db8::1]:443#v6"
        self.assertEqual(nf.parse_endpoint(uri), ("2001:db8::1", 443))

    def test_unreadable_input(self):
        # A node we cannot address must be dropped, not guessed at.
        for uri in ("", "trojan://", "vmess://@@@@", "not-a-link"):
            with self.subTest(uri=uri):
                host, port = nf.parse_endpoint(uri)
                self.assertFalse(host and port, f"{uri!r} parsed to {(host, port)}")


class HostPort(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(nf.split_host_port("example.com:443"), ("example.com", 443))
        self.assertEqual(nf.split_host_port("example.com"), ("example.com", 0))
        self.assertEqual(nf.split_host_port("[2001:db8::1]:1080"), ("2001:db8::1", 1080))
        self.assertEqual(nf.split_host_port("1.2.3.4:notaport"), ("1.2.3.4", 0))


class Dedupe(unittest.TestCase):
    def test_label_does_not_make_a_node_unique(self):
        a = "trojan://pw@1.2.3.4:443#jp-01"
        b = "trojan://pw@1.2.3.4:443#hongkong-speed"
        self.assertEqual(nf.dedupe_key(a), nf.dedupe_key(b))


class GuardedWrites(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "out.txt")

    def test_thin_result_keeps_previous(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("old1\nold2\n")
        self.assertFalse(nf.write_lines(self.path, ["one"], 3))
        self.assertEqual(nf.read_lines(self.path), ["old1", "old2"])

    def test_thin_result_creates_nothing(self):
        self.assertFalse(nf.write_lines(self.path, [], 1))
        self.assertFalse(os.path.exists(self.path))

    def test_result_at_threshold_is_written(self):
        self.assertTrue(nf.write_lines(self.path, ["a", "b"], 2))
        self.assertEqual(nf.read_lines(self.path), ["a", "b"])

    def test_unchanged_result_is_not_rewritten(self):
        nf.write_lines(self.path, ["a"], 1)
        self.assertFalse(nf.write_lines(self.path, ["a"], 1))


class Sample(unittest.TestCase):
    @staticmethod
    def uri(i):
        return f"trojan://pw@10.0.0.{i}:443#node{i}"

    def test_previous_list_is_probed_first(self):
        alive = [self.uri(i) for i in range(10)]
        previous = [alive[3], alive[1]]
        got = nf.choose_sample(alive, previous, 5, random.Random(0))
        self.assertEqual(got[:2], [alive[3], alive[1]])
        self.assertEqual(len(got), 5)

    def test_previous_list_loses_nothing_to_duplicates(self):
        alive = [self.uri(i) for i in range(10)]
        got = nf.choose_sample(alive, [alive[0]] * 4, 4, random.Random(1))
        self.assertEqual(got[0], alive[0])
        self.assertEqual(len(set(got)), len(got))

    def test_entries_missing_from_the_pool_are_dropped(self):
        alive = [self.uri(1)]
        previous = [self.uri(9) + "-dead", alive[0]]
        self.assertEqual(nf.choose_sample(alive, previous, 5, random.Random(0)), [alive[0]])

    def test_sample_cannot_exceed_the_pool(self):
        alive = [self.uri(i) for i in range(3)]
        self.assertEqual(len(nf.choose_sample(alive, [], 800, random.Random(0))), 3)


class Config(unittest.TestCase):
    def test_missing_file_yields_documented_defaults(self):
        cfg = nf.load_config(os.path.join(tempfile.mkdtemp(), "absent.json"), need=False)
        for key in ("sources", "mirrors", "schedule", "limits", "probe", "core", "clash"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["limits"]["keep"], 8)
        self.assertEqual(cfg["schedule"]["interval"], 600)

    def test_partial_override_keeps_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"limits": {"keep": 3}, "clash": {"provider": "mine"}}, fh)
            cfg = nf.load_config(path)
        self.assertEqual(cfg["limits"]["keep"], 3)
        self.assertEqual(cfg["limits"]["sample"], 800)
        self.assertEqual(cfg["clash"]["provider"], "mine")
        self.assertEqual(cfg["clash"]["list_file"], "nodefeeder-nodes.txt")


class SourceFetching(unittest.TestCase):
    def setUp(self):
        self.cfg = nf.deep_merge(nf.DEFAULTS, {})
        self._real = nf.http_get

    def tearDown(self):
        nf.http_get = self._real

    def stub(self, payload):
        nf.http_get = lambda url, timeout=40: payload

    def test_plain_payload(self):
        self.stub(b"trojan://pw@1.2.3.4:443#a\nss://x@2.2.2.2:80#b\nnot a node\n")
        _, count, _ = nf.fetch_source(self.cfg, "someone/repo/main/sub")
        self.assertEqual(count, 2)

    def test_base64_payload(self):
        raw = b"trojan://pw@1.2.3.4:443#a\nvless://u@2.2.2.2:443#b\n"
        self.stub(base64.b64encode(raw))
        _, count, _ = nf.fetch_source(self.cfg, "someone/repo/main/sub")
        self.assertEqual(count, 2)

    def test_all_mirrors_failing_is_reported(self):
        def boom(url, timeout=40):
            raise OSError("network down")

        nf.http_get = boom
        _, count, payload = nf.fetch_source(self.cfg, "someone/repo/main/sub")
        self.assertEqual(count, 0)
        self.assertTrue(payload.startswith("unreachable"))

    def test_first_working_mirror_wins(self):
        calls = []

        def flaky(url, timeout=40):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("mirror one is down")
            return b"trojan://pw@1.2.3.4:443#a\n"

        nf.http_get = flaky
        _, count, _ = nf.fetch_source(self.cfg, "someone/repo/main/sub")
        self.assertEqual(count, 1)
        self.assertEqual(len(calls), 2)


class Subscription(unittest.TestCase):
    """What other devices pull: share links, a base64 list, and a Clash profile."""

    def setUp(self):
        self.cfg = nf.deep_merge(
            nf.DEFAULTS,
            {"state_dir": tempfile.mkdtemp(),
             "serve": {"bind": ["127.0.0.1"], "port": 7691, "token": "s3cret"}},
        )
        self.links = ["trojan://pw@1.2.3.4:443#a", "ss://x@2.2.2.2:80#b"]

    def test_base_url_uses_first_bind_and_token(self):
        self.assertEqual(nf.sub_base_url(self.cfg), "http://127.0.0.1:7691/s3cret")

    def test_public_url_wins_over_bind(self):
        cfg = nf.deep_merge(self.cfg, {"serve": {"public_url": "https://nodes.example.com/"}})
        self.assertEqual(nf.sub_base_url(cfg), "https://nodes.example.com/s3cret")

    def test_writes_links_base64_and_profile(self):
        nf.write_subscription(self.cfg, self.links)
        directory = nf.sub_dir(self.cfg)
        self.assertEqual(nf.read_lines(os.path.join(directory, "nodes.txt")), self.links)
        with open(os.path.join(directory, "sub.txt"), encoding="utf-8") as fh:
            decoded = base64.b64decode(fh.read()).decode()
        self.assertEqual(decoded.split(), self.links)
        with open(os.path.join(directory, "profile.yaml"), encoding="utf-8") as fh:
            profile = fh.read()
        # The profile must point the client at the http provider, not at a local file.
        self.assertIn("type: http", profile)
        self.assertIn("url: http://127.0.0.1:7691/s3cret/nodes.txt", profile)

    def test_nothing_is_written_for_an_empty_list(self):
        nf.write_subscription(self.cfg, [])
        self.assertFalse(os.path.exists(nf.sub_dir(self.cfg)))


class Serving(unittest.TestCase):
    def setUp(self):
        self.cfg = nf.deep_merge(
            nf.DEFAULTS,
            {"state_dir": tempfile.mkdtemp(),
             "serve": {"enable": True, "bind": ["127.0.0.1"], "port": 0, "token": "tok"}},
        )
        nf.write_subscription(self.cfg, ["trojan://pw@1.2.3.4:443#a"])
        self.servers = nf.build_servers(self.cfg)
        self.port = self.servers[0].server_address[1]
        threading.Thread(target=self.servers[0].serve_forever, daemon=True).start()

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def get(self, path, host=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, ""

    def test_token_path_serves_the_list(self):
        status, body = self.get("/tok/nodes.txt")
        self.assertEqual(status, 200)
        self.assertIn("trojan://", body)

    def test_requests_without_the_token_are_hidden(self):
        self.assertEqual(self.get("/nodes.txt")[0], 404)
        self.assertEqual(self.get("/")[0], 404)

    def test_served_profile_uses_the_host_the_client_asked_for(self):
        # A phone that can resolve one public name but not another must still get a
        # profile whose provider URL points at something it can fetch.
        status, body = self.get("/tok/profile.yaml", host="phone.example.com")
        self.assertEqual(status, 200)
        self.assertIn("url: https://phone.example.com/tok/nodes.txt", body)

    def test_directory_listing_is_refused(self):
        self.assertEqual(self.get("/tok/")[0], 403)

    def test_files_outside_the_sub_directory_are_unreachable(self):
        with open(os.path.join(self.cfg["state_dir"], "list.txt"), "w", encoding="utf-8") as fh:
            fh.write("trojan://secret@9.9.9.9:443#private\n")
        self.assertEqual(self.get("/tok/../list.txt")[0], 404)


if __name__ == "__main__":
    unittest.main()
