# P2A 结构化补丁、Diff 与回滚设计

## 目标

为 DevPilot 增加一条可审查、可回滚的 R1 代码变更基础链路：模型只能提出结构化变更草案，服务端负责边界校验、哈希并发控制、暂存、Diff 生成、原子应用和回滚。P2A 本阶段不开放生产写入口，不接入任意 Shell，不把 Windows 路径限制称为 OS Sandbox。

## 范围与不变量

- 支持 `create`、`update`、`delete` 三类文件操作。
- 变更只能落在显式允许的项目根目录内；拒绝绝对路径、`..` 逃逸、符号链接逃逸、`.git`、`.env*`、密钥/证书文件和超限内容。
- 单个 ChangeSet 最多 12 个文件、80 KiB 补丁正文、1,200 个变更行；单文件不超过 512 KiB。
- 每个文件记录前置 SHA-256、内容 SHA-256、大小和操作类型。
- 文件写入前必须检查测试专用 `TestAuthorizationGrant`；生产 API、CLI、nanobot Tool 不提供项目写入口。
- 前置哈希、范围或补丁校验失败时零部分写入。
- 应用过程中失败时恢复受影响文件；无法确认状态时锁定并拒绝后续写入。

## 组件设计

1. `patches` 领域模块：定义不可变的 `FilePatch`、`ChangeSetDraft`、校验结果和稳定错误码，不依赖 FastAPI 或 nanobot。
2. `PatchValidator`：解析相对路径、检查项目根和禁止项，读取受限源文件并计算前置哈希，验证限额和补丁完整性。
3. `ChangeSetService`：保存草案的规范化表示，生成统一 Diff 和哈希，按 ID 加载变更；不接受调用方在 apply 阶段重新提交补丁正文。
4. `LocalPatchApplier`：在目标项目同卷暂存目录中生成文件镜像，二次校验后原子替换；保存备份和前后哈希，支持幂等拒绝与回滚。
5. `TestAuthorizationGrant`：仅供测试和演示使用，绑定项目、任务、计划/补丁范围和过期时间；不进入 Prompt、Session、Tool 参数或 SSE。

## 数据流

```text
结构化草案
  -> 规范化路径/内容
  -> 校验限额、秘密与前置 Hash
  -> 保存不可变 ChangeSet + Diff
  -> 测试授权
  -> 再次加载并校验授权/Hash
  -> 暂存、原子应用
  -> 记录前后 Hash 与备份
  -> 成功或完整回滚
```

P2A 只保留服务层与测试入口。P2B 才将真实用户审批、任务/计划 API 和 nanobot 的“提出草案/请求审批”适配器接入；P3 才处理验证执行后端。

## 错误处理

所有拒绝返回稳定错误码，例如 `PATH_OUTSIDE_PROJECT`、`FORBIDDEN_FILE`、`PATCH_LIMIT_EXCEEDED`、`PRECONDITION_HASH_MISMATCH`、`AUTHORIZATION_REQUIRED`、`AUTHORIZATION_REPLAYED`、`APPLY_FAILED` 和 `ROLLBACK_FAILED`。错误消息不得包含密钥、补丁秘密内容或未经脱敏的环境变量。

## 测试与证据

- 单元测试覆盖路径逃逸、符号链接、禁止文件、二进制、限额、Diff 和状态转换。
- 集成测试覆盖创建/更新/删除、前置哈希变化、零部分写入、幂等拒绝和回滚。
- Python 与 Node fixture 各提供至少一个可修改小功能或 bug 用例。
- 运行既有 P1 回归、空库和升级迁移、Ruff、Pyright、Secret Scan 与 Markdown 检查。
- 证据记录测试授权缺失不写文件、真实 Diff→rollback 演练和失败时项目锁定行为。

## 回滚点

本阶段代码以独立提交交付；出现未知半应用状态时保留 manifest、备份和失败事件，人工确认前禁止继续写入。代码层面可回退到 P2A-01 提交，运行数据不提交到 Git。
