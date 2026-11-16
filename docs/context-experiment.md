# 初始资料上下文策略实验

Deep Agents继续负责运行时与既有上下文primitive。本轮个人贡献是资料呈现策略、恢复约束和受控对照；不把框架的summarization、compaction或filesystem能力称为自研。

## 两个策略

- full（默认）：保持既有行为，导入资料的全文与元数据放入初始消息。
- catalog（可选）：完整资料仍保存在Ledger中，初始消息只提供source id、标题、类型、hash、字符数和read_source入口。正文通过已有read_source工具按需读取，逐字摘录仍由record_evidence检查。

```powershell
.\.venv\Scripts\researchdesk.exe "研究问题" --local-only --document ./notes.md --context-mode catalog
```

catalog只允许本地资料限定模式，不改变工具参数schema、权限或重试规则。它没有实施全文检索、自动摘要或分页；read_source仍返回全文。读取后的资料会进入对话，所以不能声称整个长任务上下文有界，也不能仅凭初始消息缩短就宣称节省Token。

两种策略都保留完整Source与Evidence，catalog不会截断正文或把标题当证据。字符数仅用于观察输入体积，不是Tokenizer计费结果；真实总Token由所有模型轮次的usage汇总。

## 恢复

run.json保存context_policy的版本、策略、初始字符数和输入hash。input.json保存实际准备好的初始消息。恢复直接使用它，不重新读取外部文件或重建目录，也不允许通过--context-mode覆盖策略。新格式检测到输入/hash或策略版本异常会拒绝继续；旧运行无context_policy时沿用原恢复行为，不推断它经过了新的hash校验。

## 受控pilot

```powershell
.\.venv\Scripts\python.exe -m researchdesk.context_eval
# 先用一项任务验证命令与产物
.\.venv\Scripts\python.exe -m researchdesk.context_eval --limit 1
```

默认使用已有三项受控开发任务，各策略各生成一次，不运行修订。两组固定相同模型名、系统prompt代码、研究问题、输出约束、资料内容hash、工具权限、调用预算10、外部资料请求0、工具重试0和评分规则。每组独立运行原任务rubric评分与line/v2引用评分。记录源代码hash，代码在两组执行间变化则不能作为完整对照。

当前顺序固定full→catalog，尚未随机化或平衡顺序；本地资料导入时间、运行路径、模型服务状态及不同报告的评分批次上下文也可能影响结果。任务少、文档短、同模型评分且参考标签未独立人工确认，不视为独立held-out测试或显著性实验。已观察到评分器波动，因此不把单次通过率差异当作可靠性结论。

比较内容：

- Task Success Rate与每任务rubric、Citation Precision、Evidence Coverage。
- 初始字符数、全部生成模型轮次的Token、单独的评分Token。
- read_source逻辑调用数、实际尝试数和成功读取数。
- 每任务生成耗时、工具成功率与重执行次数（沿用原评测产物）。

短文档上catalog可能多一次模型/工具往返，导致总成本更高；同一工具预算下，额外读取也可能挤占证据记录预算。这些都是策略代价，不自动增加catalog预算来掩盖。失败、未知用量与评分失败均保留，未知不按0计算。

## 输出与后续

runs/context-evaluations/<ID>/result.json和summary.md汇总两组结果及catalog减full的每任务差值；full/与catalog/各自保留正常评测目录、原报告、资料快照、完整回执和评分输出。它们仍可使用独立人工review导出流程。

一次完整pilot的退出码0仅表示两组评测已完整记录，不等于catalog胜出；3表示评分不完整，1表示驱动/控制条件错误。默认策略始终保持full，不按更低的字符数或更漂亮的一次评分自动晋升。

如果该策略在当前任务上无收益，应明确保留负面结论。后续若要测试更长资料，需先冻结新的长文档任务、控制输入/评分预算并增加独立重复和人工标注，再考虑分块读取或检索策略；不在短文档结果上推断长任务优势。
