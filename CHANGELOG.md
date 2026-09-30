# Changelog

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
