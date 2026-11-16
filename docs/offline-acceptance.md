# 零API机制验收演示

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.acceptance_demo --output runs/offline-acceptance/demo-001
```

输出目录必须不存在，不指定output时自动创建唯一目录。命令不加载.env、不创建模型、不发HTTP请求，配置了真实密钥也不会使用。

## 查看结果

先打开输出目录中的summary.md，再看research/report.md、fixture-quality.json和result.json。演示包含七项机制检查：

1. 无权限搜索在适配器执行前拒绝。
2. 伪造摘录不能进入证据账本。
3. 关闭并重新打开真实SQLite库后，相同operation_id复用持久化回执，不重复写证据或增加计数。
4. 持久化工具调用额度用尽后，新operation_id被拒绝。
5. 不存在的证据引用不能通过编号检查。
6. 外部请求额度为0时，provider_timeout拒绝传输准入；没有HTTP适配器被调用。
7. 固定报告中的无引用事实仍保留在质量覆盖率分母，得到预期50%缺口。

前六项调用真实Ledger/ToolExecutor/ResearchTools逻辑，最后一项调用真实assess计算逻辑，但输入标签由ScriptedJudge固定提供。result状态mechanism_checks_passed只表示这七项预期行为符合；不是事实认证、模型准确率、真实任务成功或人工验收。

复用演示只关闭/重开应用SQLite，不kill进程、不运行LangGraph。真实进程故障注入请使用现有fault_eval的scripted模式并阅读task-recovery-evaluation.md，不能将本演示包装成新checkpoint实现。

本次演示产物位于runs/offline-acceptance/acceptance-20261116。运行目录不提交，代码与固定资料可在新目录复现。若中途失败，保留原目录供检查，不能覆盖它来隐藏失败。

阶段能力和未完成项见[阶段验收记录](status-20261116.md)。人工参考标签仍需真实审阅，本演示不会代填。
