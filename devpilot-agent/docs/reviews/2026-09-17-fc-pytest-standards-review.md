# FC pytest profile 变更 Standards 复核

## 复核对象

- 固定点：`92884b9`
- 候选：`65cf72c`
- 增量证据与集成测试：`dd7a253`
- 本轮最终记录提交：`037c080`（仅补证据/审查记录，不改变实现行为）。
- 范围：`fc_sandbox.py`、`verification.py` 及相关测试、P6 证据。
- 审查员：Standards reviewer `01a0ad94-af8d-7c33-a195-b52dc2975cf7`（Pascal）。

## 结论

**PASS。** Standards reviewer 确认：

- `python-regression` 使用服务端固定的 `python -m pytest -q`，模型不能注入命令。
- pytest bootstrap 仅对 `project-pytest` 和 `python-regression` 生效。
- 项目 allowlist、显式快照、provider-default 标记和清理边界没有被放宽。
- 改动范围最小，无新的明显 Fowler 异味；集成测试覆盖调用顺序。
- 相关 pytest、Ruff、Markdown 和 diff 检查均通过。

增量复核确认没有遗留阻塞。FC 网络隔离未证明，按项目既定边界记录，不作为通过依据。

## 可复现检查

以下命令在 `037c080` 工作区执行，均为退出码 `0`：

```powershell
Set-Location devpilot-agent
$repoRoot = (Resolve-Path ..).Path
$env:PYTHONPATH = "$repoRoot\nanobot;$repoRoot\devpilot-agent\src"
& "$repoRoot\nanobot\.venv\Scripts\python.exe" -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check src scripts tests
& .\.venv\Scripts\python.exe .\scripts\check_markdown.py
git diff --check
```
