# ResearchDesk

[![Offline verification](https://github.com/AutNiHilo-carry/ResearchDesk/actions/workflows/offline-ci.yml/badge.svg)](https://github.com/AutNiHilo-carry/ResearchDesk/actions/workflows/offline-ci.yml)

面向开发者技术研究与选型的长任务 Agent，重点是证据可追溯、工具受控、故障恢复与可评测性。基于 Python、Deep Agents 和 LangGraph；默认一个 Lead Research Agent。

用户提出问题 → 制定计划 → 搜索/读取资料 → 记录证据 → 比较方案 → 生成带引用报告 → 检查、有限修订或转待复核。

**当前处于 Phase 3，工程机制已有验证，研究质量与独立人工标签仍待验收。** 详见 [2026-11-19 阶段交付记录](docs/status-20261119.md)。完整说明集中在 [文档导航](docs/index.md)，方向以 [项目约束](docs/direction.md) 为准。

## 核心工程贡献

Deep Agents / LangGraph 提供 runtime、checkpoint、context 和 tool calling 等 primitive。本项目在这些能力之上设计应用策略，边界如下：

| 能力 | ResearchDesk 实现与验证 | 边界 |
| --- | --- | --- |
| Evidence Ledger | 保存 claim、source、原文摘录、位置、时间和内容 ID，验证引用完整性 | 原文存在不等于结论被语义支持 |
| Tool Policy | 参数、权限、超时、请求预算、有限重试、持久化回执与结果校验 | 静态权限；不承诺任意副作用 exactly-once |
| Recovery Policy | 复用 checkpoint 和已完成回执；强杀实验检查预算和状态不变量 | 部分模型步骤可能重执行；评分结果不确定时停止 |
| Evaluation / Observability | 任务 rubric、引用指标、故障注入、运行诊断与人工复核材料 | 模型评分会失败或波动；测试数不是研究准确率 |

## 零 API 快速体验

已验证 Windows / Python 3.11。在 PowerShell 中运行：

```powershell
git clone https://github.com/AutNiHilo-carry/ResearchDesk.git
cd ResearchDesk
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m researchdesk.acceptance_demo
```

打开命令打印的 `summary.md`，查看权限、摘录、预算、回执复用和引用缺口的七项检查。无需 `.env` 或 API Key；安装依赖需要网络。演示使用固定资料和固定标签，不验证真实研究质量。见 [快速开始](docs/quickstart.md) 和 [演示合同](docs/offline-acceptance.md)。

## 真实研究与恢复

真实研究调用 DeepSeek / Tavily，会产生费用；先按快速开始配置本地 `.env`。doctor 只检查配置存在性，不验证密钥有效性。

```powershell
Copy-Item .env.example .env
.\.venv\Scripts\researchdesk.exe --doctor
.\.venv\Scripts\python.exe -m researchdesk.workflow "研究问题" --local-only --document ./notes.md --max-model-calls 12 --max-provider-requests 0 --tool-retries 0
.\.venv\Scripts\python.exe -m researchdesk.workflow --resume runs/workflows/<工作流ID>
```

`notes.md` 需自行准备；此示例只读取本地资料。网页研究及工具预算见 [受检工作流](docs/checked-workflow.md)。恢复使用原计划，不能通过替换模型、资料或额度绕过绑定。请求上限不是 Token 或金额上限。

| 退出码 | 工作流含义 |
| --- | --- |
| 0 | 本次模型辅助证据门槛通过，人工审核仍 pending |
| 3 | 需要复核，或评分结果不确定而拒绝自动重发 |
| 4 | 模型请求额度耗尽 |

详细错误应查看状态、摘要和诊断；其他错误码不在此简表中概括。

## 当前验证证据

- [最近一次代码验收](https://github.com/AutNiHilo-carry/ResearchDesk/actions/runs/44551585768)：775项离线测试、固定资料演示与源码目录外的 wheel 安装检查通过（2026-11-19，代码快照 `430f568`）。
- [恢复矩阵](docs/recovery-ci.md)、[模型响应/checkpoint 间隙](docs/model-response-gap.md)、[评分结果落盘间隙](docs/workflow-verification-gap.md)：不同窗口采用不同恢复语义，安全停止不等于研究任务完成。
- [技术选型任务集](docs/technical-selection-eval.md)与[真实模型首轮结果](docs/technical-selection-pilot-v1.md)：生成流程可运行，评分有失败和争议，未证明候选策略更优。
- [模型评分与验收边界](docs/model-assessment-boundary.md)、[人工复核材料](docs/technical-selection-human-review.md)：评分有效、模型门槛和最终验收分别记录，人工标签仍待填写。
- [计数合同两题对照](docs/counting-contract-pilot.md)、[正文上限修订](docs/body-limit-live-repair.md)：保留应用验收失败与修订拒绝结果；[8组事实性标签材料](docs/abstention-label-review.md)仍待实际审阅。

CI 不注入模型或搜索密钥。JUnit、固定机制结果和 wheel 按白名单归档，保留7天；依赖安装与构建需要网络。复现见 [CI 说明](docs/offline-ci.md) 和 [wheel 安装检查](docs/wheel-installation.md)。

## 深入阅读

| 主题 | 入口 |
| --- | --- |
| 工具与预算 | [工具控制](docs/tool-policy.md)、[模型调用预算](docs/model-call-budget.md)、[尾部验证额度](docs/tail-verification-budget.md) |
| 证据与来源 | [语义验证](docs/evidence-verification.md)、[来源审计](docs/source-audit.md)、[单轮修订](docs/report-repair.md) |
| 评测与复核 | [任务评测](docs/task-evaluation.md)、[评分稳定性](docs/task-grading-stability.md)、[人工审核协议](docs/human-review.md) |
| 可观测性与实验 | [运行诊断](docs/run-diagnostics.md)、[Context 对照](docs/context-experiment.md)、[全部文档](docs/index.md) |

默认搜索上下文 full、报告写作 v1、业务评分提示词 v1。其他候选策略显式启用，不根据更高的一次分数自动晋升。Phase 4 的进一步 Context/SubAgent 实验和 Phase 5 的 Skill Lifecycle 不用于替代当前质量验收。

历史模型输出、网页快照和待审表保留在本地 `runs/`，不随仓库发布；文档中的 runs 路径不是公开下载链接。当前也不承诺完整 Token/总时限预算、网页权威性认证、任意外部写操作恢复或普遍研究质量优势。
