# Project overview

## 技术栈

- `Python`

## 入口

- `pyproject.toml:[project.scripts].fixture-python`
- `src/demo/main.py`

## 目录

- `src`
- `tests`

## 运行方式

仅根据项目声明列出候选入口；本报告没有执行仓库代码。

- `pyproject.toml:[project.scripts].fixture-python`
- `src/demo/main.py`

## 测试

- `tests/sample_checks.py`

## 依赖

- `pyproject.toml`

## 风险

- 只读扫描；未安装依赖、启动进程或访问网络。
- 忽略目录、疑似密钥文件、二进制内容和超限文件不会进入上下文。

## 未知项

- 未发现额外未知项。

## 证据文件

- `pyproject.toml`
- `README.md`
