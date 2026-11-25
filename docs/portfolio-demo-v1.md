# 实习展示：新案例与可复现演示

新读者可直接从[3–5 分钟讲解路线](#五分钟讲解路线)开始。演示采用一份真实研究交付和一个可离线重现的发布失败，五题批次供进一步查看评分和局限。

[首轮真实执行结果](portfolio-cases-result-20261123.md)已记录：实际21次SDK请求；v1评分格式失败，v2模型与应用门槛通过1/3，人工审核pending。不是成功率提升证明。

失败讲解可结合[逐句引用诊断](portfolio-citation-diagnosis-20261123.md)，展示结论引用不完整、推断缺引用和混合句排除问题；开发诊断与待人工表分别保留。

首轮把已有可靠性机制应用到5个后端技术研究场景，执行前3题的固定v1→v2对照，后2题当时保留未执行。后续[独立批次已完成后两题](portfolio-reserved-result-20261123.md)，五题完整状态见[阶段汇总](status-20261123-quality.md)。任务与候选rubric由项目编写，资料是公开官方文档的局部中文转述，不是独立人工金标或独立held-out benchmark。

| 场景 | 研究问题 | 首轮安排 |
| --- | --- | --- |
| sqlite_live_backup | 持续写入的SQLite WAL库怎样备份，能否只复制主文件 | 执行v1/v2 |
| postgres_queue_claim | PostgreSQL 18队列表能否用SKIP LOCKED，锁与一致性边界是什么 | 执行v1/v2 |
| api_v1_retry_key | Stripe API v1返回500后如何选择幂等键，不能外推哪些保证 | 执行v1/v2 |
| postgres_read_snapshot | Read Committed事务中两次普通SELECT是否保证同一结果 | not_executed |
| sqlite_pool_foreignkeys | 连接池外键开关与事务内设置是否真的生效 | not_executed |

资料见evals/portfolio-cases-v1.json及portfolio-sources-v1.json，6份摘记逐文件绑定SHA256，核对日期2026-11-23。PostgreSQL限定18；SQLite未固定版本／编译配置；Stripe明确限定API v1，不能把同页API v2规则混入。未执行数据库压测、支付或外部Stripe API。

## 三步复现

项目环境已安装时，在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/portfolio_demo.py prepare
```

prepare零API、不加载凭证，会打印新的冻结计划目录。任务、顺序、语料、模型、预算、计数合同、源代码与演示脚本进入校验。实际模型默认为deepseek-flash；每题每组最多4次生成、每组最多2次批量评分，共最多28次SDK请求，无SDK自动重试。该上限不是Token或金额上限。

确认使用打印的计划目录，显式执行付费步骤：

```powershell
.\.venv\Scripts\python.exe scripts/portfolio_demo.py execute <冻结计划目录>
```

只有execute会加载本地.env并使用DEEPSEEK_API_KEY。生成向配置的DeepSeek接口发送问题与公开资料；业务评分另外包含rubric及报告，引用评分包含报告行和证据摘录。本轮本地资料模式，Tavily请求预算0。实验默认写作v1/v2固定顺序、同一body-length-v1计数合同、相同评分器及阈值；不自动修订或选择最好的一轮，不晋升默认策略。

已有execution目录（包括中断或评分失败）会在加载凭证前拒绝，不能用同一计划重复付费。需要重新整理已有结果时使用离线步骤：

```powershell
.\.venv\Scripts\python.exe scripts/portfolio_demo.py package <冻结计划目录> --output <新的展示目录>
```

package不重新生成或评分；只有完整评分的组才会导出可验证交付包，评分失败／缺失保持unknown／not_packaged，原报告与失败产物仍保留。展示首页列出全部5题，未执行的两题不进入通过率。原计划、源文档或脚本变化时拒绝，不手改冻结hash绕过验证。

execute返回0仅在两组应用门槛都通过时成立；存在未通过／未知返回3；操作错误返回1，参数错误返回2。package返回0仅代表离线整理完成，具体质量看首页与原记录。人工复核始终pending，未知Token不填0，金额未测。

## 五分钟讲解路线

1. **30 秒介绍项目。** ResearchDesk 接受开发者的技术研究或选型问题，用 Evidence Ledger 保存结论与出处。Deep Agents 和 LangGraph 提供运行时及检查点，ResearchDesk 实现工具控制、恢复决策和评测。
2. **60 秒讲真实交付。** 问题是持续写入 SQLite WAL 库时能否只复制主文件。沿着结论查看 WAL 与持久状态的证据摘记，再看 Online Backup API 和备份边界。历史运行交付了 189/200 字符的带引用报告，通过业务和引用的模型辅助门槛。 [WAL 证据摘记](../evals/fixtures/sqlite-wal-backup-portfolio-v1.md) · [真实运行记录](final-turn-pilot-20261124.md)
3. **60 秒讲失败和恢复。** 另一轮真实运行因最终稿达到 214/200 而拒绝发布。固定该候选与同一运行的 190 字符预检稿做离线对照，关闭回退会拒绝发布，开启后保留超长稿并交付已预检草稿。真实子进程测试在选择回执写完、报告写完两个窗口分别强杀进程，重启后完成发布，模型和工具调用次数未增加。 [发布与回退对照](final-body-gate-guide.md#同一次历史运行的发布对照) · [强杀测试](../tests/test_overflow_publication_kill.py)
4. **60 秒展示可复现证据。** 打开零 API 演示打印的 `summary.md`，展示权限拦截、证据验证与回执复用。恢复实验可提前生成回执，讲解时对照故障前后的调用计数。研究质量的独立人工验收仍待完成。 [演示检查项](offline-acceptance.md) · [恢复矩阵](recovery-ci.md)

仓库根目录安装依赖后，可运行以下命令。两类验证都使用固定资料且无外部 API 请求。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.acceptance_demo
.\.venv\Scripts\python.exe -m researchdesk.fault_eval --scripted --timeout 120
.\.venv\Scripts\python.exe -m pytest -q tests/test_overflow_publication_kill.py
```

公开仓库提供源码、证据摘记和实验记录。已有本地评测包时，可另演示 `python -m researchdesk.delivery verify <包目录>`；新克隆可直接运行上述零 API 验证。

2026-11-25 在 Windows 上复跑了以上路线。机制检查 7/7，恢复矩阵确认注入并恢复 3/3，回退发布的两项强杀测试通过。本地回执位于 `runs/portfolio-closeout/20261125-a/`。

## 作者复核发现了什么

189 字符的原报告通过了模型辅助门槛。项目作者逐项复核时发现，原报告写了主文件与 WAL 分离的风险，也提出复制时保留 WAL，但漏写了「WAL 属于数据库持久状态」。这个要求是业务验收的一部分，因此作者复核中三项通过两项。 [原运行与模型评分](final-turn-pilot-20261124.md)

未发布样稿只修订这一行，补写持久状态说明并沿用同一条摘录，正文增至 197/200 字符。作者确认新增陈述及其引用，其余未改行沿用前一次作者判断。离线复算得到三项业务要求通过，六处引用均获作者完整支持。本轮不调用外部 API；原报告与历史评分继续保留，样稿的独立人工复核仍待完成。

## 面试中需要本人讲清楚

- 为什么引用精度、证据覆盖率和业务rubric要分开看？
- 模型评分通过但正文超长，应用为什么仍拒绝？
- 为什么预算耗尽、未知副作用与已经发布的结果采取不同恢复策略？
- 相同模型与固定顺序对照能说明什么，不能说明什么？
- 哪些是Deep Agents／LangGraph primitive，哪些是自己的策略、存储及实验？

本页的准备和打包测试使用明确的注入评分，不会变成人工参考答案。真实执行成绩另行记录后再用于简历，不预填通过率或性能提升。
