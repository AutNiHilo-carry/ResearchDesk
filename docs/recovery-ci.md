# 进程强杀恢复的CI证据

普通离线演示只关闭/重开SQLite。真正进程中断使用现有fault_eval与fault_worker：固定本地资料、真实Deep Agents图和LangGraph SQLite checkpointer，模型换成固定脚本。它不是新的自研checkpoint。

## 覆盖范围

一个无故障对照，加三个强制终止窗口：证据已入库但dispatch待完成、回执已提交但工具尚未返回、报告已写出但运行状态尚未完成。

实验只终止自己创建的子进程树，不根据外部marker中的任意PID操作。必须在落盘状态确认达到目标窗口后才计为已注入；未到达窗口、恢复失败均不会伪装成成功。

检查恢复后原输入/策略、资料、证据、回执和预算保持一致，确定键证据不重复写，已完成操作不重复执行；pending的幂等证据操作允许重放。再次resume应为无操作。外部副作用exactly-once、机器断电、磁盘损坏、业务质量恢复均未覆盖。

## CI中的展示

现有全量pytest已运行test_scripted_full_cli_kill_resume_matrix。CI新增收集步骤，把该测试生成的matrix/result.json和summary.md复制到artifacts/recovery-matrix并归档，不重复运行一套故障实验。

收集必须找到唯一矩阵目录；测试改名或路径变化时会明确失败，不静默跳过。上游pytest失败不会因收集成功而通过。Artifact仅包含固定资料的汇总，不上传checkpoint、SQLite或其他历史runs。

在offline-verification-windows-py311归档中查看artifacts/recovery-matrix/result.json和summary.md。结构性Recovery Rate只以确认注入场景为分母，另报全部计划故障通过率和故障到达率；单组固定实验不能估计生产总体可靠性。

## 本地复现

```powershell
.\.venv\Scripts\python.exe -m researchdesk.fault_eval --scripted --timeout 120
```

不加载.env，不调用真实模型或Tavily。每次创建新实验ID，保留故障前后快照与恢复日志。若省略scripted会进入真实模型模式，含义与费用不同；本页命令始终显式指定scripted。

2026-11-16独立本地验证：对照通过，3/3窗口确认注入并通过结构性恢复，三个完成态再次恢复均无操作；CI收集逻辑也在已有pytest产物上本地验证通过。该结果不是任务成功率或人工语义验收。

本地原始资料在runs/recovery-ci-probe-20261116下，不随仓库公开。CI新增归档是否成功，以对应提交的[Actions运行](https://github.com/AutNiHilo-carry/ResearchDesk/actions/workflows/offline-ci.yml)为准。更完整的语义与限制见[任务层故障恢复评测](task-recovery-evaluation.md)。
