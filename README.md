# ResearchDesk

[![Offline verification](https://github.com/AutNiHilo-carry/ResearchDesk/actions/workflows/offline-ci.yml/badge.svg)](https://github.com/AutNiHilo-carry/ResearchDesk/actions/workflows/offline-ci.yml)

一个可恢复、可验证、可评测的长任务研究 Agent（当前处于 Phase 3，研究质量仍待稳定验收）。

Python + Deep Agents；必要时局部下沉 LangGraph。框架负责 runtime / primitive，
本项目聚焦业务策略、工具可靠性、恢复语义、证据与评测。

阶段能力、验证证据及未完成项见[当前阶段验收记录](docs/status-20261116.md)。

无需密钥可运行 `python -m researchdesk.acceptance_demo` 查看固定资料的机制验收演示，详见[零API验收说明](docs/offline-acceptance.md)。固定标签演示不代表真实研究质量。

## 当前实现

- Deep Agents 单 Lead Agent，DeepSeek 模型，Tavily 搜索 / 文档提取。
- 可显式导入本地 UTF-8 Markdown / 文本文档。
- 不可变 source snapshot、claim → source → snippet → location → retrieval time。
- 原文片段精确匹配，报告证据编号有效性检查，自动附加证据台账。
- 每次运行独立输出 sources.jsonl、evidence.jsonl、run.json、report.md。
- 离线演示和脚本模型集成测试均不消耗 API；它们不证明真实研究质量。
- 应用侧 Tool Policy：严格参数、工具权限、调用/外部请求预算、只读有限重试、同一 run 持久化缓存、结果校验与审计日志。
- LangGraph SQLite checkpoint + 应用侧持久化工具回执、预算、资料/证据恢复及进程锁。
- 独立的报告语义验证命令、固定参考标签样例和 exact-match 对照基线。

已有小型受控本地资料任务集，并开展少量真实网页研究及Context先导实验；仍缺少独立人工标注的网页任务集和泛化质量证据，来源可信度判定及未知外部写操作恢复待完成。
旧版运行无法恢复；新版 live 运行自动启用持久化，离线 demo 不提供 graph 恢复。
不将框架的工具调用、上下文管理等能力表述成自研。

## 离线持续集成

已添加[离线 CI 配置](.github/workflows/offline-ci.yml)，推送 main、提交 PR 或手动触发后运行全量测试与固定资料演示，并保存验收产物。不注入模型/搜索密钥。[首次远程 CI](https://github.com/AutNiHilo-carry/ResearchDesk/actions/runs/43837421928)已通过507项测试、离线演示和产物上传，详见[CI 说明](docs/offline-ci.md)。

进程强杀恢复已在全量测试中覆盖；CI同时归档该矩阵的固定资料结果，见[恢复验收证据](docs/recovery-ci.md)。

## 离线快速体验（PowerShell）

需 Python 3.11，先确认 `python --version`。以下流程不需要 `.env` 或 API Key；安装依赖需要网络。

```powershell
git clone https://github.com/AutNiHilo-carry/ResearchDesk.git
cd ResearchDesk
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m researchdesk.acceptance_demo
```

打开命令打印的 `summary.md`，查看权限、证据、预算、回执复用和质量缺口的七项机制检查。模型标签为固定样例，演示不验证真实研究质量；详见[快速开始](docs/quickstart.md)。安装耗时取决于网络和磁盘。

### 可选：真实研究

只有需要调用 DeepSeek / Tavily 时才复制并填写 `.env`，真实调用会产生费用。保留空白密钥模板，不提交实际密钥。

```powershell
Copy-Item .env.example .env
.\.venv\Scripts\researchdesk.exe --doctor
```

doctor只检查配置存在性，不验证Key有效性。带请求预算的完整研究入口见[受检工作流](docs/checked-workflow.md)。独立运行测试可用 `.\.venv\Scripts\python.exe -m pytest -q`。

本地历史运行、模型输出与待审阅表位于 `runs/`，默认不随仓库发布；文档中的此类路径是本地索引，不代表GitHub上存在对应文件。

## Tool Policy

默认 20 次应用工具调用、8 次外部请求（含重试）、单次 HTTP 阶段超时 30 秒、最多额外重试 1 次。
可使用 --max-tool-calls、--max-provider-requests、--tool-timeout、--tool-retries、--tool-deadline 调整。
tool_events.jsonl 记录调用、拒绝、失败、重试和缓存命中；run.json 保存本次策略参数。
详见 [docs/tool-policy.md](docs/tool-policy.md)，包括重试安全边界、预算定义和恢复限制。

## 中断恢复

启动时打印 Run ID；进程中断后使用同一个 output 目录：

```powershell
.\.venv\Scripts\researchdesk.exe --resume <32位RunID>
```

恢复使用原问题、模型和预算，已完成操作复用回执，未知副作用不自动重放。
见 [docs/recovery-policy.md](docs/recovery-policy.md) 的恢复窗口、预算语义和故障测试。

## 限制

引用检查只证明“编号存在、摘录存在于保存的资料”，不证明 claim 被语义支持。
可单独运行模型辅助语义检查，输出 Citation Precision / Evidence Coverage 和问题定位；它不等于事实认证。
搜索结果和正文分别保留快照，不将摘要伪装成网页全文。
模型执行限制为 60 个 graph steps，HTTP 和模型有超时；尚无完整 token / 时间预算策略。
原始资料、报告和运行记录可能包含任务内容，请自行管理本地 runs/。
依赖解析后的精确版本保存在 requirements.lock.txt（如已生成），可用 pip install -r 安装。

## 证据验证与评测

```powershell
.\.venv\Scripts\python.exe -m researchdesk.quality verify 9e359ea792374eed84afb6c74b4569b9
.\.venv\Scripts\python.exe -m researchdesk.quality benchmark
```

输出保留原报告，另存评估 JSON 和 Markdown。参考标签仍需人工复核。
详见 [证据验证与指标口径](docs/evidence-verification.md)。

可选原文锚定分句和 v2 judge：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.quality verify 9e359ea792374eed84afb6c74b4569b9 --granularity clause --judge-prompt v2
.\.venv\Scripts\python.exe -m researchdesk.quality compare
```

旧 line/v1 行为保留；新策略不等于完备原子 claim 抽取。
见 [分句、引用范围与对照实验](docs/claim-granularity.md)。

## 端到端研究任务评测

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval
```

三项受控任务覆盖选型、来源冲突和证据不足；执行层禁用外部搜索，报告生成与验收分开统计。
所有模型轮次用量单独记录，评分器用量也纳入汇总。
见 [任务集、成功标准与统计边界](docs/task-evaluation.md)。

## 方向与里程碑

可对已完成报告运行一次证据绑定修订，保留原稿、候选、差异和拒绝回退结果：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.repair runs/<RunID>
.\.venv\Scripts\python.exe -m researchdesk.paired_eval runs/task-evaluations/<基线评测ID>
```

见 [单轮修订、恢复边界与配对实验](docs/report-repair.md)。

新增版本/条件/分母三类迁移任务，以及不显示模型评分的待人工填写包：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval --dataset evals/transfer-tasks-v1.json
.\.venv\Scripts\python.exe -m researchdesk.review export runs/task-evaluations/<评测ID> --dataset evals/transfer-tasks-v1.json --output runs/reviews/<唯一包名>
```

人工提交后单独统计分歧，空模板不会计为人工通过。见 [新任务与人工复核](docs/human-review.md)。

对同一完整引用评分输入进行有界重复测量（默认3轮），保留分歧与未知用量，不做多数表决或改写原分数：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.judge_stability <评测目录> --dataset evals/transfer-tasks-v1.json --repeats 3
```

见 [三处争议诊断与评分一致性](docs/judge-stability.md)。

任务层进程终止与恢复矩阵（真实模式会调用模型）：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.fault_eval --scripted
.\.venv\Scripts\python.exe -m researchdesk.fault_eval
```

分别检验证据、回执和报告三个窗口，记录恢复率、状态不变量和重复恢复行为。见 [任务层故障恢复评测](docs/task-recovery-evaluation.md)。

离线汇总已有运行的模型成本、工具状态、预算和恢复事件，生成独立诊断报告：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inspect_run runs/<RunID>
```

无需API Key；不修改原运行。见 [运行诊断与事件时间线](docs/run-diagnostics.md)。

可选资料目录上下文策略与受控对照（默认仍为全文）：

```powershell
.\.venv\Scripts\researchdesk.exe "研究问题" --local-only --document ./notes.md --context-mode catalog
.\.venv\Scripts\python.exe -m researchdesk.context_eval
```

比较完整生成成本、读取调用和引用质量，不按初始字符数自动晋升策略。见 [Context策略实验与恢复边界](docs/context-experiment.md)。

可选持久化模型请求次数预算：

```powershell
.\.venv\Scripts\researchdesk.exe "研究问题" --local-only --document ./notes.md --max-model-calls 12
```

请求前事务预留，失败和中断不退款，恢复不清零；修订继承剩余额度。它不是Token/费用上限，独立实验评分另计。见 [模型调用预算与恢复语义](docs/model-call-budget.md)。

一条命令完成研究、验证、最多一轮修订和诊断：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.workflow "研究问题" --local-only --document ./notes.md
.\.venv\Scripts\python.exe -m researchdesk.workflow --resume runs/workflows/<工作流ID>
```

默认共用12次模型请求额度，未过门槛则标记待复核；模型门槛通过不等于人工审核或业务验收通过。见 [受检工作流与恢复状态](docs/checked-workflow.md)。

工作流也可配置 `--max-tool-calls`、`--max-provider-requests`、`--tool-retries`、`--tool-timeout`、`--tool-deadline`，策略随计划固定，源/候选分支不一致会拒绝恢复。见 [工作流工具预算](docs/workflow-tool-budget.md)。

有模型预算时，修订生成会为候选验证持久化预留一次额度；余额不足或生成触及尾部时保留原稿并转为待复核。见 [尾部验证预算](docs/tail-verification-budget.md)。

可选 `--search-context preview` 缩短搜索工具返回给模型的摘要，已入库快照与ID保持不变，默认仍为full。正文读取、权限和预算独立控制；见 [搜索上下文预览](docs/search-preview.md)。

候选报告写作策略可通过 `--report-policy v2` 启用，默认仍为 `v1`。版本和实际提示词哈希随运行固定，修订继承；见 [报告写作策略版本](docs/report-writing-policy.md)。候选策略尚未证明质量提升。

通过 `python -m researchdesk.report_eval --model deepseek-flash` 可先生成不调用 API 的冻结对照计划，之后使用 `--execute-plan` 执行一次。业务 rubric 与引用评分同时检查，默认不晋升；见 [报告策略对照实验](docs/report-policy-experiment.md)。

通过 `python -m researchdesk.inference export runs/<RunID> --output runs/inference-traces/<新目录>` 可离线导出“原句—前提—证据”附件；结构完整不等于语义通过，不影响原报告评分。见 [推断证据链](docs/inference-trace.md)。

`python -m researchdesk.inference_check prepare` 可离线冻结指定原句的独立语义判定请求，再由 `execute` 单次调用模型，分别检查前提支持、范围、推断及结论强度。保留原分数，准确率尚未验证；见 [独立推断语义判定](docs/inference-assessment.md)。

`python -m researchdesk.inference_calibration prepare evals/inference-calibration-v1.json --output runs/inference-calibrations/<新目录>` 可离线准备4对正反例，`score` 比较已有输出与候选参考。标签一致不自动表示理由正确，整体质量仍待复核；见 [推断判定离线校准](docs/inference-calibration.md)。

同一入口的 `prepare-execution <校准包> --model deepseek-flash --output <新计划>` 先离线固定请求，`execute <计划>` 最多调用模型1次批量处理这些案例；候选参考留在本地，失败或中断不自动重试。

校准包可显式指定 `--judge-prompt v2` 使用候选 scope/inference 合同，默认仍为v1。旧参考及原结果保持不变；见 [判定合同 v2](docs/inference-contract-v2.md)。

见 [docs/direction.md](docs/direction.md)。先跑通真实主流程，再推进可靠性与评测。

接口参考：
- https://github.com/langchain-ai/deepagents
- https://reference.langchain.com/python/langchain-deepseek/chat_models/ChatDeepSeek
- https://docs.tavily.com/documentation/api-reference/endpoint/search

