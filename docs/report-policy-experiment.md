# 报告写作策略对照

比较 v1 与候选 v2 是否改善证据支持，同时保留问题要求。复用 task_eval 的独立任务 rubric 与逐行引用评分，不调整门槛，不增加修订轮数，不将评分器换成更宽松版本。

## 先冻结计划

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_eval --model deepseek-flash
```

这条命令不读取凭证、不调用模型。默认冻结既有 research-tasks-v1 的三个虚构资料任务、全文、问题、rubric、模型、超时、所有应用 Python 文件哈希、生成策略与评分提示词哈希。默认每任务每组最多6次生成请求，工具最多10次，无搜索、无工具重试；每组另外最多两次批量评分请求。整个计划最多40次 SDK 模型请求，不是 Token 或费用上限。

实际问题和资料发送给配置的 DeepSeek API；评分请求另外包含 rubric 与报告。rubric 不进入生成请求。任务集已经用于开发，不能称为独立 held-out benchmark，参考要求仍待独立人工复核。

## 执行一次已检查的计划

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_eval --execute-plan runs/report-evaluations/<ID>
```

这是付费模型实验。只能使用冻结计划，不能同时指定替换参数；文件和代码漂移会在请求前拒绝。固定 v1→v2 顺序，每任务每组一次生成，不修订、不自动晋升。每个完成的运行再次检查模型预算、报告策略、上下文和工具范围。

execution/result.json 与 summary.md 汇总两组结果，各组保留完整原报告、证据、rubric、引用精度/覆盖率、耗时、Token、失败与未知值。任务通过需同时满足业务 rubric 和原有引用门槛，不能通过删掉任务要求换取较高引用分数。生成失败记为失败；评分未知保持未知。

实验执行目录只创建一次；中断或评分失败保留已完成部分，禁止直接重跑造成不可见的重复付费。需要重跑时创建独立计划并明确成本，不会在此入口自动恢复。相比逐阶段恢复，这个小样本实验选择保留失败、限制重试和实现复杂度。

## 结论边界

固定顺序、单轮、短文档和同模型评分存在偏差，且已有评分器稳定性实验发现波动。离线注入测试标记 injected_offline，只检验接线、预算、漂移拒绝和结果保留，不能证明质量收益。默认 v1 与人工审核 pending 保持不变；后续是否扩展任务、重复次数或晋升，需要真实结果与人工复核支持。

## 选择指定任务

可重复传入 --task-id 选择1至3题，保留给定顺序；不能与 --limit 或 --execute-plan 混用。准备仍为零 API，执行预算随所选题数冻结。见 [按任务 ID 准备对照](report-task-selection.md)。

## 模型口径与应用状态并列

汇总保留原模型通过率和成本差值，新增每组应用状态及通过/失败/未知的任务数量。逐题配对列展示两组各自的长度与应用状态；不会因为模型通过率是1就省略超长或未检查。应用状态复用已有 gate 派生规则，不信任缓存 application_assessment 或旧 pairs 徽标。

每组状态只从保存的分项记录重算，没有重新读取研究正文、证据或任务集，因此属于记录视图，不是有绑定的验收。invalid_record 表示记录缺失或不一致，数量显示 N/A，不用0/0/0冒充已检查。历史任务缺少长度记录时保持 not_checked / unknown。配对保留两组任务 ID 的并集，缺少对方时成功标签和指标差值保留 None、应用状态为 unknown；重复任务 ID 拒绝处理，避免静默覆盖。

新实验 result.json 的 pairs 增加状态字段；原模型 success、质量与成本字段保留原口径。仅有 complete 表示流程完成，不表示任一策略可自动晋升。人工审核继续 pending。

## 零 API 重绘历史对照

```powershell
.\.venv\Scripts\python.exe -m researchdesk.report_eval --summarize-result runs/report-evaluations/<ID>/execution/result.json --output runs/report-comparison-views
```

本入口不加载凭证、不调用模型或准备新付费计划，只读取给定对照 JSON，另存 summary.md 和带源文件哈希的 provenance.json；不会改写历史结果或原 pairs。输出目录须新建，不覆盖旧视图。不接受模型、任务选择、预算、超时或执行目录等替换参数。中断留下的视图若缺 provenance.json，应视为未完成。

返回0只表示格式化成功；未知、失败和已中断实验仍可保留阅读材料，不能把此退出码当作验收通过。真实自动化验收请对每组的 result.json 使用[绑定结果检查](body-constraints.md)中的 `task_eval --check-result`。旧待执行计划因源码绑定变化须重新准备，已完成历史保持原样。

2026-11-19 已离线重绘后序三题真实对照，保留 v1 原模型通过率2/3、v2为3/3。应用记录视图分别为 failed（通过/失败/未知=0/1/2）和 unknown（0/0/3）；外部副作用题在两组都显示长度 not_checked。没有因原 v2 模型全通过就补造应用验收通过。新视图位于本地 `runs/report-comparison-views/c61540ca64a24349a019658a2863dee0/`，原结果逐字节未变，本轮零 API。
