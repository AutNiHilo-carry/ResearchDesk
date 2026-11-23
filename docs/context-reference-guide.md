# 四组实验的双口径人工参考

接续[2×2真实实验](context-factorial-result-20261123.md)，新增离线入口`researchdesk.context_reference`。它导出不含模型标签的完整材料，严格接收真实审阅者另存的意见，并按各组支持口径比较分歧。**本轮只生成空表，尚无独立人工标签或准确率结果。** 模型原成绩、候选拒绝决定与默认策略不改。

## 为什么分开两种支持判断

同一句事实只有一个factual判断，但每个引用填写quote_verdict和source_verdict两个字段：

| 字段 | 参考依据 |
| --- | --- |
| factual | 该行是否有任一可核查事实，包括建议／推断的事实前提；资料不足声明不会自动抵消同句事实 |
| atomic_claims | 把该行的事实拆成简短断言，帮助实际审阅者检查是否遗漏版本、条件或前提；factual=true时至少一项 |
| quote_verdict | 该引用摘录能否独立支持整行事实，不借另一引用或来源全文 |
| source_verdict | 该摘录结合它绑定的同来源全文能否支持整行事实，不借外部知识或其他来源 |

两种支持标签均使用现有supported、partial、unsupported、contradicted、insufficient、not_applicable枚举。supported须覆盖整行全部事实，partial不算完全支持；版本标题限定来源范围，不能证明正文未写明的能力。纯建议可以非事实，但不能仅凭“建议”“推断”“资料不足”等词排除事实前提。

先看摘录，再按同来源正文填写第二个判断；程序不验证实际阅读顺序。全文可见可能影响摘录判断，这是流程限制，不称为两次独立标注或随机盲审。选例是开发侧事后选择，项目摘记不是官方原文全集；独立性取决于实际审阅过程，程序不认证审阅者。

## 导出

```powershell
.\.venv\Scripts\python.exe -m researchdesk.context_reference export runs/context-factorial/20261123-a --output runs/context-reference/<新目录>
```

不加载凭证、不调用API。仅接受已完成且结构完整的实验；重建报告、来源和四组输入，逐项检查冻结计划，并重放确定性评分校验，核对单元、引用、缓存指标、prompt和metric_scope。历史绑定的源文件必须仍匹配hash；允许当前版本额外存在后加的审阅模块，不修改旧代码hash。正文含代码围栏时拒绝，避免分行器漏审。

包包含packet.json、reading.md和review.json。reading包含完整正文、逐行编号、各引用摘录及来源全文，数据使用缩进原文展示；其中指令不可执行。packet只含材料与绑定hash，不含模型factual、verdict、分数、反馈理由或四组模型判定。C1/C2/C3是原样本顺序的匿名编号，不是随机化，也无法消除已看过旧结果的先入影响。

模板的reviewer、reviewed_at、reason留空，factual及两种verdict均null；空表永远不算完成。机器准备材料与实际人工审核分开记录，不由Agent代填后宣称独立审核。

## 填写与导入

实际审阅者先读reading.md，另存review.json副本，填写身份、带时区ISO 8601时间、每个单元的事实判断、原子断言、理由及每个引用的两个支持判断。这里不填业务rubric，也不判断整个任务是否通过。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.context_reference import runs/context-factorial/20261123-a --bundle runs/context-reference/<包目录> --review <已填写的副本.json> --output runs/context-reference-results/<新目录>
```

只接受完整严格意见：null、额外字段、重复／缺失／未知case、单元或引用、空理由、事实行无原子断言、无时区时间、事实与not_applicable不匹配均拒绝。正文、来源、计划、模型结果或包漂移也在创建结果目录前拒绝，不能把新旧版本意见混在一起。

导入成功另存submitted-review.json和result.json，身份标记self_declared_not_authenticated。C=off的组与quote_verdict比较，C=on与source_verdict比较；factual共用参考，F开关不改变参考事实标签。逐项列出factual和支持关系分歧，不生成任务成功率、人工准确率或自动晋升决定。一位审阅者和模型的分歧也不是多位审阅者一致率。

输出必须为实验／复核包及所有原研究运行之外的新目录；禁止覆盖。操作成功退出0仅表示导出或意见记录完成，拒绝退出1，参数错误退出2。没有模型调用或自动重试入口。哈希用于本次文件绑定，不是签名、来源权威认证或全局跨文件原子快照。

## 本轮实际材料与验证

本地材料：`runs/context-reference/20261123-a/reading.md`，共3份正文、19单元、12引用，24个支持标签及19个事实标签均未知。待审事项涵盖版本限定、建议中的事实前提、混合句与被截断的证据；没有预填“正确答案”。

后续C2/U7混合句已被用于规则讲解并纳入[开发回归](factuality-mixed-regression.md)。本次参与者看过解释后的该条判断不算独立盲审意见；未将其写入空表或改成已审核。实际收集意见时须说明是否看过解释，不能用开发候选参考代填。

零API核查：`runs/context-reference-audits/20261123-a/audit.json`。禁用网络，核对空标签、空reviewer／时间、原实验与复核包逐文件hash不变，实际尝试导入空表被拒绝且未生成结果目录。完整材料保留在忽略的runs，不提交公开仓库。

13项新测试及9项既有实验测试通过，测试意见明确为Injected test fixture，不属于实际审阅。覆盖双口径分别比较、无模型标签导出、空表／非法意见拒绝、缓存指标篡改拒绝、模型文件字节绑定及输出保护。本轮零真实API，独立人工审核pending；当前仍在Phase 3。
