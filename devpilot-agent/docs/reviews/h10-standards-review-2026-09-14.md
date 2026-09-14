# H10 Standards review — 2026-09-14

审查员：子 agent Halley（只读）；复核同一审查员完成。

## 初次结论：NEEDS_FIX

1. 启动脚本允许 `C:\` 状态目录和允许根目录；
2. 新启动控制面后没有等待 `/healthz`，可能导致 nanobot 先进入 API offline；
3. 现场证据只有汇总，没有 chat、operation、时间和日志级引用。

## 集中修复

- `start-integrated-harness.ps1` 解析并强制 `StateRoot`、`AllowedRoots` 位于 E 盘；
  特别保留 `E:\` 根路径，拒绝 `C:\...`。
- 控制面新启动或既存时均轮询 `/healthz`，30 秒未就绪即停止，不启动 nanobot。
- H10 证据补充本轮与历史记录的明确区分、chat/operation/time/JSONL 路径和可复跑命令。

## 增量复核：PASS

复核确认：E 盘根路径不会退化为 `E:`；C 盘反例被拒绝；PowerShell parser 与
`git diff --check` 通过。该 PASS 只覆盖当前 H10 增量，不改变 H10-A/H10-D 尚未最终完成的状态。
