# 模型评分与最终验收的边界

技术选型试验出现过评分引文错误、额外字段、负向标准误判等问题。详情见 [完整批次稳定性试验](task-grading-stability.md)。即使输出校验通过、模型门槛达标，也不等于研究结论已由人工验收。

原工作流已经保留 `human_review=pending` 和 `needs_review`；本次补齐任务评测与报告策略对照的机器字段和摘要展示，不修改工作流门槛，不改变旧分数，不升级默认评分提示词。

## 新结果的状态

每个任务新增 `assessment`，由现有生成状态、批量评分状态和评分结果派生：

| scoring_status | 含义 | model_gate |
| --- | --- | --- |
| valid | 批次评分完成，存在 rubric、引用质量和布尔任务判定 | passed 或 failed |
| invalid | 批次评分失败，或完成状态缺少必要评分结果 | unknown |
| not_scored | 生成未完成，未获得有效报告评分 | unknown |
| pending | 批次仍在运行 | unknown |

所有这些状态下，`human_review` 和 `final_acceptance` 均为 `pending`。本入口没有执行人工审核的能力，也不会把输入文件里自行填写的 approved 字段当作审核证明。

`valid` 只描述评分合同和评测流程完成，不证明语义判定正确。`passed` 是原有业务 rubric 与引用门槛的模型辅助结果，不是最终验收。任务原有 `success` 和任务通过率仍保留；生成失败继续计入原任务失败分母，不因展示层的 unknown 而改分。

批量评分失败时，即便部分业务评分已保存，整体任务评分仍显示 invalid/unknown，部分结果保留供复核。不会重新请求模型、清洗额外字段或从原资料借用引文以改变结论。

## 离线查看历史结果

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval --summarize runs/technical-selection-pilots/<ID>/execution/v1/result.json --output runs/assessment-views
```

此分支在加载凭证前执行，不调用模型。它在指定输出根目录下创建新的 UUID 目录，写入新 `summary.md` 和 `provenance.json`。来源哈希绑定读取到的原 JSON 字节；`api_requests=0`、`scoring_recomputed=false` 明确标识只重渲染。

原 JSON、正文、评分和旧摘要不覆盖。新视图现场派生状态，不信任旧文件内可能过期的 assessment；它不验证原 JSON 的真实性，也不进行语义复评。缺少必要结构的文件拒绝渲染。原有未知值保持未知，原标量分数不重算。

报告策略对照摘要也明确标注“人工审核/最终验收待进行”，表中的通过率仅是模型辅助判定。源码变更仍受既有冻结计划哈希约束，旧计划漂移后不会直接执行；离线显示旧结果不需要放宽该约束。

## 本轮验证

本轮零 API。使用上轮真实试验 v1 评分失败和 v2 评分完成的两份原结果生成独立视图：前者显示 invalid/unknown，后者按原分数显示 passed/failed，全部仍待人工验收。原结果哈希保持不变。

测试覆盖通过/未过模型门槛、批次评分无效、生成失败、运行中状态、畸形布尔判定、过期 assessment、来源不修改、离线分支不加载凭证、不重复覆盖和坏输入不产生有效摘要。测试桩不代表模型质量验证。
