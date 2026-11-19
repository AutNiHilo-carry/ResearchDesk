# 正文长度：独立的确定性检查

[前提正文实验](premise-body-pilot.md)中，业务 rubric 三项均通过，但报告去掉引用与空白后仍有563个字符，未达到200字要求。`body-length-v1` 把计数作为应用检查，不让模型自行判断是否遵守长度，也不把引用/业务分数与长度混成一个模型分数。

## 计数合同

使用 Python Unicode 码点数量，只忽略 Unicode 空白和规范 Evidence 引用：`[E` + 16位小写十六进制 + `]`，也支持成对全角括号。字母、标点、Markdown、代码围栏及其内容、HTML、零宽字符和普通链接均计入；不按 Token、字节、视觉字形或中文分词计数，不进行 Unicode 归一化。组合字符和 emoji 序列可能占多个码点。

上限是明确传入的1到100000正整数，等于上限通过。空正文、只含空白或规范引用的正文，以及损坏的 Evidence 引用括号为 invalid；超过上限为 failed。非规范 ID 不作为可忽略引用，不因其被写在括号内就隐藏正文。引用 ID 是否真实、摘录是否支持结论由证据验证独立处理；长度通过不证明引用有效或正文有意义。

结果保存计数规则、上限、完整输入文本哈希、实际计数、状态和原因，不修改或截断正文。哈希是本地一致性绑定，不是防篡改签名。围栏、注释和文本中的“忽略本段”等内容不能改变计数。

## 离线使用

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_constraints ./body.md --max-chars 200 --output ./new-body-check.json
```

输入为明确的 Markdown/text 正文文件，检查所有传入文本，不自动删除 Evidence Ledger 附录。输出必须是新的 JSON 文件，不覆盖原文件；返回码通过0、超长3、无效输入或错误2。全程不加载凭证、不调用模型。

## 任务评测接线

新 `task_eval` 批次在准备模型评分前，以原评测已有的正文读取规则取得 body，保存独立 `constraints`：默认检查200码点，模型提示词中的原200字要求和模型评分提示词不改变；精确计数口径在评测结果与本文公开。此版本仍是候选应用合同，不声称自然语言中的“字”只有这一种解释。

每条任务分别输出 `model_gate`、`constraint_gate` 和 `application_gate`。模型与长度均通过时应用检查才 passed；已知长度失败或无效即 failed；模型评分未知而长度通过仍为 unknown。生成未完成不能应用通过。长度失败也保留模型评分以便诊断，没有自动修改报告、自动重试或跳过付费评分的策略。

原 `success` / Task Success Rate 保留原模型辅助口径，不能当成新应用检查通过率。新增应用状态存放在 `application_assessment`；消费方要检查这个字段，不能只读原 success。最终人工验收仍 pending。这里接入的是任务评测入口，尚未改变普通 Agent/工作流的修订选择或生产执行门槛。

## 显式应用验收与退出码

新任务评测可显式选择自动化退出条件：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval --dataset evals/technical-selection-v1.json --limit 1 --gate application
```

此命令仍会生成报告并调用配置的模型，沿用已有费用与预算行为；`--gate` 不会增加调用，也不自动修订。所有任务的模型与确定性检查都通过才返回0，失败或未知返回3。`cli-gate.json` 单独记录所选合同、逐任务状态和退出码；原 result.json 的模型分数不改。默认或 `--gate model` 保留原模型口径；任务评测入口不会默认把候选应用合同推广到所有调用方。

