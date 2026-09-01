---
name: repo-analysis
description: Analyze a registered repository using read-only project inspection and produce a traceable overview.
metadata:
  devpilot:
    version: 0.1.0
    status: active
    reviewed: true
    allowed_tools:
      - devpilot_inspect_project
      - devpilot_publish_artifact
    risk_level: R0
    source: catalog
---

# Repository analysis

## 适用场景

用户要求分析已注册项目的目录、技术栈、入口、依赖和测试结构。

## 禁用场景

不得写入源码、安装依赖、启动仓库脚本、访问未确认目录或根据不完整信息编造结论。

## 前置条件

- Project 已注册。
- Project 的真实路径已确认并位于 allowed roots。
- `devpilot_inspect_project` 处于只读模式。

## 执行步骤

1. 先确认用户指定的项目路径，并让 Tool 在 allowed roots 内做真实路径校验。
2. 调用 `devpilot_inspect_project`，只使用返回的确定性事实。
3. 对每个未发现的信息写“未发现”或“无法确认”，不得根据常见项目习惯补全。
4. 生成固定章节的 Markdown 报告。
5. 使用 Tool 返回的 `project_id` 和 `run_id` 调用 `devpilot_publish_artifact`，名称固定为 `project-overview.md`。

## 允许 Tool

- `devpilot_inspect_project`：R0，只读扫描。
- `devpilot_publish_artifact`：R1，仅写入服务管理的 Markdown Artifact 根目录。

## 输出契约

生成包含以下固定章节的 `project-overview.md` Artifact：技术栈、入口、目录、运行方式、测试、依赖、风险、未知项、证据文件。

报告必须说明：没有执行仓库代码；扫描受文件数、元数据字节数和单文件大小限制；忽略目录、疑似密钥和二进制内容不会进入上下文。

## 失败处理

路径越界、Tool 返回错误、超限或可能包含密钥时，停止发布并向用户报告稳定错误类别；不得改用 nanobot 内置 Shell 或直接写文件。
