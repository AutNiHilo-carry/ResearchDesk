# 保存评分的人工复核操作指南

这条流程只收集事实性意见：阅读上下文 → 另存填写表 → 零API记录意见。它不修改原评分，不自动晋升策略，也不构成任务验收。与常规逐报告的 `researchdesk.review import` 是不同格式。

## 1. 找到材料

在已配置项目依赖的本地仓库中，先打开已有稳定性核验目录的 `review.md`，同时保留同目录的原始audit.json、gate.json、review-context.json和review-template.json。原评测目录、原任务集及原稳定性result.json也必须保留，专用入口会重建核对，不能只携带一份表来导入。

本项目已有2题17行的本地材料：

```text
runs/stability-direct-checks/6e711131be6747aa917c57552e6876f7/85855f2d2d5545dd92228f15297cbc26/
```

该路径不是公开附件，新克隆仓库不会自带这些文件。没有此材料时，可按[保存结果重检](judge-stability.md#保存结果的零-api-核验)从自己的完整原记录生成；需要的是已保存记录，不要为了导出材料重新调用付费评分。

## 2. 另存一份填写表

以下PowerShell示例在仓库根目录执行，仅复制空表，不填写人工意见，不调用API：

```powershell
$reviewBundle = Resolve-Path 'runs/stability-direct-checks/6e711131be6747aa917c57552e6876f7/85855f2d2d5545dd92228f15297cbc26'
$reviewDraftRoot = Join-Path 'runs/stability-review-submissions' ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $reviewDraftRoot -ErrorAction Stop | Out-Null
$reviewDraft = Join-Path $reviewDraftRoot 'review.json'
Copy-Item -LiteralPath (Join-Path $reviewBundle 'review-template.json') -Destination $reviewDraft -ErrorAction Stop
Write-Output $reviewDraft
```

编辑打印的review.json，原模板不动。reviewer填实际审阅者，reviewed_at填含时区的实际审阅时间，例如格式 `YYYY-MM-DDTHH:mm:ss+08:00`；不要复制示例身份或时间来冒充审核。保留version、context_sha256、全部unit_id及原有字段；status可以仍为pending，由程序计算完成度。

每行填写factual（true／false／null）、atomic_claims（字符串列表）、reason。确定标签必须有理由，原子主张字符串不能只含空白。未审或资料不足时保留null，可以在reason解释缺口。保留所有行，不能只提交已看过的行。不得用“建议”“不能确认”等单个词直接决定分类。

review.md包含完整评分输入的分行正文与引用摘录，不是完整原文。需要更多资料时查看实际来源，不能把没提供的内容当作反证。机器标签与差异在audit.json，建议先独立记录意见再查看；这些仍是事后选材，不称为独立盲测。

## 3. 记录意见

将下面的填写表路径替换成自己的文件。其余路径对应上面的本地材料，输出使用新的目录：

```powershell
$reviewFile = 'runs/stability-review-submissions/<你的目录ID>/review.json'
$reviewRecord = Join-Path 'runs/stability-review-records' ([guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m researchdesk.stability_review 'runs/generation-contract-plans/5746f5fdbf8547d3abe02f453d26ffd4/execution/v2' --dataset 'evals/technical-selection-v1.json' --source 'runs/v2-formal-stability/13d4b90822d54de3ac288c2570726cea/executions/1e150d37df2a429a98a96b54e40417aa/result.json' --bundle $reviewBundle --review $reviewFile --output $reviewRecord
$reviewExitCode = $LASTEXITCODE
Write-Output "exit=$reviewExitCode; output=$reviewRecord"
```

| 退出码 | 含义 | 下一步 |
| --- | --- | --- |
| 0 | 全部行有确定标签，格式和原记录绑定通过 | 阅读新summary.md，仍不能据此宣布研究正确或任务通过 |
| 3 | 已另存意见，但还有null／未知 | 阅读未知数，后续另存新的意见版本；不要把未知填成false来求通过 |
| 1 | 提交或绑定不合法，未写接受结果 | 检查填写字段、原材料、时区及源记录；不要改哈希绕过校验 |
| 2 | 命令参数错误 | 查看 `python -m researchdesk.stability_review --help` |

不要把所有非零退出码都看成程序崩溃，也不要把0等同于模型或应用质量门槛。合法输出包含submitted-review.json（提交原文）、result.json（记录与未知计数）和summary.md。原门槛、历史成绩不回写；若要补充意见，保留旧版本，使用新提交文件和新输出目录。

审核者身份自报、未经认证；程序不计算准确率或Task Success，final_acceptance保持pending，promotion保持none。具体绑定、错误与多文件读取限制见[专用入口合同](judge-stability.md#专用人工意见记录入口)。
