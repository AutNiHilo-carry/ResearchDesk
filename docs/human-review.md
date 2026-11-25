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

## 未发布修订稿的复核意见

从原评测的单个任务、候选正文和原证据生成可分享材料：

```powershell
python -m researchdesk.review export-candidate <原评测目录> --dataset <任务集.json> --task-id <任务ID> --candidate <候选正文.md> --output <新目录>
```

导出 `reading.md`、绑定正文和证据的 `packet.json` 以及空白 `review.json`。来源只显示文件名，材料中没有已有的评分或人工意见。引用无效或原评测与候选之间的证据无法核验时，拒绝导出。

收到绑定到 `unpublished-candidate-review-v1` 复核包的已填写表后，可以离线导入：

```powershell
python -m researchdesk.review import-candidate <原评测目录> --dataset <任务集.json> --bundle <候选复核包目录> --candidate <候选正文.md> --review <已填写的表.json> --output <新目录>
```

导入会从原评测重建复核材料，核对候选正文、冻结要求和来源证据，并检查表格中的事实标签、引用和逐字正文锚点。结果单独保存为对未发布稿的自述意见，原报告和模型评分继续保留。审阅身份和独立性由实际审核流程确认；本命令只检查内容绑定，无外部 API 请求。

## 同文重评审计

