# 评测区分生成失败与最终发布拒绝

2026-11-24，将[显式最终正文门槛](final-body-gate-guide.md)接入任务运行器和摘要。**正文存在但发布检查拒绝时，标为publication_rejected，不再笼统显示generation_failed；不会给未发布候选调用语义评分器。** 本轮仅脚本输出和离线回归，零真实API，不修改历史评分或默认策略。

## 状态与成本

| 状态 | 含义 | 模型评分 | 应用状态 |
| --- | --- | --- | --- |
| completed | 正常完成并交付；启用最终门槛时完成态核验通过 | 沿用现有评分流程 | 仍需合并固定正文合同及模型门槛 |
| publication_rejected | 有可核验候选及失败回执，实际未发布report.md | not_scored／unknown | failed |
| invalid_artifacts | 声明、策略、正文、回执或发布状态矛盾，无法确认合法拒绝 | 不评分 | failed |
| timed_out | 任务进程超时 | 不评分 | failed |
| generation_failed | 其他未完成情况 | 不评分 | failed |

超时保持优先，不因磁盘残留拒绝标记改判为合法发布拒绝。未发布任务仍计入任务成功率的失败分母，不记为已完成交付；model_gate=unknown，不编造引用精度或覆盖率0分。原generation_completion_rate继续按completed交付记录计算，不借候选存在抬高完成率。

每项仍保留生成阶段的预算／model_calls／Token等generation_metrics；跳过评分不是生成免费。所有任务都被拒绝时没有评分调用，evaluator_usage=[]、evaluator_total_tokens=0表示明确未调用，而非评分准确率0；未知生成用量仍null。

## 可选运行参数

现有task_eval.run_task接受runtime字段final_body_limit，例如与固定任务正文合同一致的200。它传入CLI并记录final_body_policy，默认不传则原命令和行为不变；非法布尔、字符串或范围在任务目录创建前拒绝。生成输入仍只有问题和原后缀，不把rubric／参考标签放入请求。

该字段是运行器显式配置，不是ResearchTask数据schema或通用评测CLI的新默认。实验仍须在自己的冻结计划中保存配置、代码及预算；本轮没有进行新模型实验。

## 拒绝记录需要核验

不能只凭error_type="FinalBodyRejected"推断已经可靠检查。新增final_body_gate.verify_rejection(run)：

1. 要求停止运行的manifest为failed／FinalBodyRejected、有有效冻结策略且没有report.md。
2. 复制application.sqlite3及WAL／journal到私有临时目录，前后核对文件hash，拒绝链接逃逸或复制期间变化；在副本中读取策略、来源、Evidence和预检回执，不让SQLite打开原路径。
3. 将保留候选按原引用规则纯规范化（格式不合法则按原文检查），重算最终本地检查及预检匹配关系，逐项核对完整失败回执。
4. 返回publication=blocked、回执hash和完整本地检查；任何不一致归为invalid_artifacts。运行器再核对读到的候选hash与检查对象一致，避免把两份正文混为一项。

引用格式正常但ID未知时，可以出现“字数通过／引用结构失败”，摘要会分别显示。全角引用规范化导致的raw候选与实际检查对象差异被保留，不能仅凭原始文本hash不同就认定回执损坏。

这些是停止运行材料的结构／绑定核验，不是语义复评、签名或全局多文件原子快照。旧的完整评分重检和delivery合同没有放宽；全批候选均未发布、没有质量缓存时，不把它包装成完整评分交付包。可单独调用verify_rejection只读检查拒绝材料。

## 摘要与回归

任务摘要保留原指标，新增“最终发布被拒绝”区，按实际拒绝策略打印字数／上限、body状态、citation_structure、semantic_support=not_assessed和publication=blocked。它不会把本地检查通过显示成研究正确，格式化已有摘要也不等于重新核验源文件。

15项新增测试及既有运行器／最终门槛测试共35项通过。使用注入的固定长正文和真实CLI／SQLite落盘，验证：合法拒绝分类、回执／候选篡改及孤立report.md拒绝、超时优先、不调用业务或引用评分器、非法参数预拒绝、原文件只读核查、全角规范化、引用失败与长度失败分开、运行时丢失参数或退出码矛盾拒绝。测试不构成模型质量或人工参考。

本地可读机制示例：`runs/publication-rejection-examples/20261124-a/summary.md`，明确标注为脚本输出而非真实研究成绩。显示publication_rejected、not_scored、模型unknown、应用failed、201/200与publication=blocked。provenance保存源结果hash、零API和原文件不变声明；原始示例与测试数据留在runs，不提交仓库。

默认最终门槛和草稿工具仍关闭，不因分类更准确就宣称报告质量改善。当前保持Phase 3，后续真实对照应同时记录未发布候选、已消耗资源与最终交付率，不能靠跳过失败评分抬高结果。
