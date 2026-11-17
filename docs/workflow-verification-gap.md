# 工作流评分结果落盘间隙

2026-11-17，在已有 [工作流 CLI 测试](workflow-cli-acceptance.md) 基础上增加两个实际进程强杀窗口。目的不是把所有故障都恢复到通过，而是验证：评分调用已经发生、结果尚未持久化时，即使还有额度，也不能自动再付费请求一次。

## 两个故障点

| 故障点 | 已有状态 | 缺少的状态 | 强杀时预算 |
| --- | --- | --- | --- |
| response | 固定供应商已构造响应，checks_started 已保存 | 用量/完成事件、验证结果文件 | 2 completed + 1 reserved；剩余3 |
| validated | 输出已通过结构校验，用量与完成事件已提交 | original-quality.json | 3 completed；剩余3 |

response 窗口位于测试供应商返回给审计包装器之前；它模拟响应已可用而应用尚未提交完成记录，不等于捕获了真实网络响应。validated 窗口位于正常 `assess` 返回之后、评分 JSON 保存之前。两个点只存在于 tests worker，不增加生产故障开关。

父进程只强杀本次创建的进程树，以已落盘的 marker 确认到达窗口，不凭退出码或耗时猜测注入成功。研究阶段使用真实 Deep Agents/LangGraph graph 与 SQLite checkpoint；评分、生成供应商为固定脚本，dotenv 与网络连接均禁用。

## 恢复检查

每个窗口强杀后，分别启动两个新 CLI 进程执行相同 workflow 的 `--resume`。都返回退出码3和 `verification_outcome_uncertain`，phase 保留 repair，人工审核 pending。

断言包括：

- source/candidate Run ID 不变，不另建付费分支。
- 预算的每条 reservation（含 call ID、状态和 usage）均不变，不退款、不重复 admission。
- 脚本供应商轨迹始终只有两次生成、一次评分；恢复没有新调用。
- 原稿与候选保留的回退报告哈希不变，没有凭空生成评分文件。
- 初始剩余3次额度，排除“只是没额度所以没调用”的假解释。

`completed` 模型调用事件只证明审计包装器完成，不包含可直接复用的验证结论。缺少评分文件时不能据此标成通过；reserved 调用也不能被当成远端没有执行。程序选择保守停下，代价是需要人工检查而不能完全自动恢复。

不要手动删除 checks_started、返还 reservation 或覆盖旧结果来让实验“恢复成功”。该场景的预期成功是安全停止，不能合并到“研究任务完成恢复率”。它也不提供跨系统 exactly-once 保证。

## 复现与结果

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_workflow_verification_gap.py tests/test_workflow_cli.py -q
```

本轮本地5项通过，耗时60.81秒，含两个强杀窗口和已有三个正常 CLI 场景。无模型/搜索 API 调用，没有更改生产恢复策略或原始评分。全量 CI 自动运行这些测试；JUnit 中分别记录两个窗口，临时脚本结果不是公开的真实模型故障数据。

与已有 [模型响应/checkpoint 间隙](model-response-gap.md) 的区别：后者验证研究生成步骤可能重执行或因预算停止；本实验验证应用评分结果不确定时必须拒绝自动重发。两者采用不同恢复语义，不能把框架 checkpoint 概括为所有阶段都可以安全 retry。
