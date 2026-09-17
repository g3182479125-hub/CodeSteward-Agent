# P6-05：python-regression 真实 FC 回归证据

## 目的

本记录补充 `65cf72c` 引入的能力变化：正式 `python-regression` 验证不再被 FC
能力检查错误拒绝，而是使用服务端固定的 pytest 命令，并在同一个临时 Sandbox
内完成依赖启动安装和回归测试。

## 固定条件

- 代码候选：`65cf72c`，固定点：`92884b9`。
- 项目：`training-projects/python-project-template-bugfix`，仅在本机显式
  `DEVPILOT_FC_ALLOWED_PROJECTS` allowlist 中。
- FC：`fc-agent-sandbox`，region `cn-beijing`，template `code-interpreter-v1`。
- 网络：`provider-default`，请求 `network_allowed=False`；网络隔离未证明。
- pytest 配置：`DEVPILOT_FC_INSTALL_PYTEST=1`。
- 服务端固定命令：
  `python -m pip install --disable-pip-version-check pytest`，随后
  `python -m pytest -q`。模型和项目文件都不能提供或修改这两个命令。

## 真实闭环

### 1. 故障基线

项目保持未修复状态，真实 FC 项目测试返回：

```text
command: python -m pytest -q
status: failed
exit_code: 1
failure_run_id: c7abbb30-fcc5-4476-b7a7-c03941cf2716
```

该运行的预期结果是 3 个已知缺陷失败、2 个测试通过，作为 Bug-fix 提案的失败证据。

### 2. 提案与应用

- Bug-fix ChangeSet：`eb6f1dbb-7d50-4ff3-b6db-84928c2086e8`。
- 允许修改：仅 `main.py`；测试文件是验收规格，不在补丁范围内。
- 正式 `r1.apply` 成功，变更前哈希为
  `dd957367759f4221db9b8bfcdea3e8000ae5a434601506c121d1fc85ceb81fa1`。

### 3. FC 回归验证

- 正式 `r2.verify`，profile：`python-regression`。
- 控制面 Run：`e36b45e8-d172-489c-8178-978746c3bfda`，事件中记录完整的
  `changeset.verification_completed` 回执。
- Sandbox：`sbx-9d504d1e-39f3-4698-a25a-e32da91576e2`。
- 结果：`status=verified`、`exit_code=0`、`duration_ms=23195`。
- 标准输出：

```text
.....                                                                    [100%]
5 passed in 0.02s
```

验证成功后由 FC 适配器完成 Sandbox 清理；回执保留 `network_policy=provider-default`，
没有把该策略表述成断网保证。

### 4. 回滚与录制复位

验证成功后正式 `r1.rollback` 成功，ChangeSet 状态为 `reverted`。最后再次运行项目测试
仍返回 `status=failed`、`exit_code=1`，当前 `main.py` 已恢复为上述基线哈希。因此录制
可以从“发现 3 个失败”开始，完成提案、确认、应用、5 passed 验证和回滚。

## 自动化覆盖

`test_control_plane_routes_python_regression_to_fc_pytest_bootstrap` 验证控制面、profile
检查、FC 后端、固定 pytest 安装命令和测试命令之间的完整调用顺序；FC 合同测试同时覆盖
bootstrap 失败、网络策略、配置开关和回执边界。