配对实验同时保存comparison-audit.json，检查最终正文哈希、引用评分完整输入哈希及未变正文的标签分歧，避免把相同报告两次评分的波动归因于修订。已有实验可不调用模型补做审计：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.evaluation_audit <基线目录> <配对目录> --output runs/comparison-audits/<唯一名>
```

它保留原始分数，不能选择更漂亮的一次作为最终结果；单次分歧也不能估计方差或证明哪个评分器是对的。下一步应优先人工核对争议摘录和计算推导，再设计独立重复运行。

## 导入期间的模型结果绑定

导入在复核包校验后，核对实际用于分歧比较的 result.json 与 citation-quality.json 内容哈希是否仍与包一致，再从这份已核对的文本解析。任一内容变化、原评分被删除或原本缺失的评分后来出现，均在创建输出目录前拒绝，不能把不同版本的模型成绩归到同一复核包。

若导出时没有模型引用评分且导入时仍缺失，继续允许人工复核，缺失项不生成模型／人工引用分歧。这个检查不提供多个文件的全局原子快照，也不认证审核者身份；它保证本次比较使用的模型文本与已校验包绑定，而非保证磁盘文件此后不再变化。回归使用明确标识的测试标注，不能作为独立人工意见。

## 对照审计的资料与标签绑定

`evaluation_audit` 会从两侧当前报告、来源和证据重建逐行引用评分输入，核对保存的 quality_input_sha256；不再仅凭两份结果文件声明的哈希相等，就认为输入相同。报告含 fenced 文本时拒绝，避免分行器省略内容。SQLite 通过共享读取器的私有副本读取，保留 WAL，原数据库目录不写入。

完整引用评分必须有输入绑定，且每个单元恰好出现一次；单元 ID、任务 ID、行号、文字及引用清单必须对应重建输入，每个引用标签的 ID 也必须完整且不重复。审计保存实际读取的两份评分文件 SHA256，解析和哈希使用同一份字节。当前检查限于输入和标签锚点，不重算缓存质量指标，不验证标签语义正确性，也不是业务或应用门槛验收；需要完整指标复算时使用[已有结果验收](model-assessment-boundary.md)中的对应入口。

缺失／未完成的引用评分分别标记 missing／incomplete，citation_label_comparison_status 为 unavailable；空分歧列表不表示标签一致。旧资料没有保存输入绑定时，citation_input_identical 为 null，不能称为相同输入；若同时提供完整标签却没有绑定，则拒绝比较。正常的同文标签差异仍保留，模型成绩和默认策略不改写。

该检查绑定本次实际读取的材料，不是多文件原子快照或作者认证；还不能证明供应商后端模型版本相同。正文不变但其他任务或证据上下文不同，也不能直接归因于随机评分波动。回归使用脚本资料和标签，覆盖正文变化、证据变化、错文字、重复标签、缺少绑定和缺失评分，不替代独立人工意见。

## 控制条件指纹版本

新审计使用 controls_version=`evaluation-controls-v2`，同时保存 controls 本体。它包含数据集、任务顺序、门槛、语料哈希、模型声明、模型设置、任务后缀／两种评分 Prompt 哈希，以及此前漏入指纹的 generation_contracts。哈希覆盖版本和完整条件：

```python
from researchdesk.review import fingerprint
assert audit['controls_sha256'] == fingerprint({
    'version': audit['controls_version'], 'controls': audit['controls'],
})
```

fingerprint 使用保留 Unicode、按键排序的 JSON 文本计算 SHA256。缺少历史生成合同记录时，controls.generation_contracts 为 null，不推断成 legacy 默认值；有记录与缺少记录因此能在指纹中区分。左右合同不一致仍会拒绝，既有其他控制条件比较保持不变。

旧审计没有 controls_version，其 controls_sha256 未覆盖生成合同，不与 v2 哈希直接比较；摘要显示 legacy_unversioned。新版本不会修改旧审计、报告或分数，需要时从原材料重新离线审计并另存输出。这个指纹证明记录内容的绑定，不证明声明符合实际执行、模型后端快照相同或某个策略有效；它也不包含全部修订参数，不能用同一指纹宣称整个实验配置完全相同。

## 事实分母变化诊断

对两侧评分均可比较且正文未变的任务，审计新增 factuality_audit：按已绑定的 factual 和引用 verdict 重算事实行数、覆盖行数与覆盖率，列出事实→非事实的 excluded_units 以及反向的 included_units。覆盖定义与原验证器共用：事实行至少有一条 supported 且没有 contradicted 才算覆盖；不把多个 partial 合并为 supported。

出现分类集合变化即标记 review_required；如果事实行减少、覆盖率升高且覆盖行没有增加，额外列入 coverage_increase_task_ids。摘要展示前后分子／分母和被排除的单元，防止把“少算了一条未覆盖事实”解读为正文质量提高。它不判定哪一次分类正确，也不自动恢复旧分类或更改验收门槛。

这些计数来自标签，不信任缓存的 covered 或聚合覆盖率；这不是完整缓存指标一致性验收。未知 verdict 或 factual 与引用 verdict 的非法组合在计数前拒绝，invalid_evidence 仍按原派生标签规则处理。评分缺失显示 unavailable，正文变化的任务不作同文排除诊断；零事实行的覆盖率为 null，不能当作100%。同文任务的全批次评分上下文是否一致另由 citation_input_identical 标识，不据此推断采样或后端变化的原因。

入口与[现有同文审计](#同文重评审计)相同，输出仍只是诊断，成功退出不表示任务通过；原报告和成绩不写回。回归覆盖覆盖率因排除升高、排除已覆盖行但未升高、全部排除、仅引用变化、缺失评分与未知标签，并确认原文件未变。测试标注不代表真实人工参考，本轮实现不调用模型。

已对[真实 v2 复放](v2-formal-stability.md)保存的两轮标签作零 API 验证：版本题事实行4→3但覆盖率仍为1.0；WAL题事实行5→4、覆盖行4→4、覆盖率0.8→1.0，两轮均将 WAL 列入需要关注的任务。诊断附件在本地 `runs/factuality-denominator-diagnostics/35a0644687b14285ad991c9995363acd/result.json`；该附件是经输入及标签核验后的计数视图，不是一次新的完整任务评测。另核对真实 v1/v2 正文不同的比较返回 no_unchanged_reports，不误套同文结论。

## 显式事实分类门槛

自动化需要退出码时，可显式启用：

```powershell
python -m researchdesk.evaluation_audit runs/baseline-eval runs/regraded-eval --output runs/factuality-gates/new --gate factuality
```

示例输入目录需替换为已有的两份评测，输出必须为新目录；命令零 API。不加 --gate 时仍是兼容的诊断模式，格式化成功返回0，即使摘要提示复核，也不能把退出0当作任务通过。

--gate factuality 会另存 gate.json，并在摘要记录状态：0表示两侧评测完整、完整引用输入相同、全部正文未变且事实分类集合无变化；3表示 review_required 或 unknown，不放行。未知包括缺失评分、旧审计信息不全、评测未完成、上下文不同或正文变化。非法输入仍失败返回1，参数错误由CLI返回2。

gate.json 使用 factuality-comparison-gate-v1，audit_content_sha256 绑定 result.json 解析后的规范内容，可用 fingerprint(json.loads(...)) 复算。该哈希用于内容绑定，不认证作者。分类变化包括排除事实或新增事实，不仅检查总数是否相同；人工意见与最终验收仍pending，没有自动晋升。

这个门槛只检查本次同文同输入的事实分类一致性，不验证引用 verdict 的正确性或一致性，也不验证缓存全部指标、业务验收与研究质量。0不是研究成功、报告改进或独立人工审核完成；资料不同或正文变化时返回3也不等于新报告一定有错。应用验收仍使用任务评测的独立门槛。

已将真实 v2 历史引用标签与已保存的新轮次标签制成显式标识的离线审计副本：候选侧复用原正文和业务标签，仅替换引用标签并派生相应计数，历史用量标记为复制信息，不是新增消费；副本不是新的研究运行，也不用于恢复或统计新的任务成功。CLI实测同文分类变化返回3／review_required，同一历史标签与自身比较的接线校验返回0／passed，真实v1与v2正文不同的比较返回3／unknown。自比较不是独立重复实验。记录位于本地 `runs/factuality-gate-probes/4b22edf90c164e379f4af7a9e045678f/`，原v2目录逐文件哈希未变，整个验证零API。
