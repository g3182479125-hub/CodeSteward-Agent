---
name: replace-with-kebab-case-name
description: One-sentence capability description.
metadata:
  devpilot:
    version: 0.1.0
    status: draft
    allowed_tools: []
    risk_level: R0
    source: catalog
---

# Skill name

## 适用场景

明确什么时候可以选择此 Skill。

## 禁用场景

明确什么时候必须拒绝或转人工确认。

## 前置条件

- 项目已注册并完成路径确认。
- 所有输入均在允许根目录内。

## 允许 Tool

只列出本 Skill 需要的固定 Tool；不允许任意动态 Tool。

## 步骤

1. 收集结构化事实。
2. 校验输入边界。
3. 生成结构化结果和 Artifact。

## 输出契约

输出必须说明事实来源、失败项、时间和关联 Run。

## 失败处理

遇到路径越界、能力缺失或不确定结果时停止，不编造结果。
