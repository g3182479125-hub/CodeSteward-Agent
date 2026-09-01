---
name: git-change-summary
description: Summarize repository changes with read-only Git facts and testing suggestions.
metadata:
  devpilot:
    version: 0.1.0
    status: draft
    allowed_tools:
      - devpilot_git_summary
    risk_level: R0
    source: catalog
---

# Git change summary

## 适用场景

用户要求总结当前分支、工作区、Diff 统计、最近提交和建议测试。

## 禁用场景

不得 commit、push、checkout、reset、clean、删除文件或执行仓库脚本。

## 输出

输出变更事实、影响范围、风险、测试建议和 Conventional Commit 草案；没有变化时明确说明“无 Git 变化”。
