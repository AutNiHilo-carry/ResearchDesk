# Checkpoint / Resume 与 Recovery Policy

## 能力归属
LangGraph 的 SqliteSaver 保存 graph 状态、消息和待执行节点。
ResearchDesk 提供 run 身份、恢复决策、持久化工具回执、证据去重、预算连续性、
导出修复和进程互斥。没有自研 checkpoint runtime。

## 使用

新运行自动启用持久化，启动时打印 Run ID。进程中断后在项目目录执行：

```powershell
.\.venv\Scripts\researchdesk.exe --resume <32位RunID>
```

如原先用了 --output，恢复时使用同一个 --output。恢复不能更换问题、资料、
模型名称或策略参数，均读取原 run manifest。Key 仍来自本地环境，不存入 manifest。
模型名称固定并不保证服务商实际模型权重不变。
旧版无 format_version=2 的运行和离线 --demo 不能恢复。

## 文件与一致性

- checkpoints.sqlite3：框架 checkpoint；使用相同 thread_id 和 durability="sync"。
- application.sqlite3：ResearchDesk 的资料、证据、回执、尝试数、调用预算、外部请求预算与审计事件。
  使用 SQLite WAL、synchronous=FULL 和事务。
- sources/evidence/tool_events.jsonl：可检查的导出副本，不是恢复事实来源。
  恢复时从应用数据库重建，修复进程中断导致的缺失或半行。
- run.json、input.json、draft.md、report.md 使用临时文件 + 原子替换。
- run.lock：操作系统文件锁；两个进程不能同时恢复，进程死亡后锁自动释放。

两个数据库不构成分布式事务。靠稳定操作 ID、回执、记录去重协调它们之间的窗口，
不能将它表述成跨数据库 exactly-once。

## 恢复语义

| 中断位置 | 行为 |
|---|---|
| input 已准备，框架还没有首个 checkpoint | 读取已保存 input，开始执行 |
| graph 中间 checkpoint | invoke(None)，继续原 thread 的待执行节点 |
| 工具成功回执已保存，graph 尚未保存工具结果 | 相同 tool_call ID 返回回执，不再次执行、也不再次扣调用预算 |
| 资料/证据已落库，工具回执未保存 | 只读请求按剩余尝试预算重做；证据通过稳定内容 ID 去重 |
| 操作状态不确定，属于未知副作用工具 | 拒绝自动重做，要求人工对账 |
| graph 已结束但报告/manifest 未完成 | 从最终 checkpoint 重新导出报告，不再调用模型 |
| run 已完成且报告存在 | 直接返回已有报告 |

同一操作 ID 搭配不同工具/参数会拒绝，不能串用旧回执。
相同参数、不同操作 ID 的成功搜索/读取可复用持久化资料快照。
失败回执在原操作 ID 下也会复用，不通过重启绕过失败或重试上限。

## 预算和时间

逻辑调用次数、外部请求次数、单操作尝试数持久化，恢复不重置。
网络尝试在发送前扣预算，因此进程中断后即使不知道是否发出，也保守计入。
恢复同一个已完成操作不额外扣调用预算。
工具准入期限是每个活跃进程的时间窗，恢复时重新开始；它不是累计任务时长硬预算。
模型 token、费用和任务累计墙钟时间目前没有硬预算。

## 故障测试

tests/recovery_worker.py 使用真实 Deep Agents graph 和 SqliteSaver，
通过脚本模型和 MockTransport 排除网络/模型随机性，不调用付费 API。
父测试进程等待指定窗口，再终止子 Python 进程，重新启动恢复：

1. search 成功回执落库，graph 工具结果 checkpoint 尚未落库。
2. evidence 落库，工具回执和 graph 结果尚未落库。

断言 provider 调用仍只有一次、资料/证据各一条、预算不重置、报告能生成，
再次读取完成 checkpoint 不新增模型/网络调用。
这证明受测进程中断窗口的恢复行为，不等于真实服务商故障或机器断电验证。

## 限制
外部请求成功后、回执提交前被杀，仍可能重复只读调用和产生费用；
不声称避免一切重复搜索。预算限制重放次数。
当前只安全恢复本项目的只读工具和幂等证据记录。
未知外部写操作需要服务端幂等键或对账策略，尚未支持。
SQLite 适合本地单运行恢复，不代表多机调度或生产规模能力。
checkpoint 内可能保存完整资料及模型状态，应作为本地私有运行数据管理。

接口参考：
- https://docs.langchain.com/oss/python/langgraph/durable-execution
- https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-sqlite

