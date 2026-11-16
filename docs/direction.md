# ResearchDesk 项目基准（用户确认版，2026-11-05 记录）

## 定位和底座
可恢复、可验证、可评测的长任务研究 Agent。
Python + Deep Agents，底层理解 LangGraph；不切换 Pydantic AI，不重写 Agent Framework。
Deep Agents 提供 runtime / primitive；项目负责业务策略、可靠性、评测。
抽象阻碍关键实现时，局部下沉 LangGraph。

## 简单业务
技术研究 / 选型问题 → 计划 → 搜索 / 文档 → 资料 → 证据 → 方案比较 → 带引用报告。

## P0
1. Evidence Ledger + Verification：claim/source/snippet/URL 或 file/retrieval time。
   验证引用支持关系，处理冲突与证据不足；评测 Citation Precision、Evidence Coverage。
2. Tool Execution Control：request/schema/permission/budget/timeout/bounded retry/idempotency/result validation。
   区分参数错误、超时、重试安全性、副作用重复执行、权限和异常返回。
3. Recovery Policy：复用框架 checkpoint primitive，定义恢复语义。
   kill process 后重启恢复，验证步骤复用、必要重执行、去重、副作用与一致性。
4. Evaluation + Fault Injection：小型研究任务集；记录 Task Success Rate、Citation Precision、
   Evidence Coverage、Tool Success Rate、Recovery Rate、Token Cost、Latency、Retry Count。
   对照实验尽量固定模型、Prompt、数据集，明确变量。

## Context / SubAgent / RAG
Context 的贡献是策略和实验：全量保留、summarization、compaction、filesystem offloading、
evidence 外置存储；比较成功率、成本、稳定性、信息丢失和引用质量。
默认单 Lead Agent。仅在并行或隔离有价值时增加 Research SubAgent，并对照报告质量、
引用质量、Token Cost、Latency、Context Pollution；无明显收益则不用。
RAG 服务资料获取、证据选择、claim grounding、citation verification，重点是结论有证据支持。

## Skill Lifecycle（P1/P2）
轨迹 → Candidate Skill → 离线 Eval → 旧版对照 → Regression Check → 人工审核
→ Version → Promote → 必要时 Rollback。不能由 Agent 自行认定有效后直接上线。

## 贡献边界
框架：runtime、checkpoint/context primitive、memory、SubAgent、HITL、tool calling。
个人贡献：Evidence Ledger/Verification、Tool Policy、Recovery Policy、Fault Injection、
Evaluation Harness、Context 策略与实验、后期 Skill Lifecycle。
表述：“基于框架的 X 能力设计 Y 策略，通过 Z 实验验证。”

## 阶段与验收
- Phase 1 MVP：真实研究问题 → 搜索/文档 → Evidence → 带引用报告。真实调用验证前不宣称完成。
- Phase 2 可靠性：Tool Policy、Checkpoint/Resume、Recovery Policy、Fault Injection。
- Phase 3 质量：Evidence Verification、Evaluation、Observability。
- Phase 4 差异化：Context 对照实验、可选 SubAgent。
- Phase 5：Skill Candidate、Eval Gate、Human Review、Version、Rollback。
前一阶段未跑通，不增加后续高级功能。

## 范围与目标
少数重要问题做深，避免堆 Agent/框架/Tool、自研完整框架、复杂前端或虚构规模。
每个亮点回答：为什么需要、缺少的后果、框架提供什么、自己实现什么、替代方案、
trade-off、实验验证、故障处理。
与 CityMate 互补：CityMate 展示 Java 后端/高并发/缓存/消息/订单可靠性；
ResearchDesk 展示 Agent Harness/工具可靠性/恢复/证据/评测。

