# 从任务摘要查看具体引用缺口

后序技术选型对照中，写作 v2 达到3/3模型任务通过，但仍有未引用的推断，见 [原始观察](technical-selection-next-pilot.md)。覆盖率门槛0.6允许部分事实未覆盖，因此“通过门槛”不能被展示成“全部关键结论已经证实”。

已有 `verification.markdown` 可以逐行展示引用关系；本次复用这个渲染器接入任务评测，不修改门槛、提示词或原始评分。

## 新评测

引用评分通过严格校验后，除 citation-quality.json 外，任务评测自动生成 citation-quality.md。任务 CLI 和报告写作对照的各组摘要会链接该页。未成功完成的批次不把未接受标签显示成有效评分详情。

定位页展示所有被模型判为事实的行及其引用关系，包含单元 ID、行号、原文、covered、conflict 和每条链接的 verdict/reason；同时单列被模型排除为非事实的行，便于复核是否错误排除了重要结论。它不自动识别哪些句子“最关键”，也不因标记推断或建议就免除其中事实前提的证据要求。

## 历史结果：零 API 生成独立视图

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval --summarize runs/technical-selection-next/4af1461cccc148d6bb6039c687ee66d1/execution/v2/result.json --with-quality --dataset evals/technical-selection-v1.json --output runs/quality-gap-views
```

`--with-quality` 只能与 `--summarize` 使用。必须提供与原评测相同的任务集；默认任务集仍是 research-tasks-v1，因此技术选型示例显式指定 dataset。没有凭证读取、模型调用或搜索。

此入口先复用人工复核包的任务/资料/快照检查，按原 `load_run` 的 draft.md 优先规则还原实际送评正文，再检查 `quality_input_sha256`。随后将已保存 factual、verdict、reason 标签放回原确定性校验器，核对单元、正文、引用 ID、覆盖率、汇总指标和每任务指标的一致性；不会让模型重新判一次。

结果不完整、评分输入缺失、实际送评正文改变、评分行错配、派生覆盖率不一致或文件缺失，都在输出目录创建前拒绝。原有不带 `--with-quality` 的摘要功能不变，仍可显示评分无效的批次。

新目录包含 summary.md、citation-quality.md 和 provenance.json；provenance 记录原结果与评分文件哈希、输入指纹、零 API 和 `scoring_recomputed=false`。这是对已保存标签的结构/一致性校验，不是新的语义评分。原文件、分数和人工标签不变。

哈希只绑定当前读取到的文件，不能证明标签确实来自某个模型或未被一致地改写；该视图也不是活动运行的跨文件事务快照，应对停止的评测使用。正式人工审核仍走 [人工复核入口](technical-selection-human-review.md)。

## 本轮实际结果

2026-11-17，已为原写作 v2 的三题结果生成视图：

`runs/quality-gap-views/e67e9cf6ec134d45bf6116f3110143e9/summary.md`

摘要仍按原记录显示3题模型门槛通过，最终验收 pending；详情保留 `external_effect_unknown:U6` 的未覆盖推断，以及 PostgreSQL 题标题/判定/建议中的缺口。没有删除句子、降低门槛、生成修订版或将缺口换算成人工标签。

本轮零 API。回归覆盖“门槛通过但有未覆盖行”的展示、原文件不改、模型/凭证入口禁用，以及正文、评分文本、指标和输入绑定错配拒绝。固定测试标签只验证接线，不代表语义准确率。
