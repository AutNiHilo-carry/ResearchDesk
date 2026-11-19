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
