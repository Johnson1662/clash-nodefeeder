# Changelog

## 0.2.0 — 2026-09-30

- 新增 `serve`：内置只读 HTTP 服务，让手机/第二台机器订阅同一份名单（路径可用随机 token 保护，绝不列目录，只暴露订阅文件）
- `pick` 除了喂给本机 Clash，还会写 `sub/` 三份文件：`nodes.txt`（分享链接）、`sub.txt`（base64，通用订阅格式）、`profile.yaml`（Clash 系客户端用的配置，内部是 `type: http` 的 provider，名单更新后客户端自己拉）
- 新增 `serve.public_url`：把对外的公开地址烤进 `profile.yaml`，手机拿到的就是公网地址
- 文档补充「在手机上用」一节，写明手机端 Clash 与 Tailscale 抢同一个 VPN 位、不能指望 Tailscale 直连这条路
- 新测试 8 个：订阅文件内容、base64 往返、token 路径、无 token 404、不列目录、`..` 越不出订阅目录
## 0.1.0 — 2026-09-30

首个公开版本，由一个自用脚本集重写而来。

- 三阶段流水线：`fetch`（多源合并去重）→ `filter`（TCP 存活）→ `pick`（一次性内核实测）
- 全部写入带阈值保护：上游抖动、池子为空、无一节点可用时一律保留上一份文件
- 名单"只进不退"：上一轮的节点优先复测，仍然可用就保留
- Clash 集成：自动探测 Clash Verge 数据目录与 unix socket，写名单后让 Clash 重读 provider；也支持直接指定 HTTP 控制口
- `doctor` 自检：源可达性、池子与名单新鲜度、内核、Clash 接线、可选的真实出口 IP
- `profile` 生成可导入的 Clash 配置，provider 名与绝对路径按本机配置填好，避免两边手抄不一致
- 单文件、纯标准库、CPython 3.11+；systemd `--user` 单元由 `install` 生成
- 24 个单元测试覆盖会静默出错的纯函数：分享链接解析（vmess 缺 padding / url-safe、ss 两种写法、IPv6、多协议）、防护写入、抽样、镜像回退
