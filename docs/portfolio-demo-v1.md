# 实习展示：新案例与可复现演示

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

## 3–5分钟讲解路线

1. 从真实需求说明“为什么需要资料研究”，展示5题目录、官方来源与研究范围。
2. 展示一题从问题、证据摘录到报告的路径，点击每个关键结论对应的来源，解释指标来自模型而非人工金标。
3. 对照v1/v2的模型门槛、正文计数、应用门槛及Token；保留失败，不用一次分差证明策略更优。
4. 展示可携带交付包，运行 `python -m researchdesk.delivery verify <包目录>`，说明验包通过与研究正确性不同。
5. 用现有[强杀恢复证据](recovery-ci.md)讲一个故障窗口：框架提供checkpoint，项目负责回执、预算和恢复语义；未知操作不能凭猜测重复执行。

实际模型运行耗时不必硬塞进视频时长，可展示有来源绑定的已保存执行记录；清楚标注“历史运行回放”，不冒充现场实时完成。材料包含机器标签，不称为独立盲审。开发侧阅读和独立人工验收分别标识。

## 面试中需要本人讲清楚

- 为什么引用精度、证据覆盖率和业务rubric要分开看？
- 模型评分通过但正文超长，应用为什么仍拒绝？
- 为什么预算耗尽、未知副作用与已经发布的结果采取不同恢复策略？
- 相同模型与固定顺序对照能说明什么，不能说明什么？
- 哪些是Deep Agents／LangGraph primitive，哪些是自己的策略、存储及实验？

本页的准备和打包测试使用明确的注入评分，不会变成人工参考答案。真实执行成绩另行记录后再用于简历，不预填通过率或性能提升。
