# 文档导航

首次运行从 [快速开始](quickstart.md) 开始；项目定位和完成边界见 [当前阶段](status-20261117.md) 与 [方向约束](direction.md)。

## 运行与交付

- [受检工作流](checked-workflow.md)、[命令行全链路验收](workflow-cli-acceptance.md)
- [零 API 演示](offline-acceptance.md)、[离线 CI](offline-ci.md)、[wheel 安装](wheel-installation.md)
- [运行诊断](run-diagnostics.md)、[模型评分与最终验收](model-assessment-boundary.md)

## 工具、预算和恢复

- [工具控制](tool-policy.md)、[工作流工具预算](workflow-tool-budget.md)
- [模型请求预算](model-call-budget.md)、[尾部验证额度](tail-verification-budget.md)
- [恢复策略](recovery-policy.md)、[任务恢复评测](task-recovery-evaluation.md)、[恢复 CI 证据](recovery-ci.md)
- [模型响应/checkpoint 间隙](model-response-gap.md)、[评分结果落盘间隙](workflow-verification-gap.md)

## 证据、引用与修订

- [证据验证](evidence-verification.md)、[引用格式](citation-format.md)、[来源审计](source-audit.md)
- [单轮报告修订](report-repair.md)、[写作策略版本](report-writing-policy.md)、[写作对照](report-policy-experiment.md)
- [分句原文锚点](claim-granularity.md)、[正式分句策略](quality-clause-policy.md)、[粒度诊断](granularity-probe.md)
- [分句锚点 v2](clause-spans-v2.md)、[条件边界回归](clause-scope-regression.md)、[推断作用域边界](inference-scope-boundary.md)
- [验证组合管线](verification-pipeline.md)

## 任务评测与人工复核

- [任务评测](task-evaluation.md)、[人工审核协议](human-review.md)
- [技术选型8题](technical-selection-eval.md)、[开发侧审查](technical-selection-review-v1.md)、[模型首轮结果](technical-selection-pilot-v1.md)
- [评分合同校准](task-grading-calibration.md)、[长正文目标项复核](task-grading-replay.md)、[完整批次稳定性](task-grading-stability.md)
- [技术选型人工材料](technical-selection-human-review.md)、[引用评分分歧](judge-consistency.md)、[引用评分稳定性](judge-stability.md)

## 作用域、推断与候选参考

- [作用域侧车](scope-trace.md)、[语义检查合同](scope-check.md)、[人工参考口径](scope-reference-rubric.md)
- [推断证据链](inference-trace.md)、[独立推断判定](inference-assessment.md)、[推断合同 v2](inference-contract-v2.md)
- [推断校准](inference-calibration.md)

## Context 与历史

- [Context 对照实验](context-experiment.md)、[搜索返回预览](search-preview.md)
- [2026-11-16 阶段快照](status-20261116.md)、[2026-11-17 当前交付](status-20261117.md)

文档中的本地 runs 路径不代表公开附件。固定样例、脚本供应商、模型评分与真实人工标签分别标识；不把任一类自动转换成另一类。

评测任务可通过 [任务 ID 选择](report-task-selection.md)准备，避免复制或改写原任务集。

[后序三题真实对照](technical-selection-next-pilot.md)记录 PostgreSQL 并发与证据不足场景的分数、用量及待复核缺口。

[逐行引用缺口视图](citation-gap-view.md)复用已有模型标签，离线定位具体句子与证据关系。

[外部副作用题定向修订](targeted-repair-budget-result.md)记录反馈复用、实际预算停止和原稿保留结果。

[候选修订预算快照](repair-budget-context.md)说明显式启用、输入冻结、恢复拒绝及与硬预算的边界。

[修订预算提示真实对照](repair-budget-comparison.md)记录 off 与 snapshot-v1 的候选质量、用量及不晋升决定。
