# 可恢复的受检研究工作流

过去研究、语义验证、单轮修订和诊断需要分别执行。本命令复用已有CLI、Deep Agents/LangGraph检查点、Evidence Ledger、修订策略和观测工具，增加应用层阶段编排。workflow.json记录进度，不替代框架运行时或图检查点。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.workflow "仅依据资料说明部署方式、写者限制和证据缺口" --local-only --document ./notes.md
# --resume接收工作流目录，不是研究Run ID
.\.venv\Scripts\python.exe -m researchdesk.workflow --resume runs/workflows/<工作流ID>
```

不提供文档时可沿用原研究CLI的联网检索，需要原有模型及Tavily配置。--context-mode catalog仍要求本地资料限定模式，默认full。新工作流默认模型请求额度12，可在创建时用--max-model-calls修改；恢复禁止替换问题、资料、模型策略或额度。单个研究运行的旧CLI默认方式不变。

## 阶段和判定

1. 冻结输入：导入的最多四份UTF-8 md/txt文件复制到工作流corpus/并保存hash；不保存API Key。
2. 研究：调用原研究CLI，保存研究Run ID。中断后复用这个ID、原输入、权限和预算；已经完成的研究不重跑。
3. 验证/单轮修订：调用现有repair_run，最多一个候选分支、最多一轮修订。原稿已过支持门槛时直接保留，不强行改写。
4. 判定绑定：检查选中正文hash、原稿hash、缓存验证的输入和prompt hash，重新解析已保存标签并离线计算相同支持门槛，不额外调用评分器。
5. 诊断：为原研究和选中运行导出独立诊断。执行或预算失败时尽量为已存在的运行导出诊断，诊断失败不掩盖原执行状态。

状态含义：

| 状态 | 退出码 | 含义 |
|---|---:|---|
| model_support_gate_passed | 0 | 本次模型辅助证据门槛通过，人工复核仍pending。 |
| needs_review | 3 | 研究和最多一轮修订已结束，但支持门槛未通过；保留原稿和候选。 |
| verification_outcome_uncertain | 3 | 缺少可接受的持久化验证结果（含网络中断或评分格式失败），不自动再付费评分。 |
| budget_exhausted | 4 | 继承的模型额度已耗尽，不清零或自动调高。 |
| interrupted | 130 | 中断，保留当前阶段和已有运行以恢复。 |
| research_failed / needs_inspection | 非0 | 研究失败、缓存与报告不匹配、输入变化或不完整fork等，需要检查。 |

支持门槛沿用原规则：Citation Precision ≥0.9、Evidence Coverage ≥0.6，且不退化、不减少已支持行数、无无效或冲突引用。不是所有事实都已得到支持的保证。未通过时选中原稿仅表示回退，没有“验收通过”的含义。

无独立业务rubric时，模型支持门槛不能代替任务验收。模型评分已有波动，本工作流不通过多数表决、重复重试或降低门槛来制造成功。状态model_support_gate_passed也不是事实认证、人工批准或自动发布许可。

当前门槛按非空prose行检查。为防止漏评分，受检工作流暂不接受代码围栏或draft中被Evidence Ledger分隔符截去的额外章节；这些报告保留为待检查，不通过“只评分剩余部分”获得通过标记。普通研究CLI行为不受此限制。

## 恢复与预算

工作流持有自己的OS锁，各研究运行继续使用原RunLock。研究ID在子阶段返回时记录；若在记录之前进程退出，恢复先发现工作流research/下唯一的已有运行，不新建另一份研究。有多个研究或不完整候选fork时拒绝猜测或自动开新分支。

研究、共享模型的自动摘要、候选生成和修订验证均遵循原持久化调用额度。原研究已消耗的额度随候选继承，工作流只使用这个候选分支。独立批次评测不在本命令内自动运行。仍不是账号总预算、金额上限或全局墙钟deadline；模型HTTP超时、框架步数限制沿用现有实现。

完成后的resume只复查缓存与hash，不再调用研究或验证模型。报告、验证文件或证据变化后，不继续沿用旧通过标记。一次needs_review完成后也不会因为resume而自动多修订一轮。真正结果未知的验证沿用repair的保守策略；普通研究模型的图恢复仍按原框架及预算语义执行。

准备阶段未完成的研究或半写入fork，可能不能自动恢复；会保留内容并要求检查，不删除目录或静默创建新的付费分支。

## 产物

runs/workflows/<ID>/：

- workflow.json、summary.md：冻结参数、研究/候选ID、阶段、门槛结果、hash与人工pending状态。
- corpus/：导入文件快照。
- research/<RunID>/：原图检查点、证据、回执和报告。
- candidate/<RunID>/：原稿、最多一轮候选、差异与缓存验证。
- diagnostics/<ID>/：只含元数据的原研究和选中运行诊断。

selected_report始终是供复核的报告路径；只有status明确为model_support_gate_passed才表示模型门槛通过。读取方不能以路径或report.md存在作为研究成功判据。
