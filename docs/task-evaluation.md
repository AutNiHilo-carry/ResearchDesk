# Controlled-Corpus End-to-End Task Evaluation

## 定位
这批评测运行真实 Lead Agent 的计划、资料读取、Evidence Ledger 和带引用报告流程，
再独立评任务验收点与引用支持关系。外部搜索在执行层禁止，目的是先控制资料变量。
它不是网页研究 benchmark，也不是行业能力认证。

evals/research-tasks-v1.json 定义三个虚构产品场景：
- 单机离线选型：推荐合适方案，同时承认写者限制。
- 同版本、同生效日期资料冲突：呈现双方并保留最终判断。
- 没有 exactly-once 证据：说明已知能力，不从本地文件保存推出恢复保证。

这些任务未用于前面的句子标签回归集；参考验收点仍由项目维护过程整理，
尚未独立人工复核。任务少、资料虚构，不代表泛化能力。

## 运行
```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval
# 只跑第一个任务做冒烟验证
.\.venv\Scripts\python.exe -m researchdesk.task_eval --limit 1
```

支持 --dataset、--output、--limit 和 --timeout。
默认每任务 180 秒、最多 10 次应用工具调用、外部请求预算 0、工具重试 0。
各任务在独立子进程和 run 目录运行；超时仅终止本评测创建的子进程树。
生成阶段不接收验收 rubric，输入只含问题、固定输出约束和冻结资料。
资料内容快照及哈希、任务集哈希、模型名称、Prompt 哈希、参数、运行产物均保留。
同一服务商模型名称可能指向更新后的权重，因此不保证比特级可重现。

## 工具与恢复
新增 --local-only --document PATH，可独立用于研究 CLI。
只暴露 read_source / record_evidence 和框架 write_todos，
执行层也拒绝 search_sources，即使配置了 Tavily Key 也不能通过它绕过受控资料。
作用域写入原 run manifest，恢复仍使用原作用域。
评测驱动本身暂不支持 resume；其中各研究任务保留正常 checkpoint，可单独恢复。

## Task Success Rate
任务通过要求同时满足：
1. 子进程和研究 run 正常完成。
2. 任务模型 grader 对所有验收点判 passed，引用的报告片段必须逐字存在。
3. 引用完整性无错误；模型估计的 Citation Precision >= 0.9、Evidence Coverage >= 0.6。
4. 外部 provider 请求次数为 0。

生成完成率单独统计，生成一份文件不能直接算任务成功。
超时/生成失败计入任务成功率分母。
grader 输出缺项、编造报告片段或 citation judge 格式错误时标 scoring_failed，
Task Success Rate 为 null，不将未知当成成功或给出部分漂亮分数。
判定依赖模型和未独立复核的 rubric，必须人工检查争议项。

所有报告合并做一次任务 rubric 评分和一次引用支持评分，两者都无工具权限，
最多额外两次模型调用。引用评分固定为 line/v2，保留同一统计口径。
多条事实放同一行仍可能被保守判 partial；这会作为报告质量问题保留。

## 用量与可靠性指标
生成 Agent 每次模型请求记录 model_started/model_completed/model_failed。
只写调用 ID、Token usage、耗时和异常类型，不写提示原文、回复正文、Key 或隐藏推理。
Token 汇总覆盖所有模型轮次，而非最后一句；任一轮用量未知时总量记 null。
生成用量和两个评分器用量分开列出，避免把 grader 成本藏起来。

记录 Task Success Rate、生成完成率、引用质量、Tool Success Rate、完整 Token 用量、
生成耗时、工具重执行次数与评测耗时。
Tool Success Rate 来自持久化调度结果，权限/预算拒绝也纳入失败统计。
retry_count 表示工具第一次之后的额外执行（恢复重放也计入），不等于模型 HTTP 重试。
这批任务不主动注入恢复故障，Recovery Rate 为 N/A，不能混用之前故障单测的结果。
未配置价格表，因此 estimated_fee 为 null，不猜测人民币/美元成本。

## 产物
runs/task-evaluations/<evaluation_id>/：
- result.json、summary.md：汇总与每任务结果。
- corpus/：冻结的 UTF-8 资料。
- tasks/<task_id>/generation.log：CLI 日志。
- tasks/<task_id>/research/<run_id>/：原始研究运行、checkpoint、证据与模型调用日志。
- citation-quality.json：独立引用评分详情。

退出码 0 表示本次模型辅助评分全部通过；3 表示任务未全过或评分未完成；1 是驱动错误。
任何结果都不触发默认策略、Skill 或代码的自动更新。

## 限制
目前尚未覆盖真实检索噪声、网页变化、大规模任务集、多次运行方差、独立人工标签，
也未评估完整任务层面的故障恢复率。下一步应扩大真实任务和人工复核，而非只调当前三题。

