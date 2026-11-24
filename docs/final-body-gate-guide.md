# 显式最终正文发布检查

2026-11-24，补齐[草稿检查实验](draft-inspection-pilot-result.md)中“检查稿与最终稿不同”的缺口。新增独立开关`--final-body-limit`，在Ledger发布report.md之前重新检查实际最终正文。**默认关闭，原草稿工具仍只是反馈；本轮零真实API，尚未证明研究质量改善。**

## 使用及语义

```powershell
python -m researchdesk "研究问题" --document <资料.md> --local-only --report-policy v2 --draft-inspection-limit 200 --final-body-limit 200 --max-model-calls 5 --max-tool-calls 10 --max-provider-requests 0
```

这是新live运行的启用示例，会调用配置的模型，本轮没有执行。final-body-limit可以独立使用，不要求Agent调用inspect_report；同时启用时建议采用同一上限，两者仍是分别冻结的合同。非法范围、demo或resume替换选项在凭证加载前拒绝。

该应用检查不是Agent工具，不消耗工具额度，不调用模型／搜索，不增加可用预算。Agent仍可修改预检稿，但实际最终稿必须满足启用的本地门槛才能发布。启用时，系统提示加入最终门槛与计数说明；原v1/v2写作和默认行为不改。

只检查body-length-v1及引用结构／完整性：正文非空、计数合规、引用有效且对应保存摘录和来源。使用现有inspect逻辑，代码围栏、畸形正文及过大检查输入不静默省略。逐行上限60、输入／反馈字符预算沿用已有检查；长度未超限不保证其他结构条件也通过。

**不判断摘录是否支持整句，不检查业务完整性，不替代引用评分、任务验收或独立人工审核。** semantic_support和business_requirements均not_assessed。它关闭最终长度／引用结构的发布窗口，不解决所有证据质量问题。

## 发布顺序与失败

Ledger先保留候选draft.md，再按既有规则规范化引用，随后生成final-body-check.json；门槛通过后才继续发布report.md。检查使用实际规范化后的正文，receipt中body_sha256绑定这份正文；若引用格式本身不合法，也保留失败检查。规范化失败时draft可能仍是原始候选，应以receipt及既有规范化附件解释实际检查对象。

回执记录版本、冻结策略、正文合同、引用结构、最后一次成功预检的正文hash、与预检是否相同、api_requests=0。没有预检时相关字段null，不会凭空宣称检查过。即使matches_last_preflight=false，只要实际最终检查通过，仍可发布；不需要让模型再付费调用工具确认同一正文。

失败抛出FinalBodyRejected，CLI返回1并保存failed状态、候选和检查记录，不发布report.md、不自动补轮／重试。默认未启用时仍保留原save_report行为；不会给旧报告加门槛或重写原成绩。

回执status=passed仅表示本地检查完成，publication=not_claimed始终不变。它在真正写report.md之前产生，之后的磁盘故障仍可能导致发布失败，不能把回执通过当作已经发布。

## 冻结与恢复

新策略在run.json与SQLite记录中绑定版本、完整计数合同和提示hash。产生模型／工具调用后不能给原先未启用的运行补开策略，恢复不能移除或改变限额。Agent构建和CLI运行前绑定，Ledger保存前再次核对；启用后的有效生成提示仍受原report_policy的hash约束。

图已完成但发布失败时，沿用LangGraph现有完成检查点，恢复后对同一最终稿重新做本地检查并发布，不启动新图或新增模型请求。若最终稿仍超限，恢复仍拒绝，不能靠恢复刷新预算或换正文。没有承诺远程模型调用exactly-once。

已完成快捷恢复还要复算当前正文和结构、核对完整检查回执（包括预检匹配关系），并确认report.md的正文前缀与draft／回执一致。删除或替换策略、改草稿、篡改回执或已发布正文均拒绝。该检查不是完整交付manifest：附录和整包一致性仍使用delivery验包。

## 本轮验证

14项新增测试与草稿检查／Agent既有测试共34项通过，均为固定数据或脚本模型，没有真实API调用。覆盖最终稿不同于预检稿时重新计数、超长／未知引用／围栏／畸形引用拒绝发布且候选留存、无额外工具请求、完成态篡改拒绝、默认旧行为不变及非法CLI选项在凭证前拒绝。

故障注入使用真实LangGraph SQLite checkpoint与脚本模型：首次模型完成、检查通过后，注入report.md写失败；当时receipt仍为publication=not_claimed。恢复复用已完成检查点，发布成功，累计model_calls保持1；再次完成态恢复不构图。这是写异常注入，不冒充真实进程强杀或新增远程Exactly-once保证。

另禁用网络，对前一轮4份保存最终报告只读回放：

| 原组／任务 | 字符数 | 新本地门槛回放 | 原应用门槛 |
| --- | --- | --- | --- |
| off／快照 | 154 | passed | failed |
| off／外键 | 262 | failed | failed |
| on／快照 | 178 | passed | passed |
| on／外键 | 138 | passed | passed |

快照off的本地门槛通过、原应用仍失败，说明本地检查没有冒充语义验收。回放产物另存`runs/final-body-gate-replays/20261124-a/result.json`，原实验逐文件hash前后相同，原门槛不改，未重新发布旧报告。

当前仍为Phase 3；草稿检查和最终门槛均显式启用，不根据机制测试或保存报告回放自动晋升默认。下一次真实实验应分别声明新增最终门槛后的成功／拒绝及成本，不能将“更严格拒绝”直接当作生成质量提升。