已有完整评测可以零 API 检查：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval --check-result ./evaluation/result.json --dataset ./evals/technical-selection-v1.json --output runs/evaluation-gates
```

`--check-result` 默认选择 application，可显式选择 `--gate model`。它先复用有绑定的质量校验，核对任务集、冻结资料、正文、证据与已保存引用标签，再校验业务 criterion ID、布尔标签、报告原文引用和派生通过字段。最后重新计算各任务门槛，不信任缓存 application_assessment 或批次总通过率。它不重新判断标签语义，也不认证本地文件的作者身份。

本入口检查完整保存批次，不接受 `--limit` 或 `--timeout` 等生成控制；不能通过运行参数悄悄只验收其中一题。

结果另存到新的独立目录：gate.json、summary.md 和包含输入哈希的 provenance.json。来源文件和历史成绩不写回；不加载凭证、不调用模型。通过返回0，失败或未知返回3；无效/不完整批次、缓存不一致等校验错误返回1，并且不生成通过结论。空任务批次、重复任务、缺失评分不能变成通过。旧批次没有长度记录时，应用结论为 unknown，须保留这段历史空缺，不能临时补算后声称原批次已经执行了新规则。

`--summarize` 仍只表示成功生成阅读材料，返回0不表示验收通过；不能与 `--gate` 同用。报告策略 / Context 对照命令的退出码仍表示实验是否完成，自动化若要检查各组应用验收，应对每组 result.json 单独运行 `--check-result`。人工验收仍为 pending。

冻结的 `report_eval` 新计划额外保存 constraint_policy，并由全部应用源码哈希绑定实现。旧待执行计划不会在新代码下悄悄继续；应重新准备，保留原计划。旧已完成结果不新增分数或修改通过率，离线摘要显示 not_checked，应用状态不会由缺失长度记录自动变为通过。

`--with-quality --dataset ...` 的有绑定摘要还会对冻结正文重新执行确定性计数，拒绝被替换、删除或与策略不符的新批次长度缓存。普通只读摘要只展示并检查记录结构，不访问正文，不构成缓存真实性认证。历史文件和人工标签不迁移。

## 验证与边界

离线回归覆盖200/201边界、全角引用、非规范 ID、Unicode空白/零宽字符、组合字符、围栏内容、空输入、非法上限和 CLI 禁止覆盖。任务级测试使用明确标注的脚本供应商，证明模型全通过也不能覆盖超长、模型未知不被补判通过、旧结果未检查、以及冻结正文与缓存不一致时拒绝。

真实失败正文只需离线重检，不重新调用模型。它证明计数机制能暴露已知违规，不证明生成质量改善，也不解决联合推断、过强建议或 rubric 覆盖不足。当前没有自然语言自动抽取任意约束、自动压缩或新 Agent。

2026-11-18 已用新 CLI 重检上述真实新稿，得到 failed、563/200、body_too_long；返回码3，原正文逐字节哈希未变，零 API。独立结果保存在本地 `runs/constraint-probes/b702e79ca87347c6bcd13598be42b3e3/`，没有回写旧评测或把它伪装成当时已执行的检查。

2026-11-19 对历史后序三题 v2 运行零 API 结果验收：application 为 unknown、退出3，显式 model 为 passed、退出0；原 result.json 哈希未变。独立记录位于 `runs/evaluation-gate-probes/application/46affc0ccfd54f489088fe5df2f2dad5/` 和 `runs/evaluation-gate-probes/model/ef2bf5aedc06467fa14465e1e09b3d70/`。这是对历史已有模型标签的校验，没有补造长度记录或新增模型判定。

## 显式向生成端说明计数规则

默认 `legacy` 保留旧 TASK_SUFFIX 的原始字节及其问题哈希规则。候选 `body-length-v1` 在旧后缀之后追加精确计数说明，来自同一计数模块；没有改变验证器、评分提示词、门槛或模型预算，也没有证明能提高遵循率。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_eval --dataset evals/technical-selection-v1.json --task-id version_scope --task-id wal_default_claim --generation-contract body-length-v1 --max-model-calls 4
```

这是零 API 准备命令。所选任务、资料和 rubric 保持原值，两组写作策略都使用同一生成计数合同；此计划比较写作策略，不是 legacy 与计数提示的效果对照。显式生成合同不是新的 Agent runtime 或自动压缩器。

`task_eval` 新付费评测也支持同一 `--generation-contract body-length-v1`，可同时指定 `--gate application`。计划执行、已有结果验收或离线摘要不接受用此参数替换保存的版本。Context、fault 和 paired 入口尚未提供此候选选项，仍用 legacy；不会通过共享常量静默改变它们。

冻结计划和新批次保存逐任务 generation_contracts，包含版本、实际后缀哈希、精确提示文本和约束规则。单一版本批次的 task_prompt_suffix_sha256 是实际后缀哈希；混合版本的直接 API 批次不伪造单一哈希，使用逐任务记录。新增提示也计入生成输入预检上限。

实际 run_task 命令附带该问题后缀，记录完整问题哈希。候选运行须同时具有一致的运行记录和 run.json.question，否则在请求评分前记 invalid_artifacts。rubric 仍不进入生成问题。回放按保存的任务版本核对实际问题，拒绝丢失或改变候选绑定；旧 legacy 问题和已完成历史不自动升级。已有待执行计划因源码变化须重新准备，不绕过原源码哈希检查。

同一控制变量的评测审计会拒绝不同 generation_contracts，不能把改变提示词的结果标成其他变量完全固定。生成计数提示与长度拦截分别记录；提示有可能仍被模型忽略，不能因此放宽应用验收或自动追加重试。

2026-11-19 按上述两题准备 `runs/generation-contract-plans/5746f5fdbf8547d3abe02f453d26ffd4/`，两组均为候选合同，每任务每组生成上限4次，含评分最多20次 SDK 请求。准备阶段零 API；后续已完成[两题真实对照](counting-contract-pilot.md)，实际15次请求、四份报告均未通过应用门槛。执行结果另行保存，没有将旧外部副作用题的失败改成成功。
