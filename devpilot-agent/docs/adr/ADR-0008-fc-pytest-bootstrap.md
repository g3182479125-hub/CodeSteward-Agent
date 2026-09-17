# ADR-0008：FC Python 项目测试的固定 pytest 启动安装

## 状态

已接受（2026-09-17）。这是 N1 受控项目测试的范围例外，不开放宿主机 Shell，
也不接受模型提供的依赖安装命令。

## 背景

当前 FC 内置 `code-interpreter-v1` 模板没有 pytest。创建新模板需要额外的镜像仓库
凭据，而演示项目的 Python 测试依赖 pytest。用户明确要求先完成视频录制用的真实
全流程，因此需要一个不依赖自定义镜像的临时方案。

## 决策

- 仅当执行 profile 为 `project-pytest` 时，在同一个短生命周期 Sandbox 中先运行服务端
  固定命令：`python -m pip install --disable-pip-version-check pytest`。
- `DEVPILOT_FC_INSTALL_PYTEST` 默认值为 `1`；设置为 `0` 可关闭。
- 安装命令不经过模型、不接受用户项目中的命令、不写入项目快照，也不适用于 Node
  测试、普通 Python 执行、语法检查或模型候选执行。
- 严格网络策略下必须显式允许网络；`provider-default` 下如实记录网络隔离未知。
- 安装失败、超时或回执不完整都只能形成基础设施失败/未知结果，不得生成
  `failure_run_id` 或推动 bug-fix 提案。

## 后果

每次 Python 项目测试会增加安装时间、网络依赖和 FC 成本；pytest 版本未固定，后续
若要稳定验收，应改用预装 pytest 的自定义模板。当前方案只服务于录制和开发验证，
不把临时安装当作 FC 网络隔离证据。
