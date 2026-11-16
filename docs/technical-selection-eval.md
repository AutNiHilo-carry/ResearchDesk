# 技术研究与选型任务集 v1

ResearchDesk 的业务垂类是面向开发者的技术研究与选型。本任务集用数据库选型中的约束保留、错误推论和证据缺口，检查研究报告是否在资料范围内回答。它复用现有 `task_eval` 与 `report_eval`，不增加另一套 Agent 或评分引擎。

## 资料和范围

入口：[technical-selection-v1.json](../evals/technical-selection-v1.json)。包含8个任务、5份中文局部摘记，每个任务3项候选 rubric。

| 场景 | 候选验收重点 |
| --- | --- |
| 单机离线、低写入工具 | 按场景推荐并保留单写者限制 |
| 跨主机共享 WAL 文件 | 保留同机要求，区分应用服务器访问 |
| WAL 并发解释 | 读写并行不推出多个同时写者 |
| PostgreSQL 18 同行更新 | MVCC 不消除冲突写入的等待 |
| TPS / P99 承诺 | 资料没有实测就保留性能判断 |
| 外部扣款 exactly-once | 不把数据库内机制推广为外部副作用保证 |
| PostgreSQL 17 / 18 | 不把 18 版资料当成 17 版全部行为的证明 |
| SQLite 默认日志模式 | 区分默认方式、可选 WAL 和实际配置 |

摘记来自 [SQLite WAL](https://www.sqlite.org/wal.html)、[SQLite 隔离](https://www.sqlite.org/isolation.html)、[SQLite 使用场景](https://www.sqlite.org/whentouse.html)、[PostgreSQL 18 MVCC](https://www.postgresql.org/docs/18/mvcc-intro.html) 和 [PostgreSQL 18 显式锁](https://www.postgresql.org/docs/18/explicit-locking.html)，核对日期为2026-11-16。具体 URL、版本范围与摘记 SHA-256 存在 [来源清单](../evals/technical-selection-sources-v1.json)。

这些是项目编写的局部摘记，不是完整网页快照。SQLite 页面没有固定发布版本；PostgreSQL 链接固定为18版。哈希绑定本地摘记字节，不证明官方网站内容未变，也不证明摘记无遗漏。运行时引用本地摘记，不等于直接检索并验证了官方网站。

目前 rubric 是项目编写的候选口径，独立人工审核待完成。尤其“证据不足”只表示附带资料不足，不能标成“产品绝不支持”。不将这些 rubric 称为人工金标，不将结构测试通过称为报告质量通过。

## 零 API 准备

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_eval --dataset evals/technical-selection-v1.json --model deepseek-flash --limit 3 --max-model-calls 6 --output runs/technical-selection-plans
```

命令只冻结前3项任务、资料、模型名称、预算和现有 v1/v2 写作策略，不读取凭证或调用模型。若以后执行生成的计划目录，最多40次 SDK 请求；这不是本轮消耗。现有入口一次最多3项，不能把前3项结果声称为8项整体结果。

`report_eval --execute-plan <计划目录>` 会调用模型；`task_eval --dataset ...` 同样会实际调用模型，不是 dry-run。本轮未执行这两种付费操作。

默认任务集、评分门槛和默认写作策略不变。先用此小型闭集发现报告是否丢失条件，代价是覆盖面窄、资料经过人工筛选；它不测实时检索、网页冲突、工具超时或崩溃恢复，后者使用已有故障实验。

## 验证与下一轮

测试检查任务集可加载、资料与来源清单一一对应、哈希未漂移，以及全部8项任务可按3/3/2分组进入现有冻结计划。准备阶段禁止加载凭证或调用批量评测。未产生模型报告、业务成功率或与同类产品的优劣结论。

下一轮先审核摘记与候选 rubric，再用相同模型、资料和预算执行小规模对照，记录失败样例与模型评分，人工复核后再决定是否扩大任务集。版本更新应生成新的资料和清单，保留旧计划及结果。
