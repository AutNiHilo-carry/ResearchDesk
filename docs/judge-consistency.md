# 评分器事实性分类对照

离线命令：

```powershell
python -m researchdesk.judge_consistency --left original-quality.json --right candidate-quality.json --cases candidate-pairs.json --output new-result.json
```

`judge-consistency-v1` 比较显式声明的候选改写对，固定两份完整评分文件SHA256、评分器Prompt哈希、单元ID和精确文本。案例格式为 `judge-consistency-cases-v1`；relation必须为 `candidate_paraphrase_not_verified_equivalence`，不能冒充人工确认的语义等价。每个pair包括id、left和right，左右锚点各包含unit_id和text。

检查拒绝变动评分、混合Prompt、不完整结果、重复单元/案例/单元对、非布尔标签及文本锚点不匹配。输出比较数量和事实性标签差异数量，保留原标签；accuracy为null，human_review为pending，不修改质量指标，不调用模型。

真实案例位于 `runs/citation-format-live/wal-20261112/consistency-cases-v1.json`：原稿U3与候选U7。它们是值得检查的候选近义结论，尚未人工确认等价；两次报告上下文不同，且工具不证明模型版本/参数相同。因此差异只是排查线索，不能认定某个标签正确、模型不可靠或某策略造成变化。无差异也不等于正确。

下一阶段若研究评分器一致性，应人工确认等价关系、固定证据及上下文、记录模型与参数，再对独立版本评分器做受控实验。当前工具只提供可追溯的离线回归材料，不更改默认验证器或历史成绩。

[保留结论标签复核材料](abstention-label-review.md)整理8组候选句对与4处保存标签差异，附完整上下文和空白人工表；事后开发选例不构成准确率结果。

## 一行拆成多行的对照

同一命令也接受 `judge-consistency-groups-v1` 案例文件。将 `pairs` 换成 `groups`，每组的 `left` 和 `right` 都是原文锚点数组，`relation` 为 `declared_rewrite_not_verified_equivalence`。每侧每个单元只能出现一次。两个评分文件仍需绑定相同的评分提示；分组模式额外要求文件内含 `payload` 和规范 JSON 的 `input_sha256`，离线重放保存标签并核对派生分数后才进行对照。

输出保留整组每行的事实性标签、逐条引用判定和理由，并记录纳入分母的事实行数差值与获完整支持的行数差值。以 [SQLite 备份的真实评分](final-body-gate-guide.md#固定评分器的真实对照) 为例，原末行整体被排除，拆出的零阻塞判断纳入分母但只获部分支持。该组事实行数差值为 +1，完整支持行数差值为 0。结果可供复核使用，研究结论、语义等价和参考标签仍需独立确认。

## 输入快照的绑定

案例文件只读取一次：解析和 cases_sha256 使用同一份原始字节。两份评分也分别从各自读取的原始字节解析和计算哈希。若案例路径在读取后被替换或删除，输出仍绑定实际比较的旧内容，不会把旧案例的结果错误标为新文件哈希。复现时必须保存或找回对应哈希的输入文件，不能只依赖可能已变动的路径。

这保证每份输入的解析与哈希一致，不提供三个文件的全局原子快照，也不认证作者或证明磁盘文件之后未变。案例内的评分哈希继续约束左右评分，文字锚点和 Prompt 校验仍然执行。回归测试在读取左侧评分时替换或删除案例文件，确认输出保留原案例哈希和比较结果；测试不调用模型。

本地复核包位于 `runs/overflow-quote-disagreement/20261125-a/`。其中 `comparison.json` 为保存标签的对照，`review-inputs.json` 和 `review-template.json` 分别提供原问题、固定资料、两份完整评分输入和未填写的独立复核字段。两份复核材料未展示模型标签或得分。样本是在观察分歧后选出的开发案例。
