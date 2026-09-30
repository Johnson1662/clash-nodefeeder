# nodefeeder

**English** — nodefeeder keeps a spare, live-tested node list fed into the Clash you already
run. It never starts a second proxy core, never empties the list when a source dies, and never
leaves you without a network: the generated router profile falls back to `DIRECT` when every
node is dead. Standard library only, one file, no dependencies.

---

给已有 Clash 的自动备用节点通道：定期从公开源抓节点，实测筛出少数几个真能用的，喂给你**正在用的**
Clash。不另起内核、不占端口、源失效时不清空名单、节点全死时自动走直连。

## 和你见过的"每日免费节点订阅"仓库有什么不同

| | 常见的每日订阅仓库 | nodefeeder |
|---|---|---|
| 产出 | 一份大订阅、几千个节点，你自己去测速 | 一份小名单、默认 8 个，**已经实测过** |
| 跑什么进程 | 它的流水线 + 你的客户端 | 只用你已有的 Clash；实测用一次性内核，用完就杀 |
| 上游挂掉时 | 常常把订阅覆盖成空的，客户端直接报错 | 低于阈值就**不写**，保留上一份 |
| 节点全死时 | 没节点可用 | 兜底配置退 `DIRECT`，网还通 |
| 出问题时 | 翻 issue | `nodefeeder doctor` 逐项报出来 |

它不提供节点，也不打算变成订阅站；它解决的是"我主力机场挂了，怎么临时还能上网，而且别把我的网搞坏"。

## 三个阶段

```
fetch   把多个公开源合并去重成一个大池子                 pool.txt
filter  只留 TCP 端口真的应答的                        alive.txt
pick    随机抽样，用一次性内核真发请求测，留下能用的     list.txt  ->  投喂给 Clash
        上一轮的名单优先复测，活着就留着（只进不退）
```

默认节奏：每 600 秒跑一次 pick，每 3 轮重抓一次源。名单上限 8 个。

## 快速开始

需要 CPython 3.11+，除此之外零依赖。

```bash
git clone https://github.com/Johnson1662/clash-nodefeeder && cd clash-nodefeeder
python3 nodefeeder.py init          # 写一份配置到 ~/.config/nodefeeder/config.json
$EDITOR ~/.config/nodefeeder/config.json
python3 nodefeeder.py profile       # 生成一份可直接导入 Clash 的配置（路径已填好）
python3 nodefeeder.py doctor        # 自检：源、状态、内核、Clash 接线
python3 nodefeeder.py once          # 跑一轮
```

**Clash 侧**：把 `profile` 生成的那份文件导入（Clash Verge → 配置 → `+` → 本地文件）。
平时不用启用它，主力机场挂了再切过去。

**常驻**：

```bash
python3 nodefeeder.py install                    # 写 systemd --user 单元
systemctl --user enable --now nodefeeder         # 开机自启
journalctl --user -u nodefeeder -f               # 看日志
```

单元里记的是脚本的绝对路径，**移动过仓库就重新跑一次 `install`**，否则它会继续引用旧位置。

## 配置

`nodefeeder init` 会写出全部字段，常用的这几个：

| 字段 | 含义 |
|---|---|
| `sources` | 公开节点源路径列表（默认 11 个，随时会失效，别当成长期资产） |
| `mirrors` | 下载镜像模板，`${path}` 会被替换成源路径；按顺序尝试 |
| `state_dir` | 池子与名单存放目录（默认 `~/.local/state/nodefeeder`） |
| `schedule.interval` | 每轮间隔秒数；`refetch_every` 是每几轮重抓一次源 |
| `limits.min_merged` / `min_tcp_alive` | **低于这个数量就不覆盖旧文件**（防上游抖动清空池子） |
| `limits.sample` / `keep` | 每轮抽多少候选实测 / 名单保留多少个 |
| `probe.url` | 实测用的目标，默认一个会返回 204 的地址 |
| `core.binary` | 留空自动找（PATH → Clash Verge 自带内核） |
| `clash.data_dir` | Clash Verge 的数据目录，留空自动探测 |
| `clash.list_file` / `provider` | 名单文件名 / Clash 里的 provider 名，**两边必须一致** |
| `clash.control` | `auto` 优先用 Verge 的 unix socket，其次读运行配置里的控制口 |
| `clash.verify_port` | 填上你 Clash 的混合端口（如 7897），`doctor` 会顺便实测出口 |

## 命令

```
init      写一份起始配置
fetch     抓源合并去重
filter    TCP 存活筛选
pick      抽样实测并把名单投喂给 Clash
once      fetch + filter + pick，跑完退出
run       常驻循环
doctor    自检：源可达性、池子、内核、Clash 接线、出口 IP
profile   生成可导入的 Clash 配置（provider 路径按你的配置填好）
install / uninstall   systemd --user 单元
```

## 为什么名单里只有 8 个

一次真实测量的漏斗（2026-09-30，同一台机器）：

```
抓 11 个源  ->  合并去重 13438 个节点
TCP 握手    ->   3126 个端口真的应答（23%）
实测抽样 800 ->     6~10 个真的能穿出去（约 1%）
名单        ->       8 个，按延迟排序
```

免费节点的现实就是最后那 1%，而且**几分钟就会死**。所以本工具的定位是备用通道，不是主力。

## 风险与边界

- 免费节点由陌生人运营，**他们能看到你的流量去向**（域名、IP、时间）。敏感操作别走这条通道。
- 上游源是第三方，随时改名、换路径或消失：这批源在 2026-09-30 一天里有 4 个失效（2 个改名、1 个换文件、1 个彻底没了）。`doctor` 会告诉你哪些已经死了。
- 只用于网络调试与学习，请遵守你所在地的法律与所在网络的规定。
- 不做 Docker 镜像、不做 Web 面板；`run` 就是 `systemd` 里的一条 `while true`。

## 开发

```bash
python3 -m unittest discover -s tests -v     # 24 个用例，覆盖解析、阈值写入、抽样、抓取回退
```

测试只覆盖会**静默出错**的纯函数（分享链接解析、防护写入、抽样）；网络与 Clash 的真实接线用 `doctor` 现场验。

## 许可

MIT
