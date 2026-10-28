# RCA 五例评测材料包

此材料包为 `root-cause-agent-v1` 的 T7/T8 真实评测准备候选案例。它包含四个材料充分的 Python 故障案例和一个材料不足案例。候选案例已放在项目 `examples/case01-average/` 至 `examples/case05-order-export-insufficient/`；独立答案和评分依据在项目根目录 `evaluation-private/`，只供评测者使用，绝不能复制进任一案例的 `material/` 目录，也不能授权给被测 Agent。

每个配置都把自己的 `material/` 作为授权目录；许可测试只调用该目录内的固定检查脚本。评测者应先人工检查源码、问题和检查脚本，再冻结两个留出案例及评分依据。不要把本包的存在或答案路径写进发给模型的消息。

案例角色与类型：

| 案例 | 角色 | 覆盖点 | 充分性 |
| --- | --- | --- | --- |
| `case01-average` | 开发 | 算法计算错误 | 充分 |
| `case02-discount` | 开发 | 百分比单位换算 | 充分 |
| `case03-csv-boolean` | 开发 | 字符串解析与 Python 真值 | 充分 |
| `case04-shipping-boundary` | 留出 | 边界条件 | 充分 |
| `case05-order-export-insufficient` | 留出 | 信息不足时请求补充 | 不足 |

这些是受控教学案例，不是从 GitHub 复制的生产代码。它们需要评测者确认答案与测试后才可视为冻结案例。真实评测应使用最终 Agent 代码版本运行五例，并为每次运行保存完整原始产物和独立评价。
