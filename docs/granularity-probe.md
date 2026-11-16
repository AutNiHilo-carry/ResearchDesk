# 冻结逐行与分句实验输入

```powershell
python -m researchdesk.granularity_probe RUN --body RUN/revisions/candidate.md --output NEW_PACKET.json
```

`granularity-probe-v1` 是零API的实验准备工具。输入为已完成、停止运行的目录和该目录内显式选择的正文文件；不会把被保留的原稿误当作被拒绝的候选。若选择report.md，文件内容会原样进入实验，包括附录，因此优先选择纯正文draft.md或revisions/candidate.md。

冻结包包含精确正文、来源快照、证据、v2评分器Prompt，以及逐行和现有clause-spans-v1两组完整输入及哈希。所有分句必须覆盖原文且不重叠、引用证据集合必须相同，两组预算均在准备时检查。围栏、无效证据、超预算直接拒绝，不截断内容。输出包含完整研究资料，仅供本地审计与后续显式授权实验，不应视为无敏感内容的元数据导出。

单位数、无引用单位数和候选引用绑定数仅为结构统计，不是事实数、质量分或准确率。状态固定prepared_unscored，metrics为null，人工审核pending。不复用行级标签填充分句标签，不自动发起请求，不改变默认拆分策略或旧分数。冻结包覆盖整个语料，未引用语料变动也会改变冻结哈希。

## 真实样本发现（2026-11-15）

包位置：`runs/report-policy-live/wal-v2-20261115/granularity-packet-v1.json`。

| 结构统计 | 逐行 | 现有分句 |
|---|---:|---:|
| 单位数 | 4 | 6 |
| 候选引用链接数 | 3 | 3 |
| 无引用单位数 | 3 | 5 |

4个分句为explicit_span_only、2个为original_line，无parent_final_candidate_pool。新增单位来自推断与来源说明的拆分；关键事实段U2完全未拆开，依旧绑定三个引用。

原因是现有保守条件规则命中“只有一个 WAL 文件”中的“只有”，输出conditional_scope_preserved。此处是在表达数量限制，未必构成需要整行保留的条件前提。直接运行现有分句评分不会修复这个结构瓶颈；当前未做新评分，也不能声称分句提高了质量。

下一步应在独立策略版本中区分数量约束与条件依赖，加入“只有一个”“只有当…才…”等对照，验证作用域不被破坏，再对冻结样本进行模型实验。不要删除保守条件保护，也不要直接改现有版本以重写历史实验含义。
