# 按任务 ID 准备报告对照

原 `report_eval` 仅支持任务集前 N 项。技术选型有8题，但一次冻结最多3题，后面的并发、证据不足和版本场景只能另造任务文件才能选择。现增加可重复的 `--task-id`，复用原计划格式和执行器，不新建评测框架。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_eval --dataset evals/technical-selection-v1.json --task-id mvcc_write_contention --task-id performance_unknown --task-id external_effect_unknown --model deepseek-flash --max-model-calls 6 --output runs/technical-selection-next
```

此命令只准备，零 API，不加载凭证。三个任务按参数给定顺序冻结，资料仅保存它们实际使用的部分，原数据集及 rubric 不改写。完整原数据集仍参与输入绑定，所选题目和顺序也进入哈希。

## 选择规则

- 一次选择1至3个不同且存在于任务集的 ID，区分大小写，不做拼写修复或静默跳过。
- `--task-id` 与 `--limit` 互斥；未使用两者时仍默认前3题。原 `--limit 1` 等行为不变。
- 无效选择在输出目录创建前拒绝，不留下可执行计划。
- `--execute-plan` 只接受冻结目录，不接受 `--task-id`、预算或其他替换参数。

该变更不改变写作策略、评分提示词、门槛或人工审核状态。代码哈希变化仍会使旧计划拒绝执行，应创建新计划，不能修改旧 controls 绕过检查。

## 预算与实际状态

每题每组最多6次生成、固定写作 v1→v2，每组另最多2次批量评分。因此三题若执行，上限40次 SDK 请求；两题为28次，一题为16次。这是请求上限，不是金额或 Token 上限，失败不自动重试。

2026-11-17 已使用上述三题成功生成本地冻结计划，位于 `runs/technical-selection-next/` 下命令打印的唯一目录。本轮未执行，没有新增研究报告、业务通过率或质量结论。选题已用于开发，不能因为尚未调用模型就称为独立 held-out benchmark。

后续真实执行仍通过已有 `--execute-plan <新计划目录>`，会产生模型费用；原有证据、业务 rubric、评分失败保留和最终验收 pending 规则继续适用。任务含义与候选标签边界见 [技术选型任务集](technical-selection-eval.md)。

## 验证

离线测试覆盖后序题目选择、顺序/资料与哈希绑定、请求预算、默认前3项兼容、未知/重复/空/超量 ID 拒绝、参数互斥，以及无凭证准备。测试不代表这些技术场景已经通过真实模型或人工验收。

## 后续执行

入口实现时仅准备的三题，随后已按原冻结计划完成真实模型对照：21次 SDK 请求，v1模型判定2/3通过、v2为3/3，但后者仍有未覆盖推断和更高生成Token，默认保持 v1。见 [后序三题结果](technical-selection-next-pilot.md)。
