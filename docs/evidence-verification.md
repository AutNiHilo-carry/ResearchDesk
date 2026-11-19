# Evidence Verification 与参考标签评测（Phase 3 首版）

## 目标与能力边界
验证实际报告表述是否由引用的摘录支持，而不是只验证引用编号或摘录存在。
这是模型辅助的语义支持检查，不是事实认证，也不证明来源可信、资料完整或结论最终正确。
验证器独立于研究 Agent，无工具权限，不改写原报告，不自动批准结论。

## 用法
在项目根目录、配置好 DEEPSEEK_API_KEY 后执行（不需要 Tavily Key）：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.quality verify 9e359ea792374eed84afb6c74b4569b9
.\.venv\Scripts\python.exe -m researchdesk.quality benchmark
```

verify 结果在原 run/verification/<check_id>/ 下，benchmark 在 runs/evaluations/<check_id>/。
均保存 result.json 和 summary.md。支持 --judge-model、--output、--max-units、--max-input-chars。
benchmark 支持 --dataset 和 --minimum-accuracy。

## 输入与完整性
优先使用 draft.md（原报告正文），不将自动附加的 Evidence Ledger 算作报告事实。
若无 draft.md，则从 report.md 移除应用生成的附录。
逐个非空 Markdown 行作为评估单元，标题也交给分类器，避免标题中的事实被漏算。
代码围栏和纯表格分隔行不评估；一行多个事实必须全部被一条引用支持才标 supported。
这只是明确、可重复的第一版粒度，不等于完整的原子 claim 抽取。
换行方式会影响分数，不能直接跨不同排版比较。

新版运行从应用 SQLite 只读获取资料/证据，旧版运行支持读取 JSONL。
确定性完整性检查包含：引用存在、source 存在、逐字摘录匹配、
来源位置匹配、source 内容哈希/ID、evidence 内容 ID。
不通过者强制为 invalid_evidence，模型不能覆盖。
评估时锁定原 run，防止同时恢复写入。原文件内容不会改写。

## 模型判断
每条事实行与每个唯一引用的配对分别标：
supported / partial / unsupported / contradicted / insufficient。
无事实的标题、纯问题、纯建议和显式场景假设可标 not_applicable。
建议中的事实前提、以及标为“推断”的事实仍要检查。
多份资料彼此冲突时标记 source_conflict，交人工判断，不自动选一方。

最多一次模型调用，默认 60 个单元、60,000 输入字符，超限直接拒绝，
不通过丢弃行或截断摘录偷偷提高分数。请求超时 90 秒、最大输出 8192 tokens、
无自动重试；显式禁用 thinking，temperature=0、JSON Output。
temperature=0 不保证输出完全确定。记录实际 model ID、参数、prompt 版本、
输入/报告哈希、dataset 哈希、token usage 和时间，便于复查。

输出 schema、单元集合、引用集合和标签一致性均严格校验。
空白 JSON、截断、漏行、额外引用和解析失败时记录 failed、metrics=null，
不接受部分输出形成漂亮分数，也不重复付费调用。

## 指标口径
所有实际报告指标是模型估计，factual 分类本身也可能错误。

- Citation Precision：事实单元中的 supported 引用关系数 / 全部引用关系数。
  每个（单元, evidence_id）只算一次，partial 不计完全支持。
- Evidence Coverage：至少有一条 supported 引用且无 contradicted 引用的事实单元数 /
  所有事实单元数。无引用事实会进入分母。
- 若多个 partial 引用合起来才能支持整行，首版仍不计 covered，这是保守口径。
- 来源冲突、invalid links 单独统计；分母为零显示 N/A，不返回 100%。
- status=complete 只表示评估完成，no_support_gap_detected 不等于结论已获认证。

## 评测集与对照
evals/evidence-v1.json 包含 15 个小型文字支持关系样例，覆盖直接支持、数值矛盾、
多结论只支持一部分、过度概括、条件丢失、否定、版本不匹配、无关摘录、
无引用、纯建议、来源冲突、未知引用、篡改摘录、联合 partial 和资料内指令。
参考标签由项目维护过程整理，目前尚未经过用户/独立人工复核，不是权威 benchmark。
标签不发送给 judge。

记录：
- 语义标签准确率、事实分类准确率、false_support_count。
- 对照基线：只要引用摘录通过完整性检查就预测 supported。
- 默认门槛：语义标签准确率 >= 0.85、事实分类全对、false_support_count=0。
  benchmark exit code 3 表示评测完成但未通过门槛；失败不调整标签迎合模型。
- benchmark 中普通 citation precision 是对那组故意带错误断言的评分，
  判断验证器好坏应看与参考标签比较的 semantic_link_accuracy。

离线测试用固定 judge 桩验证计算、错误处理和标签隔离，不声称证明真实模型语义能力。
真实 benchmark 必须调用模型得到单独结果。
这是一组证据验证子任务评测，不代表完整研究任务的 Task Success Rate，
也尚未评测总费用、端到端 latency 或多次运行方差。

## 后续
新增可选分句策略、v2 judge Prompt 与三组控制变量实验，详见 claim-granularity.md。
verify/benchmark 仍各最多一次模型调用；compare 最多三次。

先人工复核样例标签与真实报告中的争议项，再扩展研究任务集、不同模型/Prompt 对照、
原子 claim 粒度、来源可信度和更多质量/成本指标。

接口参考：https://api-docs.deepseek.com/guides/json_mode/

## 共享运行读取器的只读边界

`load_run` 供验证、人工复核和对照审计等入口共用。SQLite 的 mode=ro 仍可能在原数据库目录创建 WAL/SHM，因此读取器先将 application.sqlite3、已有的 -wal 和 -journal 复制到自动清理的私有临时目录，只在那里打开 SQLite。白名单不包含 .env、checkpoint 或其他运行数据；SHM 仅在临时目录重建，不用可能忽略已提交 WAL 的 immutable=1。

复制前逐文件计算哈希，核对副本与原始绑定，复制后及私有读取后再次检查原数据库和日志；观察到变化便拒绝返回资料。数据库输入若不是普通文件或解析到运行目录外也会拒绝。读取不修改原来源、证据、报告或评分，仍优先使用 SQLite，旧 JSONL 运行的读取方式保持兼容。

代价是每次读取都复制整个应用数据库并多次计算哈希，适合当前本地研究运行；未优化为大规模数据库读取。应使用已停止的运行：多文件复制和前后哈希不是对并发写入者的全局原子快照，也不覆盖报告／元数据的整体事务一致性。副本无法解析时拒绝，不能回退到可能过时的 JSONL 来掩盖 SQLite 故障。

离线回归包括：已关闭 WAL 数据库读取前后原目录逐文件一致、保留仍在 WAL 中的已提交来源更新、复制期间出现 WAL、私有读取期间出现 journal 时拒绝，以及旧 JSONL 兼容。这验证文件副作用与数据读取边界，不代表模型质量或新的恢复率。本轮不调用模型，也不改变引用判定规则。
