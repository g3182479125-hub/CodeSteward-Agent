---
name: daily-dev-report
description: Build a factual daily development report from approved Git and DevPilot run records.
metadata:
  devpilot:
    version: 0.1.0
    status: draft
    allowed_tools:
      - devpilot_git_summary
      - devpilot_run_history
    risk_level: R0
    source: catalog
---

# Daily development report

## 适用场景

用户主动请求或已启用的日报计划触发时，汇总当天有证据的开发活动。

## 禁用场景

没有 Git 或 Run 事实时不得编造工作内容；不得自动发送外部消息；不得因为无变化重复制造通知。

## 输出

生成带日期、时区、事实来源、失败项和关联 Run 的 Markdown Artifact。
