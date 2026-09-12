# H6 `input_rules` 工具契约澄清设计

日期：2026-09-12  
范围：H6 模型候选插件定义入口

## 背景

真实 DeepSeek + nanobot WebUI 验收表明，H6 的运行时校验正确，但模型无法从当前宽泛的
`input_rules` schema 推断合法字段格式，导致无效探测和临时候选。该问题只影响工具描述，
不改变候选执行边界。

## 设计

在 `ModelPluginManagementTool` 的参数 schema 中完整描述规则对象：

- `field` 为安全字段名且必填；
- `kind` 为 `string/integer/number/boolean` 之一且必填；
- `max_length` 为 1–65536 的整数；
- `min_value/max_value` 仅用于数值类型且必须为整数；`number` 类型的输入值仍可为小数；
- 通过 description 提供合法示例：`{"field":"name","kind":"string","max_length":64}`。

保留现有 `_parse_rules` 作为唯一运行时校验器，不增加新工具、不改变授权、FC proxy、
候选生命周期或持久化语义。

## 验收

1. DeepSeek 在新会话中使用 `{field,kind,max_length}` 一次完成 define，不再猜测字段名；
   数值类型按需追加整数 `min_value` / `max_value`。
2. 非法规则仍在写入 artifact 前返回定义错误。
3. H6 现有测试、H5/H3/H4 回归测试和静态检查通过。
4. 同一对 Standards/Spec reviewer 只复核本次增量及关联风险；未通过前不进入 H7。
