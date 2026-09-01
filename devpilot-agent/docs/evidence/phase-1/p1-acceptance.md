# P1 acceptance evidence

状态：已完成（2026-08-31）。

## 实际闭环

真实 nanobot + DeepSeek 连续执行 3 次，均以退出码 0 完成：

1. Python Fixture：`repo-analysis` → `devpilot_inspect_project` → `devpilot_publish_artifact`。
2. Node Fixture：同一链路，第 1 次。
3. Node Fixture：同一链路，第 2 次。

SQLite 复核结果：`Project=2`、`Run=3`、`RunEvent=9`、`Artifact=3`；3 个 Run 均为
`completed`，每个 Artifact 均有独立 UUID 路径、大小和 SHA-256。

## 工具与边界

- nanobot entry point 已发现：`devpilot_inspect_project`、`devpilot_publish_artifact`。
- 项目路径先解析真实路径，再检查 allowed roots；越界路径会被拒绝。
- 扫描只读取元数据和受限文本，不执行仓库代码，不访问网络，不读取疑似密钥文件。
- 扫描上限：单文件 1 MiB、最多 20,000 文件、元数据预算 200 MiB。
- Artifact 名称固定为 `project-overview.md`，由服务端分配目录并原子写入，内容上限 1 MiB。
- Artifact 内容命中高置信度 Secret 模式时拒绝发布。
- Skill 仅支持本地 catalog → validate → reviewed/enabled → publish；不支持远程 URL 或市场安装。

## Fixture 真值

| Fixture | 技术栈 | 入口 | 测试 |
|---|---|---|---|
| Python | Python | `pyproject.toml:[project.scripts].fixture-python`、`src/demo/main.py` | `tests/sample_checks.py` |
| Node | Node.js | `package.json:main=src/index.js`、`build/test` scripts | `test/index.test.js` |

## 质量门禁

- `uv sync --dev`：通过。
- `uv run alembic upgrade head` / `alembic current`：`0001_initial (head)`。
- `uv run pytest`：16 passed。
- `uv run ruff check src scripts tests`：通过。
- `uv run pyright`：0 errors。
- `uv run python scripts/secret_scan.py`：通过。
- `uv run python scripts/check_markdown.py`：通过。
- `pnpm build`：通过。

## 已知限制

- Windows 原生没有 bwrap；P1 是只读分析闭环，不宣称已具备 OS Sandbox。
- nanobot E2E 结束时上游 HTTP 流关闭会输出异步生成器 warning，但进程退出码为 0，Tool、数据库和 Artifact 均已核验成功；该 warning 不在本 P1 范围内，后续若影响稳定性再单独建立 ADR。
- P1 尚未开放 `devpilot_run_check`、Git 变更执行、审批或任意进程能力；这些属于 P2。
