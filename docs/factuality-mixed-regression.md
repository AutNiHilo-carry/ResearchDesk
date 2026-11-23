# 已讲解混合句：固定开发回归

把“保存以端点开始执行为界；未开始执行的情形资料未说明，证据不足。”作为已经讲解过的规则案例，保存到[6条固定开发例子](../evals/factuality-mixed-dev-v1.json)。**这不是新的人类金标，也不证明模型已经修好。** 本轮零API，默认提示词、历史分数和运行时代码未改。

规则依据是现有report-quote-v2的“任一部分含可核查事实就须判断”合同。前半句明确陈述保存行为的时间边界；缺少引用或后半句声明资料不足，不让前半句自动变成非事实。判为factual=true并不表示该行为已经被证实，只表示它需要被核查。

## 数据与对照

| 案例 | 内容／目的 | 开发候选factual |
| --- | --- | --- |
| case01 | 原混合句全文，无引用，保留后半句“证据不足” | true |
| case02 | 目前资料不足，无法判断何时保存结果 | false |
| case03 | 只保留“保存以端点开始执行为界” | true |
| case04 | 建议核对资料后再判断保存边界 | false |
| case05 | 建议沿用原键，因为系统会保存同键首次请求结果 | true |
| case06 | 建议先做实验 | false |

case05唯一引用来自明确构造的测试合同，不是任何真实服务的实测或官方能力证明。纯资料不足与纯建议作为对照，避免把修复简化为“出现某个词就一律算事实”。没有在生产验证器里增加关键词分类规则。

label_status保留agent_authored_candidate_references_not_human_gold，human_review=pending，independent_acceptance_eligible=false；explained_case_ids单独记录case01。参考标签、理由和解释曝光标识与units/evidence请求payload分开，不发送给评分类模型。保留已有6条factuality-boundary-dev-v1，不覆盖历史数据。

这条已经用于讲解，因此本次参与者再按解释作答不能算该条的独立盲审意见。没有将聊天回答转换为人工表。原审阅包和19个未知事实标签不改；以后收集真实意见时应记录是否看过解释，而不是用此开发参考代填。材料准备完成与研究质量验收完成分开记录。

## 回归验证实际检查什么

复用evaluation.benchmark_inputs、verification.prepare和evaluation.evaluate，无新增评分器：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_factuality_mixed_regression.py
```

5项测试涵盖：原句及未引用状态保留，候选参考不进入payload；模拟漏掉混合句事实前提时，即使唯一引用标签完全匹配，事实性差异仍触发开发gate失败；模拟把含事实前提的建议整体排除也失败；纯建议／纯弃答保持非事实；未引用事实仍计入事实分母；禁用网络与模型构造后运行测试。

这些响应是Injected test response，正确匹配参考的测试通过只验证评测能发现指定回归，不是模型准确率或实际修复效果。新增5项加既有22项验证器测试，共27项定向测试通过。

## 已有真实结果的零API核查

对[四组实验](context-factorial-result-20261123.md)先核对冻结输入、来源、原文、保存标签和派生指标，再按整句完全相同匹配原Stripe报告U7。结果如下：

| 原组别 | 保存factual | 与开发候选true是否相同 |
| --- | --- | --- |
| 00：原提示，仅摘录 | true | 是 |
| 01：加强事实提示，仅摘录 | false | 否 |
| 10：来源上下文 | false | 否 |
| 11：两种增量 | false | 否 |

这是原批次中的4个已有判断，不是对新增6条测试集的真实评测。3处与开发候选不同只能证明问题现场仍存在，不能换算为独立人工准确率。没有重新调用模型、修订正文或重算原成绩，也没有用已讲解参考回填人工验收。

本地`runs/factuality-mixed-regression/20261123-a/`另存payload、candidate-references和observation.json，附输入及源结果hash。禁用网络核查前后原实验目录逐文件hash相同，api_requests=0、scoring_recomputed=false、promotion=none；原始产物不提交仓库。

下一次确需模型实验时，可以用同一固定批次和未泄漏参考的payload检查候选判断，但必须预先冻结预算并保留失败；本轮没有发起实验。当前没有证据可以宣称提示改进成功，Phase 3独立质量验收仍pending。
