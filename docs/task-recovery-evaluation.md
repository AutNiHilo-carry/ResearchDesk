# 任务层故障恢复评测

LangGraph提供图检查点与恢复primitive；ResearchDesk提供持久化回执、预算、证据幂等和恢复决策。本实验使用正常CLI、真实Deep Agents图和LangGraph SQLite checkpointer，观察所选故障窗口的结构性恢复，不能称为自研checkpoint。

## 故障矩阵

| 窗口 | 确认状态 | 预期恢复行为 |
|---|---|---|
| evidence | Evidence已提交，record_evidence的dispatch仍pending | 同一tool call重放，确定键去重，完成回执；预算不重新计同一次调用。 |
| receipt | record_evidence回执已提交，工具尚未返回，当前图步骤未完成 | 复用完成回执，不重新执行已完成操作。 |
| report | 报告已写出，run manifest仍running | 复用已完成图状态，重新导出并标记完成，不增加模型调用。 |

每次实验运行一个无故障对照和三个故障场景。选择同一研究问题和本地冻结资料；各场景独立生成，不要求不同运行输出逐字相同。模型和工具策略写入每个运行，恢复始终使用原始输入和策略。

故障钩子只装在专用fault_worker子进程中，不改变普通CLI行为。到达指定窗口后记录marker并暂停，控制进程只终止自己启动的进程树；不根据marker中的PID终止任意进程。停止后直接读取SQLite确认持久化状态与宣称窗口一致，未到达窗口或未确认状态不能伪装成成功注入。故障不是抛出一个异常或正常退出。

## 运行

```powershell
# 离线脚本模型；只验证运行、工具、持久化与恢复
.\.venv\Scripts\python.exe -m researchdesk.fault_eval --scripted
# 真实模型；同样禁止外部搜索，读取本地冻结资料
.\.venv\Scripts\python.exe -m researchdesk.fault_eval
```

默认使用research-tasks-v1.json的offline_selection。可指定--dataset、--task、--output和--timeout（每个子进程默认180秒，允许1到600秒）。恢复最多一次，再对已完成运行执行一次resume确认无额外执行。失败不会自动换题或重试到通过。模型图仍有现有步数上限，应用工具预算10、外部搜索/提取请求预算0、工具HTTP重试0；模型API请求仍会消耗Token。

脚本模式明确标注scripted_runtime_simulation，与real_model_local_documents分开保存。脚本模式的固定报告并不验证研究业务质量；不能把脚本运行结果当作真实模型表现。

## 成功口径与分母

恢复通过需要同时满足：运行完成、原输入/模型/权限/预算配置不变、原Source/Evidence逐条保留、旧回执与dispatch绑定不变、已完成操作不再执行、pending操作完成、预算不重置且不超限、无外部资料请求、报告引用与来源哈希有效、JSONL导出与SQLite一致。

恢复后再resume一次，要求数据库逻辑快照、报告和模型调用计数完全不变。report窗口另外要求首次恢复也不增加模型调用。对确定键的证据去重不等于语义相似证据去重；不承诺所有形式的重复研究都被消除。

Recovery Rate = 通过的已确认注入场景 / 已确认注入场景。没有确认注入时为null。另报故障到达率，以及全部计划故障场景通过率；未到达窗口计入后者失败，避免隐藏难以触发的失败。确认注入后即使恢复或验收异常，也保留在Recovery Rate分母中。

此处“通过”是结构性恢复，不是模型rubric任务成功率或引用语义正确率。没有测量报告质量恢复到与无故障对照一致，也没有估计生产故障分布或总体可靠性。外部搜索请求为0不表示真实模式没有模型网络调用。

## 产物与用量

runs/recovery-evaluations/<ID>/保存result.json、summary.md和冻结资料。各场景保留初始/恢复/再次恢复日志，确认的故障场景另存before.json、after.json和case.json，研究目录保留实际checkpoint、回执、证据与报告。

Token使用持久化模型事件汇总初次运行和恢复的所有模型轮次；已完成后的空操作恢复应无新增用量。未知用量保留null，不以0代替。工具重执行次数包含pending操作恢复重放，不能把合法幂等重放当作远程副作用exactly-once证明。

本轮不覆盖模型请求成功但检查点未提交、远程副作用执行后响应丢失、机器断电/磁盘损坏、网络分区或并发多工作进程。专用子进程的故障marker与快照用于实验审计，不是生产调度协议。


## 独立模型响应间隙实验

另有[model_gap_eval](model-response-gap.md)，用脚本模型覆盖on_llm_end记账完成但响应尚未返回图节点的窗口，分别检查补做消耗与预算停止；它不是本页三窗口矩阵的新增分母，也不覆盖真实供应方已经执行但客户端未知的所有网络窗口。
