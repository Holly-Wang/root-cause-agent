# 仓库协作指南

> 本项目中的文档统一使用中文编写，包括说明文档、设计文档和贡献指南。代码标识符、命令及必要的专有名词可保留原文。

## 项目结构与模块组织

本仓库用于构建面向受控小型 Python 案例的根因分析（RCA）Agent。项目需求见 [`brief.md`](brief.md)，规格及任务存放在 GitHub Issues。`rca_agent/` 包含配置、模型通信、文件取证、分析循环和证据报告；`tests/` 从公共分析入口验证行为；`examples/average/` 是最小受控材料与配置。运行方法和当前限制见 [`README.md`](README.md)。

初版范围是 macOS/Linux 上的单模型、单任务流程：从获授权的工作目录收集证据，只运行本次明确许可的测试，并生成可追溯的分析报告。不要假定项目支持任意命令、自动修改代码、生产环境诊断或并行 Agent。

## 构建、测试与本地开发

运行时仅使用 Python 标准库，要求 Python 3.11+、macOS/Linux。已验证的检查命令是 `python3 -m unittest tests.test_analysis` 和 `.venv/bin/python -m mypy`。开发依赖由 `pyproject.toml` 的 `dev` 定义，通过 `.venv/bin/python -m pip install -e '.[dev]'` 安装。

CLI 使用 `python3 -m rca_agent --config <配置文件> --output <报告目录>`；已验证缺少密钥时仍保存失败记录，真实模型运行待单独记录。DeepSeek 凭据由环境变量 `DEEPSEEK_API_KEY` 提供。不要将模拟通信测试当作真实接入已通过。

## 编码风格与命名约定

使用 Python 类型标注和 dataclass 表达运行配置、证据和结果，遵循 `pyproject.toml` 的 mypy strict 配置。模型、证据收集、工具、报告生成、运行控制和评测应各司其职。授权范围与资源限制必须作为分析和验证流程的明确约束。

## 测试与评测

使用标准库 unittest，从 `run_analysis` 公共入口及少量 CLI 验证配置、证据回传、引用、权限拒绝、工具故障与基础限额/超时行为；仅替代外部模型通信，使用真实临时文件。不要测试私有函数或内部模块调用顺序。

计划使用五个可复跑案例：三个开发案例、两个留出案例，其中一个材料不足。评估答案不得进入模型或工具可读取的输入；真实模型运行与模拟回归应分别记录。后续行为落地时补齐证据不足、完整超时和取消，并如实保留失败原因。

## 提交与合并请求

提交标题应简短并使用祈使表达，关联对应 Issue。合并请求需说明行为变更和范围、列出已执行的验证、指出授权或运行控制方面的改动，并关联相关问题或设计文档。尚未通过真实模型验收的任务不能仅凭模拟回归关闭。

## 安全与配置

只分析用户授权的工作目录，只运行本次明确许可的测试。模型输出或后续用户输入不得自动扩大宿主权限。不要将凭据写入源码、日志或报告；如需密钥，应通过环境变量等安全配置提供。保留证据来源信息，并区分测试断言失败、工具错误和未经验证的假设。

## Agent skills

### Issue tracker

Issues 和 specs 存放在 GitHub Issues 中，使用 `gh` CLI 操作。参见 `docs/agents/issue-tracker.md`。

### Triage labels

使用五个标准标签：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human` 和 `wontfix`。参见 `docs/agents/triage-labels.md`。

### Domain docs

采用 single-context 布局；相关文件存在时，读取根目录的 `GLOSSARY.md` 和 `docs/adr/`。参见 `docs/agents/domain.md`。
