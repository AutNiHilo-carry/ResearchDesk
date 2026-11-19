# 保留结论的事实性标签复核材料

2026-11-19，复用[计数合同两题对照](counting-contract-pilot.md)与[正文上限修订](body-limit-live-repair.md)的已有输出，整理8组候选近义句对，记录4组机器事实性标签差异。本轮零 API，没有重新评分、修改历史标签或放宽验收门槛；human_review 仍为 pending，accuracy 为 null。

## 观察结果

| 组别 | 候选句对 | 标签不同 | 标签相同 |
| --- | ---: | ---: | ---: |
| v1 / v2 写作结果 | 5 | 3 | 2 |
| v2 原稿 / 被拒修订候选 | 3 | 1 | 2 |
| 合计 | 8 | 4 | 4 |

以下是保存的机器标签，不是人工参考答案：

| 案例 ID | 左侧 factual | 右侧 factual |
| --- | --- | --- |
| pg_abstention | true | false |
| pg_unverified_and_advice | true | false |
| sqlite_version_limit | true | false |
| pg_abstention_after_repair | false | true |

例如原稿“不能据此确认 17 版所有锁行为完全相同。”被标为 false，修订候选“结论：不能确认17版所有锁行为与18版完全相同。”被标为 true。版本范围与建议句也有差异。另保留4组同标签案例，覆盖锁冲突、默认日志模式、MVCC 与摘记版本范围；同标签同样不证明判断正确。

这些句对是在观察差异后选出的开发材料，尚未确认语义等价，不能把4/8解释为错误率或泛化不一致率。两次写作及修订的全文上下文不同，原稿评分包含两个任务，候选评分只有一个报告；共同的评分 Prompt 哈希与模型别名不等于固定了供应商后端快照。不得归因于某个前缀、格式、写作策略或预算提示。

## 本地复核包

最终材料位于 `runs/abstention-label-reviews/c120282a208b4026b474bf581ffc43ef/`，不作为公开附件提交：

- `reading.md`：完整原问题、正文、来源内容、证据摘录和8组句对，保留上下文，未展示保存的机器事实性标签。
- `v1-context.json`、`v2-context.json`、`repair-context.json`：可追溯上下文，带来源运行路径、文件哈希及任务标识；排除评分结果、修订选择理由和机器 factual 字段。模型设置仅为记录或按实现整理的部分配置，不是完整原始请求抓包。
- `review-template.json`：审核者、时间和理由留空，等价关系与左右事实性标签为 null，原子主张列表为空；绑定三份上下文 SHA256。
- `*-assessment.json`、`*-cases.json`、`*-comparison.json`：单独保存机器侧材料。两份写作批次评分由原来的平铺结果显式包装，记录原文件与上下文哈希，没有伪装成原始请求记录或新评分；修订候选评分包装按原字节复制。
- `provenance.json`：记录来源评分哈希、事后选例范围、零请求及待人工复核状态。

人工复核可先阅读 reading.md，另存填写后的表，分别检查：句对是否等价；每行包含哪些可核查事实、资料范围陈述、研究状态、推断或建议；整行 factual 应如何判定并说明理由。不能仅按“建议”“不能确认”等标记词判断。以上是待审核问题，不是已经采用的新评分合同。

材料来自事后选例，即使阅读文件不显示标签，也不能称为独立盲测。审核者身份和时间是自报信息，没有自动认证、导入标签或晋升评分器的流程。独立人工意见尚缺，继续保持原报告选择、默认 Prompt 和历史指标。

## 离线核验与复现

复用现有[事实性分类对照命令](judge-consistency.md)，无需新增运行时机制。在项目环境中，以最终材料目录为当前目录执行：

```powershell
python -m researchdesk.judge_consistency --left v1-assessment.json --right v2-assessment.json --cases writing_arms-cases.json --output writing-recheck.json
python -m researchdesk.judge_consistency --left v2-assessment.json --right repair-assessment.json --cases original_repair-cases.json --output repair-recheck.json
```

输出目标必须尚不存在。结果应分别为5对/3处差异与3对/1处差异；成功退出只表示材料校验完成，不表示语义等价或人工验收通过。

准备过程使用[只读私有副本](repair-citation-diagnostics.md)读取 SQLite，包含已提交 WAL；逐文件核对原运行绑定，避免只读连接在原目录生成边车。原批次质量输入由完整报告、来源与证据重建，保存标签经确定性复算一致；候选缓存同样核验输入与评分 Prompt。另行核对两组对照可复现、8项人工字段均未填写、上下文不含机器评分字段，且原运行白名单文件哈希未变。本轮只做材料与文档核验，没有重新运行整套测试，也没有新的模型质量实验。
