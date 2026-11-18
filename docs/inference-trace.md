# 推断证据链：结构检查阶段

现有逐行引用评分分别判断每条引用是否支持整行，不能单独表达多个来源的前提共同支撑结论。inference-trace-v1 新增独立附件：原报告行 → 人工或应用声明的前提 → Evidence Ledger 证据。它只提供可审查的数据结构和完整性检查，不提供语义打分，也不替换原评分。

## 使用

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inference export runs/<RunID> --output runs/inference-traces/<新目录>
.\.venv\Scripts\python.exe -m researchdesk.inference check runs/<RunID> --trace ./trace.json --output runs/inference-checks/<新目录>
```

输入须为已完成研究运行。全程离线，不加载凭证、不调用模型、不修改原报告、数据库或评分。输出目录须不存在；先检查再创建，不覆盖历史附件。中断可能留下未完成附件，另选新目录处理，不把缺少 integrity.json 的目录当作已检查产物。

### 分析修订稿

修订被拒后，运行目录的 draft.md / report.md 仍是选中的原稿。若要分析被拒候选，须显式选择，不能把默认导出当成候选内容：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inference export runs/repairs/<RunID> --revision candidate --output runs/inference-traces/<新目录>
.\.venv\Scripts\python.exe -m researchdesk.inference check runs/repairs/<RunID> --revision candidate --trace ./trace.json --output runs/inference-checks/<新目录>
```

`--revision original` 则选择保存的修订前原稿。两者仅允许已完成的 repair_candidate 运行，核对 revisions 下对应正文与 repair-state.json 已存哈希；无候选、状态未完成、文件越界或内容变化均拒绝。候选保留全部正文，不按最终报告的附录规则截断。哈希是本地一致性绑定，不是来源真实性或语义正确性证明。

显式修订附件还绑定修订类型和完整 repair-state.json 文本哈希，与默认报告附件不能混用；状态后续变化需要重新导出。不传选项时保留旧快照格式及读取规则。修订附件仍全为 unclassified，不自动添加前提或判为通过。

后续 `researchdesk.inference_check prepare` 支持同一 `--revision` 选项。准备及执行前都会重新核对修订，执行请求不能移除选项降级为默认报告。代码更新也会使旧的待执行语义请求失效，需重新准备；历史结果保留。语义执行仍单独计费，本次只读导出不触发它。

2026-11-18 已对[预算提示真实对照](repair-budget-comparison.md)的两份被拒候选进行只读导出：off 为 11 个完整正文单元，snapshot-v1 为 9 个；均包含标题等非事实候选行，不等于事实句数。附件位于本地 `runs/inference-traces/6d859bbd45ce42e697b3207d3d9c6b88/`，两组所有分类均为 unclassified、前提为空，语义检查未执行、人工复核待完成。导出前后逐文件 SHA-256 对比确认两个原运行未变，本轮零 SDK 请求，不更改历史引用指标。运行附件不提交仓库。

导出覆盖全部正文行，kind 留 unclassified，premises 为空，rationale 为空，结果为 draft。保留全部行，即使本轮只研究几句，也不能在附件中省略其余行或替换原句。围栏和会被证据附录分隔符截掉的 draft 拒绝处理，避免静默漏检。

kind 可声明 direct_fact、source_attribution、synthesis、recommendation、abstention 或 nonfactual。分类属于声明，不是程序判断。每个前提包含局部唯一的 P1 等 id、statement 和 evidence_ids；rationale 记录简短、可审查的关联理由，不要求隐藏推理内容。纯建议可无事实前提；直接事实、来源陈述、综合及保留结论没有前提时保持 draft。前提没有证据同样保持 draft，不用空证据冒充已证实。

## 完整性边界

整个报告、全部证据和全部来源（包括未引用来源）共同生成快照哈希。检查拒绝快照变化、遗漏/重复/伪造行、改写原句、前提ID重复、重复证据、空白前提、未知证据或摘录/来源身份校验失败。每次检查还重新验证原行引用。

附件可以关联原行未引用但已有的 ledger 证据，输出显式记录 cited_on_original_line=false。这不会把证据偷偷补进报告或增加其 Citation Precision / Evidence Coverage。额外证据必须由后续修订明确引用，才能改变报告本身的引用关系。

即使所有结构约束满足，status 也仅为 structure_checked；semantic_verification 固定 not_performed，human_review 固定 pending。任意不相干的声明也可能引用一条完整的证据，结构检查无法判断其相关性、前提是否被支持、版本/条件是否一致、推断是否有效或结论是否过强。它不证明联合证据足够，不计算成功率，不自动晋升策略。

## 工程取舍与后续

先用一层“结论—前提—证据”结构，不做通用多层证明图、自动生成推理或新 Agent。单独附件保留原报告和历史指标，代价是暂时需要填写关联、处理附件版本，且语义仍未验证。相比立即放宽每条引用的门槛，这能先暴露缺失前提与引用范围，便于复核。

离线回归检验跨来源绑定、原文锚点、未引用资料变化、遗漏和重复、未知证据、待完成状态、伪造语义标签与隐藏正文。另用“证据完整但声明无关”的例子证明程序不会输出语义通过。这些测试不是独立人工标签，也不是质量改善实验。

待实际复核意见到位后，才决定如何独立评估前提支持、来源适用范围、推断有效性和结论强度。新语义口径需独立版本与对照实验，不能用结构检查结果替代现有门槛。
