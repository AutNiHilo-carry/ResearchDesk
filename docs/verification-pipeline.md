# 独立版本的来源与语义联合检查

`verification-pipeline-v1` 是显式启用的离线组合检查，不替换现有工作流，不生成新的模型评分。

```powershell
python -m researchdesk.verification_pipeline RUN_DIRECTORY --quality ORIGINAL_QUALITY.json --require-origin extracted_body --output NEW_RESULT.json
```

输入必须是已经完成、停止运行的报告目录，以及该正文对应的工作流 v2 行级评测缓存。检查正文/证据指纹、评分器Prompt哈希和每个单元/引用标签后，使用原标签重新计算原语义指标。缓存过期、Prompt不匹配或输入变化直接拒绝；不发起模型调用补救。

来源分支单独要求所有已引用证据来自 `extracted_body` 或 `search_snippet`。缺失回执、预览回执、无引用及同一快照同时被搜索/正文工具返回不能确认为满足唯一来源要求；不同来源为mismatch，证据完整性失败为invalid。两种工具均返回同一内容时使用inconclusive，是保守的审计策略，并不意味着证据错误。

输出分别保存 semantic、semantic_gate 和 provenance，绑定正文、评测输入和缓存文件哈希。两条分支均通过才标记 `checks_passed_pending_human`，否则为 `needs_review`（CLI退出码3）。来源通过不会增加引用精度、覆盖率或把unsupported改为supported；语义通过也不会覆盖来源缺失。人工审核始终pending。

这是一项新验收策略，来源约束由调用者明确指定，不代表报告中的自由文本来源声明已经被自动抽取或验证。它不验证章节号、不证明模型没有采用其他资料、不解决评测器的事实性分类一致性问题。仅适用于停止运行的目录，跨文件读取不是全局原子快照；本地数据库也不是防篡改信任根。

首个真实样本离线组合检查保存在 `runs/citation-format-live/wal-20261112/verification-pipeline-v1.json`。旧工作流、报告、评分和门槛不改动。该版本尚未晋升为默认工作流策略，不能据此声称质量得到提高。
