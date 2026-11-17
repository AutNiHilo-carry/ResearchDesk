# 长报告针对性评分复核

日期：2026-11-17。本轮使用 [技术选型首轮](technical-selection-pilot-v1.md) 已生成的实际报告正文，复用 [评分校准入口](task-grading-calibration.md)，没有重新生成报告。

## 提取与控制

新增 `researchdesk.task_grading_replay`，从既有报告策略试验导出案例。它检查原计划输入哈希、执行控制、资料哈希、题目后缀、请求构建与正文提取代码哈希、完成状态、模型和报告策略，以及运行目录是否属于对应试验分支。正文使用原 `load_run` 逻辑，排除 Evidence Ledger 附件，不删句、不改写、不截断；超出既有4000字符或批量30000字符限制即拒绝。

导出数据记录父计划、父结果、目标清单、报告文件、正文和运行清单的 SHA-256。哈希绑定本轮读取到的文件，不是对历史文件从未修改的独立证明。原试验没有单独保存完整请求正文，因此这里是依照已核对代码重建的输入，不是捕获的原始 API 载荷。

[目标清单](../evals/task-grading-replay-targets-v1.json) 选取两个原问题中的两个验收项，并分别保留写作策略 v1/v2 的对应报告，共4例：

- `wal_single_writer/single_writer`：报告是否明确说明单写者。
- `local_low_write/bounded`：是否避免声称未经测量的性能或全面优越性。

两份报告的这两项候选参考均为 true，由开发侧编写，不是独立人工标注。候选参考只留在本地计划，不发送给模型。

正文、原问题、资料和指定验收项保持原样，但批次从原来的3题各3项变为4例各1项，案例 ID 也变化。因此这是有意缩小范围的针对性复核，不能称为原始整批请求的完全重放。4例也没有负例，不能据此验证误判通过率。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_grading_replay runs/technical-selection-pilots/<原计划ID> --targets evals/task-grading-replay-targets-v1.json --output runs/task-grading-replays/<新目录>
.\.venv\Scripts\python.exe -m researchdesk.task_grading_calibration prepare runs/task-grading-replays/<新目录>/dataset.json --output runs/task-grading-replays/<新目录>/calibration --model deepseek-flash
.\.venv\Scripts\python.exe -m researchdesk.task_grading_calibration execute runs/task-grading-replays/<新目录>/calibration
```

前两步零 API，最后一步最多2次请求，评分提示词 v1→v2 各一次，无 SDK 重试。只发送公开技术摘记、自编题目、原模型报告正文与指定标准；不发送候选参考、运行路径或来源元数据。不搜索、不执行研究工具、不修订报告。单次执行、中断保留和控制漂移拒绝沿用校准入口。

## 本轮观察

父计划：`runs/technical-selection-pilots/7f88e78851ba4fe39cab347c1780d93a/`。
导出及新计划：`runs/task-grading-replays/20261117-targeted-v1/`。这些原始产物不提交。

| 评分提示词 | 逐字引文合同 | 候选标签一致数 | 返回 Token |
| --- | --- | --- | --- |
| v1 | 通过 | 4/4 | 3478 |
| v2 | 通过 | 4/4 | 3703 |

实际2次 SDK 请求，共7181个已返回 Token，金额未测量。独立人工审核 pending，未晋升候选提示词。

两版均从报告正文取到了逐字引文，也都接受了 bounded 项。其中旧版 v1 这次接受了先前被它否定的写作策略 v2 报告中的限制说明。由于批次/标准数变化且只有单次观察，无法区分上下文影响与随机波动，不能断言已找到根因，或宣称候选 v2 修好了旧错误。

本轮不修改旧报告、不覆盖旧分数、不把原任务成功率重新算成通过，不把本次业务验收项复核当成完整引用质量评分。默认业务评分提示词保持 v1。

## 验证与下一步

离线测试覆盖正文保持、附件排除、候选参考不入请求、错误来源、题目/模型/策略/资料/提取代码漂移、超限拒绝和不可覆盖导出。测试用合成运行目录，不依赖本机历史 runs。

下一步应冻结完整原批次，加入已有负例校准，并以同一批次做有限重复来检查判定稳定性；预先约定预算和比较方式。新实验仍应保留失败与未知值，避免只展示一次通过样本。
