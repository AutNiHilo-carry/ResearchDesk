# 作用域参考标签审阅口径v1

本文件是待独立审阅的工程口径，不是已验证gold。不要将模型原标签或开发者预期直接复制成参考答案。当前样本属于Prompt冻结后的文本留出，但已查看模型结果，后续一旦用于调参便不能继续作为未见数据报告。

## 分维度判断

**declarations：** 仅检查原文事实、必要前提是否被侧车完整表达。complete不代表事实正确；原文与证据有冲突，但原文断言已经完整声明，可以仍为complete，而由support承载冲突。遗漏断言、把事实错误标为nonfactual/context、遗漏必要条件，分别说明incomplete或uncertain的依据。

**conditions：** preserved表示必要前提已正确保留，或合理地声明没有必要条件；missing表示漏掉证据/适用范围中的必要前提；unnecessary表示把证据不要求的前提强加成必要条件；无法确定必要性时选uncertain。简单“文字出现了条件”不等于条件正确。

**support：** 对带声明条件的整个断言判断。supported需证据覆盖全部事实；partial为仅部分事实获得支持；contradicted需要对同一对象、版本和适用条件的明确反证；insufficient为资料不足以证明，包括仅有其他版本信息、仅表可能却要求确定结论等。版本不同本身不自动证明结论为假。

这些维度不互斥。强加必要网络前提可能同时导致conditions=unnecessary与support=contradicted，但必须解释反证针对的是必要性，而非“在网络可用时能读取”这项更弱陈述。不要因最终都不放行，就认为任意负面标签都等价。

## 审阅步骤

1. 阅读packet.json中的原文、声明片段和提供的摘录；不使用外部知识补全。
2. 为每个父级与已声明断言填写标签及简短理由，保留所有ID；误分类的片段只在父级报告，不擅自新增断言ID。
3. 引用锚点逐字取自对应摘录；没有相关锚点时可为空，但supported必须有非空锚点。不要用URL代替内容证据。
4. 不确定时保留uncertain/insufficient，不为了填满标签猜测。填写真实审阅信息和带时区时间，仅在实际完成后确认。

## 离线工具

```powershell
python -m researchdesk.scope_review export --plan EVALUATION/plan.json --output NEW_REVIEW_DIR
python -m researchdesk.scope_review import --plan EVALUATION/plan.json --review FILLED_REVIEW.json --output NEW_IMPORT_DIR
```

模板没有模型结果、开发者预期或预填标签；它仍显示原始侧车声明及结构缺口，故仅对模型/工程标签隐藏，不是完全盲测。不要额外打开model-result文件再宣称独立盲审。

导入绑定源计划及数据包哈希，复用作用域检查器的严格ID和逐字锚点验证。不验证审阅者身份或独立性，也不证明参考标签语义正确。结果仅为human_declared_not_independently_verified，身份核验false，不自动晋升、不改原评分。工具无法阻止用户伪报审阅，但代理不会代填或声称已完成人工审阅。

六个留出案例已导出至runs/scope-check-evals/text-holdout-20261116/reference-review-v1，仍待真实审阅，未生成已审核参考集。本轮没有模型/API调用。
