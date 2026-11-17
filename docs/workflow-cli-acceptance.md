# 工作流命令行端到端验收

2026-11-17，本轮补齐独立子进程中的真实 CLI 接线检查。已有工作流单元测试会替换研究或修订执行函数；新增测试保持 `workflow.main → run_workflow → research CLI → Deep Agents/LangGraph → repair_run → diagnostics` 的正常调用链，只在生成模型和语义判分的供应商边界注入固定样例。

## 三个场景

| 场景 | 固定脚本行为 | 预期状态/退出码 | 恢复检查 |
| --- | --- | --- | --- |
| 引用门槛通过 | 2次生成回合、1次固定 supported 判分 | model_support_gate_passed / 0 | 完成态完全复用，无新供应商调用 |
| 修订预算不足 | 2次生成回合、1次固定 unsupported 判分，耗尽3次额度 | needs_review / 3 | 保留原稿，0轮修订，不自动生成到通过 |
| 零模型预算 | admission 在任何供应商调用前拒绝 | budget_exhausted / 4 | 同运行恢复继续停止，额度不重置 |

所有场景均使用本地固定资料、最多2次应用工具调用、0外部请求预算、0工具重试。生成脚本复用已有故障实验 fixture；它只是记录证据并输出带引用的固定句子。语义标签为明确的测试桩，不检验真实模型能否完成研究。

每个场景启动一次新命令，再启动独立进程执行 `--resume`。断言同一个 source/candidate Run ID、报告哈希和运行计数保留；完成态 workflow 状态全部一致。脚本供应商另记录调用轨迹，避免仅凭退出码猜测是否发生重复调用。还检查 checkpoint 存在、diagnostics 输出、人工审核始终 pending。

## 不接触真实服务

worker 位于 tests 中，不增加生产 CLI 的隐蔽模拟开关。它跳过 dotenv，给生成模型设置明显的非真实凭证，替换判分供应商；Python audit hook 拒绝 connect/DNS。模型预算和事件记录仍由应用实际代码执行，因此脚本调用也受同一额度约束。

这个检查不是操作系统级网络沙箱，也不是新一轮进程 kill 实验。进程强杀窗口已有 [恢复矩阵](recovery-ci.md) 和 [响应间隙实验](model-response-gap.md)；本轮只验证正常 CLI 调用、状态转移、预算停止与再次启动时的复用。

## 复现

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_workflow_cli.py -q
```

本地3项通过，耗时29.97秒；无模型或搜索 API 调用。全量 CI 自动收集该测试，JUnit 分别记录三个场景。不将临时测试运行目录当公开真实研究产物，也不为它另建质量排行榜。

退出码0只表示本次固定模型辅助证据门槛通过，3表示报告仍需复核，4表示预算耗尽。所有这些路径都不完成人工验收，不改变默认写作/评分策略，也不修改历史实验结果。
