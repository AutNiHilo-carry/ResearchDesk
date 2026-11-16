# 独立推断语义判定 v1

inference-check-v1 消费已完成研究的推断附件，分别记录前提支持、来源范围一致性、推断有效性和结论强度。这是新增的模型辅助判定，不替换逐行引用评分、不改写原报告、不自动晋升策略。

## 先离线准备

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inference_check prepare runs/<RunID> --trace ./trace.json --unit U6 --model deepseek-flash --output runs/inference-assessments/<新目录>
```

prepare 不读凭证、不调用 API。须显式指定1到8个互不重复的原始报告 unit ID。完整 trace 仍覆盖全部原行；未选行可保留 unclassified，但所选行必须有完整前提、证据与简短可审查的 rationale。请求明确标记只评估这些句子，不能将子集结果当作整份报告通过。

冻结请求包括原报告完整行、所选前提与证据、全部来源正文，以及报告/trace、payload、判定提示词、相关源码的哈希、固定模型及 SDK 参数。全量来源供检查范围和资料缺失，不代表网页或产品全部事实。序列化输入超过60000字符则拒绝，不截断证据。rationale、资料和报告均为待核查数据，不是指令或证明。

## 单次执行

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inference_check execute runs/inference-assessments/<目录>
```

execute 会将冻结请求中的资料和报告发送给配置的 DeepSeek API，并产生费用。每目录最多一次 SDK 调用，temperature=0、timeout=90秒、max_retries=0、max_tokens=8192。一次调用不是 Token 或费用上限，也不保证服务端 exactly-once。

先核验原始输入、提示词及源码绑定，再独占创建 execution 目录，请求前保存 request_started。中断、超时、未知结果或评分失败不自动重试；再次执行同一目录会拒绝。未知用量为 null，不按零收费计算；SDK返回用量后即使输出截断也保留。不可用一个新目录悄悄补做同一次失败请求。

## 分开判断与严格校验

每个前提分别得到 supported、partial、contradicted 或 insufficient，并对其每条证据返回逐字摘录锚点。支持判定至少需要一条非空锚点；无关证据允许空锚点，但不能借此宣称支持。锚点必须是对应 snippet 的连续子串，模型无法把别的来源片段冒充该证据。逐字匹配只证明锚点存在，不能保证语义判定正确。

每个结论另外记录 scope（consistent/inconsistent/uncertain）、inference（valid/invalid/uncertain）、strength（within_evidence/overstated/uncertain）。所有前提都 supported，且范围一致、推断有效、结论不过强时，才派生 model_support_established；否则派生 model_support_not_established。不让模型自行填最终通过字段，也不把正确引用直接等同于正确推断。

严格拒绝缺项、重复或新增结论/前提/证据、编造锚点、空白理由和额外评分字段。原输出先单独保存，只有全量校验通过才写 result.json。结论包含“现有资料不足以建立保证”时，提示词要求区别于“产品确定不能提供保证”，并检查相关范围和前提，不能只因来源没逐字写结论就否决，也不能把“未提及”当能力不存在。

## 结果与验证边界

结果始终 human_review=pending，affects_original_scores=false，不计算新的 Citation Precision、Evidence Coverage 或 Task Success Rate。不改动原版 line/v2 评分。模型判定仍可能错误；独立推断判定当前仅为候选能力，未验证准确率。

离线正反例由测试桩提供标签，验证派生规则：正确摘录但版本范围冲突、推断无效、结论过强、前提缺证均不能被合成为支持建立。另测冻结输入、格式缺项、锚点造假、请求前记录、失败保留和无重试。测试桩不是模型正确性实验、人工标签或独立 benchmark；injected_offline 与真实 model_assisted 分开记录。

下一阶段应以实际人工复核和新正反例检验语义判定准确率，保留与旧逐行指标的差异，再考虑是否进入工作流。当前不接入报告验收门槛。
