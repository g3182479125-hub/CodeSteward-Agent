# P6-02 杭州 FC 兼容性诊断

状态：真实端点已验证出创建/执行能力；P6-02 生命周期契约已补齐；已增加显式
provider-default 兼容模式，但该模式不宣称网络隔离。

## 配置边界

- 地域：杭州
- API endpoint：`https://api.cn-hangzhou.e2b.fc.aliyuncs.com`
- Domain：`cn-hangzhou.e2b.fc.aliyuncs.com`
- Template：`code-interpreter-v1`
- API Key：仅存在于本机环境变量，未写入本文档、代码或日志。

## 现场结果

1. 使用当前 `FCSandboxBackend` 和 `network_allowed=False` 运行
   `uv run --extra fc-sandbox devpilot sandbox-check`。
2. 杭州端点返回 HTTP 400：`Feature 'SessionNetworkPolicy' is not supported`。
3. 用不发送该 SDK 网络策略参数的兼容性探测验证了：临时沙箱能够创建，写入一个
   非敏感 Python fixture，执行固定命令并调用 `kill()` 销毁；退出码为 0。
4. 这只能证明端点的基础生命周期可用，不能证明网络隔离策略已生效。当前通过
   `provider-default` 明确接受降级能力，不能把它写成网络隔离通过。

## 代码处理

`fc_sandbox.py` 现在将该供应商错误归类为稳定的
`FC_CAPABILITY_UNSUPPORTED`，而不是笼统的 `FC_EXECUTION_FAILED`；不会自动降级为
未受控网络模式。若用户显式设置 `DEVPILOT_FC_NETWORK_POLICY_MODE=provider-default`，
适配器会省略 FC 不支持的网络策略参数，并在结果中标记 `network_policy` 为
`provider-default`；控制面将其视为降级可用，而不是网络隔离通过。其他鉴权、超时、
创建、上传和执行异常也保留稳定错误类别。

## 下一步

严格模式仍需要确认杭州 FC 的网络策略替代配置或可证明的默认隔离语义。若本地开发
明确接受 provider-default，可按 runbook 设置后重启控制面；网页中的“执行”会在聊天
确认后进入远程沙箱，但结果必须显示降级网络策略，不能把它当作断网沙箱。

## P6-02 生命周期实现与证据

- `FCSandboxBackend.run()` 保留原有同步入口，并登记 `run_id`、创建后的
  `sandbox_id`、状态、退出码、受限输出和清理结果。
- 新增 `get_run_state()`、`collect()`、`cancel()`、`cleanup()` 和 `recover()`；重复
  取消不会创建新任务，无法在重启后查询供应商状态时返回 `unknown`，不重新创建任务。
- 创建、上传、执行、超时/取消和清理失败分别映射为稳定的脱敏错误；清理失败不会
  覆盖主执行错误，主执行成功但清理失败时不会报告完整成功。
- Fake SDK 契约测试覆盖：正常完成、重复 Run ID、取消幂等、清理失败、恢复未知；
  非零退出码保留、该组测试共 18 项通过。真实 FC 运行采用自有合成 fixture，覆盖
  `python-syntax` 通过、非零失败、超时、取消和已结束沙箱恢复为 `unknown`，并确认
  每次短生命周期运行都调用 `kill()` 清理。
- 真实 nanobot 网页验收使用 ChangeSet `edaa74b1-3cb2-4401-bfaa-0f0511109376`：
  聊天确认后 Apply 成功，FC 杭州 `code-interpreter-v1` 验证返回 `exit_code=0`，
  结果标记 `network_policy=provider-default`。
- 真实 FC 失败回归使用非敏感语法错误 fixture：返回 `exit_code=1` 和 stderr，适配器
  不抛成成功；对应沙箱仍在 `finally` 中清理。该结果与 0 退出通过案例分列记录。

以上“真实 FC 可创建/执行/清理”和“FC 网络隔离”是两个独立结论；前者已证，后者
仍未知。`recover()` 对已结束或无法查询的沙箱只返回 `unknown`，不重新创建任务；
跨进程持久化供应商证据和网络隔离证据留给 P6-03，不提前标记完成。
