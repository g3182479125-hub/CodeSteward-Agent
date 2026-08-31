"""Stable Markdown rendering for project overview Artifacts."""

from __future__ import annotations

from .inspector import InspectionResult


def _items(items: list[str], empty: str = "未发现") -> str:
    return "\n".join(f"- `{item}`" for item in items) if items else f"- {empty}"


def render_project_overview(result: InspectionResult) -> str:
    """Render facts only; unknown facts stay explicitly unknown."""
    return f"""# Project overview

## 技术栈

{_items(result.technology_stacks, "未发现支持的技术栈")}

## 入口

{_items(result.entry_points)}

## 目录

{_items(result.top_level_directories)}

## 运行方式

仅根据项目声明的入口和脚本列出候选信息；本报告没有执行仓库代码。

{_items(result.entry_points)}

## 测试

{_items(result.test_paths)}

## 依赖

{_items(result.dependency_files)}

## 风险

- 扫描为只读，未安装依赖、启动进程或访问网络。
- 忽略目录、疑似密钥文件、二进制内容和超限文件不会进入模型上下文。
- 本次扫描元数据：{result.files_seen} 个文件，{result.metadata_bytes} 字节；
  截断：`{str(result.truncated).lower()}`。

## 未知项

{_items(result.unknowns)}

## 证据文件

{_items(result.evidence_files)}
"""
