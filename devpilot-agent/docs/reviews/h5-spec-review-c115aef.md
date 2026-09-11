# H5 Spec Review：c115aef

## 复核信息

- 复核类型：Spec / 验收范围增量复核
- 复核员 agent：`01a08f4a-cd64-7012-bd4d-7c9093fb8158`
- 基线：`0303533`
- 候选：`c115aef`
- 范围：仅 H5-A～F 及 H6 范围边界；未审 H6 实现。

## 结论

**NEEDS_FIX**

## 条款结果

| 条款 | 结果 | 说明 |
|---|---|---|
| H5-A | 部分通过 | 固定 runner、JSON、30 秒、64 KiB 和无动态 pip 已实现；profile/template 绑定仍不够强。 |
| H5-B | NEEDS_FIX | 非 FC 会被拦截，但后端能力只依赖可伪造的 name/属性；缺少固定 profile/template 强校验。 |
| H5-C | NEEDS_FIX | Grant 和字段白名单已增加，但字段名白名单不等于 Host 数据投影，值级敏感范围仍不完整。 |
| H5-D | NEEDS_FIX | Grant 可由调用方直接构造，缺少 Host 签发/持久验证；operation key 也未包含 project_id。 |
| H5-E | NEEDS_FIX | 持久化是可选的；缺少取消、重启恢复和未知结果查询闭环。 |
| H5-F | 部分通过 | trial/execute 共用 FC 路径且可重放，但未接入激活路径，预算和未知结果契约仍不完整。 |

## 阻断项

1. 强制真实 FC capability/profile/template 校验；生产 runner 必须注入真实持久化 store。
2. Grant 改成 Host 可验证授权，持久化原子预算计数；operation key 纳入 session/project。
3. 增加 cancel/recover/query operation；未知结果必须查询或暂停，不能只返回 in-progress。
4. 输入投影应包含 schema/值级允许范围，而不只是字段名。

## 范围核对

H6 的 `inspect/define/trial/activate/stop/remove` 尚未提前实现；本候选仍属于可复用的
H5 执行边界，未建立第二套 Runtime 或管理页面。

## 已核对命令

- H5、FC、P3 定向 pytest：通过。
- Ruff：通过。
- 本轮未重新发起真实 FC；已有现场记录和限制见 `docs/evidence/live-harness/h5-candidate-execution.md`。
