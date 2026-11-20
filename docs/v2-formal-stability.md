# v2 正式命令复放与跨报告标签差异

2026-11-20，通过修复后的正式 CLI 对原 v2 两题完整评分输入重复2次，新增 **2次 SDK 请求、7572返回 Token**，无搜索、报告生成或重试。两轮新标签一致，但均与历史 v2 评分有3项字段差异：2项事实性变化和1项引用变化。报告原文未变，不把指标上升当作报告质量提升。

## 固定范围与正式入口

原目录为 `runs/generation-contract-plans/5746f5fdbf8547d3abe02f453d26ffd4/execution/v2/`。完整批次包含 version_scope 与 wal_default_claim，共17行、9条引用证据。沿用原生成合同 body-length-v1，重新核对其后缀与原问题绑定，保留整份报告、标题、引用和任务顺序。

请求前冻结完整输入、资料、源码、原目录逐文件哈希及已有[原 v1 两轮复放](original-batch-stability.md)结果哈希。固定 deepseek-flash、report-quote-v2、temperature=0、max_tokens=8192、timeout=90、max_retries=0、JSON输出、thinking=disabled，预定2轮，失败不补轮。

正式命令已真实执行（再次运行会产生新费用，不是零 API 核验命令）：

```powershell
python -m researchdesk.judge_stability runs/generation-contract-plans/5746f5fdbf8547d3abe02f453d26ffd4/execution/v2 --dataset evals/technical-selection-v1.json --repeats 2 --output runs/v2-formal-stability/13d4b90822d54de3ac288c2570726cea/executions
```

本地计划位于 `runs/v2-formal-stability/13d4b90822d54de3ac288c2570726cea/`，正式输出在 `executions/1e150d37df2a429a98a96b54e40417aa/`。命令成功退出只表示预定两轮完成，不是任务或人工验收通过。原始资料与输出不随仓库发布。

## 同输入的新轮次一致，但历史标签发生变化

两次新评分的17条 factual、9条引用 verdict 均一致。逐项对照历史结果，两轮都出现以下变化：

| 单元／字段 | 历史值 | 新轮次1 | 新轮次2 |
| --- | --- | --- | --- |
| version_scope:U7 factual | true | false | false |
| version_scope:U7 引用 E8074739276dff7a9 | supported | not_applicable | not_applicable |
| wal_default_claim:U7 factual | true | false | false |

版本题 U7 原文为 `所据摘记的版本范围仅为 18，未核对 17 版对应页面。[E8074739276dff7a9]`。它混合资料范围与未核对状态，新评分将整行排除为研究范围说明。

WAL题 U7 原文为 `建议：保留 WAL 的读写并行能力，通过串行化写入或使用专门处理队列的方式应对多写者需求；方案选型尚待验证。`。新评分将其排除为纯建议；其是否包含可核查的能力前提需要复核，不能只因开头写“建议”便确认排除正确。

| 引用指标 | 历史 v2 | 两次新评分各自 |
| --- | --- | --- |
| 版本题事实行／已覆盖行 | 4／4 | 3／3 |
| 版本题精度／覆盖率 | 1.0／1.0 | 1.0／1.0 |
| WAL题事实行／已覆盖行 | 5／4 | 4／4 |
| WAL题精度／覆盖率 | 1.0／0.8 | 1.0／1.0 |

WAL覆盖率上升来自未覆盖行被排除出分母，没有新的研究、证据或报告改进。旧报告仍超长，旧应用验收失败状态保持不变；本次未重新调用业务评分器，也没有颁发新的 Task Success 或 final_acceptance。

两条排除变化及完整报告、来源、旧标签和新标签另存为 `cross-report-analysis/exclusion-review.json`，reviewer、时间、human_factual 与理由均未填写。该材料展示了机器判断，且属于事后选例，不是独立盲审或人工 gold。

## 与已有 v1 的5组声明句对

复用[原先声明的候选句对](abstention-label-review.md)，不重新挑选更有利的句子。已有 v1 第1／2轮分别与新 v2 第1／2轮作离线比较；原单元 ID 与文字保留，仅新建评分包装及对应文件哈希。两份对照都得到5组中3组 factual 不同：

| 声明句对 | v1 两轮 | v2 两轮 |
| --- | --- | --- |
| pg_abstention：不能确认两版相同 | true | false |
| pg_unverified_and_advice：未核实与建议核对 | true | false |
| sqlite_version_limit：未固定版本的范围说明 | true | false |
| pg_lock_fact_control：冲突锁规则 | true | true |
| sqlite_default_control：默认日志模式 | true | true |

这些是候选近义句，尚未人工确认等价；v1／v2文字、格式、证据和整批上下文不同，执行时间也不同。不能把这3处跨报告差异直接归因于写作策略或上下文，更不能把5对比较乘以重复次数当作10个独立样本。accuracy 保持 null，不建立新的真值。

## 用量、核验与决定

| 新轮次 | 输入 Token | 输出 Token | 总 Token | 评分耗时 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2973 | 805 | 3778 | 6.015秒 |
| 2 | 2973 | 821 | 3794 | 3.422秒 |
| 合计 | 5946 | 1626 | 7572 | — |

用量完整返回，金额未测量。已有 v1 的2次请求／6529 Token 不重复计入本轮。离线核验包括：新正式输出与事前冻结输入／控制条件一致；保存标签确定性复算；两批汇总与历史逐项比较；现有 judge_consistency 对5组声明的文件哈希和文字锚点核验；原 v2 目录全部文件未变，已有 v1 结果及输入哈希未变。离线核验零 API。

本轮没有观察到两次新调用之间的差异，却观察到与历史同输入评分不同。模型别名与Prompt相同不证明后端快照相同，无法判断变化来自采样、后端或其他未记录因素；两次新评分一致也不能证明判定正确。

继续保留当前默认策略及全部历史标签，不挑更高覆盖率的一轮替换原成绩。优先核对混合句的事实性边界和被排除的事实前提；需要解释变化原因时，应事前定义单因素对照和上限，或获得真实人工参考。Phase 3 与人工验收仍未完成，本轮不追加请求。
