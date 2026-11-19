# 新任务与人工复核

上一轮三题已经用于修复报告策略，不能再称为未见测试集。新增 transfer-tasks-v1.json，在首次运行前写好问题、资料、rubric 和同样的0.9引用精度/0.6覆盖率阈值，覆盖：

- 版本边界：不能用旧版只读重试规则证明新版副作用工具的安全性。
- 条件保留：企业套餐且联网的自动快照，不能泛化为离线个人版可用；手动导出不等于自动快照。
- 指标分母：研究任务90/100与工具尝试110/120分开，未注入故障不能声称测得恢复率。

这些仍是项目内编写的虚构资料，rubric未由独立人员确认。首次执行后就成为已见数据；不称为独立 held-out benchmark，不基于它反复调 prompt 直到全过。

## 固定策略的一次迁移检查

```powershell
.\.venv\Scripts\python.exe -m researchdesk.task_eval --dataset evals/transfer-tasks-v1.json --output runs/transfer-baseline
.\.venv\Scripts\python.exe -m researchdesk.paired_eval runs/transfer-baseline/<基线ID> --dataset evals/transfer-tasks-v1.json --output runs/transfer-repaired
```

沿用原生成、单轮修订和评分 prompt，不根据新任务答案定制修订。基线评测必须完整评分后才能配对；合法失败保留，评分失败不算0分或100分，也不无上限重试付费请求。已满足修订支持门槛的原稿会直接复用，是否最终通过仍看独立业务评分。

## 导出待人工填写的包

```powershell
.\.venv\Scripts\python.exe -m researchdesk.review export runs/transfer-repaired/<评测ID> --dataset evals/transfer-tasks-v1.json --output runs/reviews/<唯一包名>
```

不调用模型，不读取API Key。packet.json包含问题、验收点、逐行报告、引用证据和完整来源快照；不展示模型评分以减少先入为主，不声称实现了随机盲审。review.json全部标签留null，reviewer和reviewed_at留空。README.md说明标注规则。

人工填写事实标签、每个引用的支持关系、业务验收结果、简短理由和逐字报告片段。reviewed_at使用带时区的ISO 8601，例如2026-11-10T20:00:00+08:00。不要把纯标题当事实，也不要把“推断”标记当作免证据许可。每个引用独立判断是否支持整行事实，partial不算supported。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.review import runs/transfer-repaired/<评测ID> --dataset evals/transfer-tasks-v1.json --bundle runs/reviews/<唯一包名> --review runs/reviews/<唯一包名>/review.json --output runs/review-results/<唯一结果名>
```

只接受完整严格标注：拒绝null、空理由、缺项/重复项、引用不匹配、编造报告片段、无时区日期和变化的评测输入。重新检查数据集/资料/原稿/证据及评测文件的hash，避免给错误版本的报告套上人工标签。哈希用于版本绑定，不是签名或身份认证。

结果单独保存提交的标注、人工任务成功率、原模型任务成功率、事实/引用/业务验收分歧。生成失败仍计入分母，不能用人工标注把失败的生成当通过。模型原结果不会覆盖。模型评分缺失时不能计算该项分歧，但可继续人工复核。

reviewer身份为自行声明，程序无法证明是谁填写。测试桩只能验证流程，不构成人工标签；导出成功也不代表完成了人工复核。本轮程序只导出待填写包，不能由Agent代填后声称独立人工审核完成。

不自动修改默认策略、Skill或门槛。一个标注者与模型之间的分歧不是inter-annotator agreement；如需评估标注一致性，应另收集多个独立人工标注并做仲裁。

## 同文重评审计

配对实验同时保存comparison-audit.json，检查最终正文哈希、引用评分完整输入哈希及未变正文的标签分歧，避免把相同报告两次评分的波动归因于修订。已有实验可不调用模型补做审计：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.evaluation_audit <基线目录> <配对目录> --output runs/comparison-audits/<唯一名>
```

它保留原始分数，不能选择更漂亮的一次作为最终结果；单次分歧也不能估计方差或证明哪个评分器是对的。下一步应优先人工核对争议摘录和计算推导，再设计独立重复运行。

## 导入期间的模型结果绑定

导入在复核包校验后，核对实际用于分歧比较的 result.json 与 citation-quality.json 内容哈希是否仍与包一致，再从这份已核对的文本解析。任一内容变化、原评分被删除或原本缺失的评分后来出现，均在创建输出目录前拒绝，不能把不同版本的模型成绩归到同一复核包。

若导出时没有模型引用评分且导入时仍缺失，继续允许人工复核，缺失项不生成模型／人工引用分歧。这个检查不提供多个文件的全局原子快照，也不认证审核者身份；它保证本次比较使用的模型文本与已校验包绑定，而非保证磁盘文件此后不再变化。回归使用明确标识的测试标注，不能作为独立人工意见。
