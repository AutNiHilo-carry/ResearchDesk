# 运行诊断与事件时间线

已有工具和模型事件分散在SQLite、JSONL与运行manifest中。inspect_run将它们汇总为诊断JSON与Markdown，帮助定位工具失败、预算问题、恢复重放、未知调用成本和引用完整性缺口。它是ResearchDesk的离线可观测性工具，复用应用日志，不自建Agent运行时，不调用模型或外部搜索。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inspect_run runs/<RunID>
# 支持实验嵌套目录，也可指定独立输出目录
.\.venv\Scripts\python.exe -m researchdesk.inspect_run <运行目录> --output runs/diagnostics
```

输出到新的runs/diagnostics/<ID>/result.json与summary.md。不修复JSONL，不写回源运行，不自动恢复任务，不覆盖历史诊断。拒绝把诊断输出写进源运行目录。无需API Key，不读取.env。

## 数据来源与一致性

应用SQLite以mode=ro、query_only打开，在单个读取事务中获取事件、dispatches、预算、回执与资料记录；诊断不依赖可能缺失或撕裂的JSONL。输出保留数据库事件序号和原时间戳，按数据库提交顺序排列，不将时间戳顺序当作跨线程因果证明。

manifest与报告不是数据库事务的一部分。诊断会比较读取前后的manifest文本；活动运行仍可能在跨文件读取间变化，因此该输出是观察快照，不是事务一致性审计证书。未完成模型调用使用inflight_or_interrupted，不能单凭这一条认定进程已死、请求失败或远端没有执行。

## 统计口径

- 模型：按model_call_id关联开始与完成/失败事件，展示角色、已知Token与耗时，以及耗时最长的调用。repair_verification与普通generation分开标记。修订候选复制了父运行历史时，includes_parent_history为true，不能直接跨父子目录相加来统计总费用。
- Token：完整总量要求开始/完成ID一一匹配、没有重复或失败、事件顺序正确且所有用量为非负整数。等量但不匹配的事件也记null，避免错误账单。未知不以0代替；未配置价格表，不换算货币。
- 工具：SQLite dispatch结果优先于审计事件。已提交成功回执却缺少completed日志，不反转成失败；标durable_result_without_completion_event作提示。pending可能正在执行，也可能需要恢复。
- 恢复：receipt_replays表示复用已完成结果；recovery_reexecutions表示日志记录了pending操作恢复执行；extra_executions取实际attempts减一，包含恢复重执行。三者不是同一个计数，也不证明远程副作用exactly-once。
- 预算：展示已用、保存的上限与剩余；0预算按0处理，未知上限为null。发现计数与dispatch总数不一致或超过上限时标记异常。
- 引用：检查当前draft（优先）或报告中的引用ID、摘录和来源hash；显示问题行号，不导出正文。这是引用完整性，不是结论的语义支持，semantic_assessment保持not_evaluated。
- 修订：读取本地repair-state，显示候选接受/回退原因和轮次。验证开始但没有持久化结果时，提示verification_outcome_uncertain，不自动重发付费请求。

## 常见定位

| 诊断代码 | 含义与后续检查 |
|---|---|
| model_inflight_or_interrupted | 检查任务是否仍在运行，不能根据缺失结束事件推断零费用。 |
| model_ambiguous_events | ID、重复事件或顺序异常；不能给出完整Token总量。 |
| tool_pending_or_interrupted | 查看运行状态与原权限策略；未知副作用不可盲目重试。 |
| durable_result_replayed | 恢复复用了回执，属于信息项。 |
| recovery_operation_reexecuted | pending操作发生恢复执行，结合attempts与幂等策略检查。 |
| durable_result_without_completion_event | 回执比日志更早提交；可能是已覆盖的进程终止窗口，不代表工具失败。 |
| tool_failure | 保留标准error_code，例如timeout、invalid_arguments、http_429；不导出供应商错误正文。 |
| report_citation_integrity_gap | 检查具体行的引用编号与证据来源；无需重新生成才能定位。 |
| repair_candidate_rejected | 复核候选的独立验证结果与回退原因；不是执行工具失败。 |

诊断文件采用字段白名单：不输出模型prompt、工具参数、返回正文、证据摘录或报告正文；保留调用ID、时间、模型名和错误类别等元数据。它不能证明源资料或元数据本身完全不含敏感信息，也不代替原日志的访问控制。

退出码0表示诊断成功生成，即使发现问题；1表示源数据/格式/输出位置导致诊断未完成，不代表研究任务本身成败。发现问题应阅读findings，而非把退出码0视为研究成功。

## 验证

测试覆盖字段过滤、零预算、错配/重复/逆序模型事件、回执与日志缺口、pending调用、引用问题行定位、修订回退和独立输出。另使用已有真实故障恢复运行和已回退候选生成诊断，不为验证可观测性额外调用模型。
