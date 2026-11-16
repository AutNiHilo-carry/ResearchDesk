# 验证范围侧车

`scope-trace-v1`补充现有inference-trace的原文作用域信息。它复用现有报告/语料完整性读取，不替换推断追踪、分句策略或语义评分。

```powershell
python -m researchdesk.scope_trace export RUN_DIRECTORY --output NEW_DIRECTORY
python -m researchdesk.scope_trace check RUN_DIRECTORY --trace EDITED_TRACE.json --output ANOTHER_NEW_DIRECTORY
```

export为每个原始报告行创建未分类片段。编辑trace时可将其划为assertion、condition、context、nonfactual；每个片段保留parent_id、start/end与精确text。start/end相对于split_units产生的去除首尾空白的行文本，不是文件字节位置。

同一报告行的片段必须无损覆盖原文，无遗漏或重叠。assertion可通过conditions链接同一行的condition片段；condition_review为pending、none_declared或linked。linked却没有链接、声明无条件却附条件链接均拒绝。未分类、未审条件、未关联条件、无引用断言均保持draft。

引用绑定以原始行内的引用括号位置为锚点，要求对应真实完整证据。scope支持declared_direct、candidate_pool、unresolved；后两种始终待处理。报告中的引用若未分配给任何断言也会保持待处理。一个引用可以被显式声明绑定多个断言，但完整性检查不证明这种多重绑定正确。

报告、来源或证据变更会使旧侧车失效。结构全部通过也仅为structure_checked，semantic_verification始终not_performed、human_review始终pending；不产生支持率、不更改默认门槛、不调用模型。

## 边界

role、条件是否完备以及declared_direct都只是待核验的声明。遗漏一个条件却将它标为context或nonfactual，工具无法自动发现；错误地声明不需要条件也不会因文字像条件而被自动纠正。本轮没有新增词法推断。跨行条件引用暂不接受，需要先人工检查作用域，而不是静默忽略。

因此该侧车用于把歧义显式保存，后续的语义检查需另行评测“声明是否完整、前提是否必要、引用是否支持带条件的断言”。不能把结构合法当作事实正确，也不应直接复用现有逐行模型标签作为侧车真值。

真实报告仅导出空白待标注模板，未自动填角色、前提或人工标签：`runs/report-policy-live/wal-v2-20261115/scope-trace-export-v1/`。使用的是被保留的原稿，不是被拒绝候选；候选结构实验仍保留于原冻结包。
