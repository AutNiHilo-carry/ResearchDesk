# 五个后端研究案例：首轮真实执行

2026-11-23，新增5题，执行前3题的写作v1→v2固定顺序对照，共生成6份报告。**v1评分失败，v2模型与应用门槛均通过1/3；不能据此证明v2更优。** 两组均为deepseek-flash、body-length-v1、200字符上限；每题每组最多4次生成，同一候选rubric和引用阈值（精度0.9、覆盖0.6）。默认写作策略不变，独立人工审核pending。

## 成绩与失败

| 任务 | v1正文字符／应用门槛 | v2正文字符 | v2引用精度／覆盖 | v2模型／应用门槛 |
| --- | --- | --- | --- | --- |
| SQLite在线备份 | 378／failed | 184 | 1.00／0.8571 | passed／passed |
| PostgreSQL 18队列领取 | 191／unknown | 198 | 0.75／0.75 | failed／failed |
| Stripe API v1重试键 | 212／failed | 180 | 0.6667／0.40 | failed／failed |

计数排除空白和标准Evidence引用标记，其他Markdown字符计入；不是自然语言字数估计。v1业务评分输出包含合同禁止的额外字段report/documents，被严格校验拒绝，整批status=scoring_failed，后续引用评分未执行。v1模型成绩、引用精度和覆盖率均未知；已知超长的两题仍可确定应用门槛失败。没有删除字段后接受、重新评分或追加修订。

v2评分完整，三个业务rubric均通过；后两题仍因引用指标未达阈值失败。模型标签不构成人工正确性结论。SQLite报告可用于展示“问题→证据→有引用报告→门槛→待审”的完整路径，但不代表已经独立验收或实测数据库行为。后两题应展示失败，不能只保留成功案例。

第4题PostgreSQL Read Committed快照、第5题SQLite连接池外键设置保持not_executed，不进入分母。资料是项目编写的官方文档局部中文摘记，任务和rubric也由项目编写，不是独立held-out benchmark。未压测数据库、未调用Stripe服务、未测试真实写副作用。固定顺序、单模型、每题一次运行和v1缺失评分限制了对照解释。

## 实际请求与用量

| 组别 | 生成请求 | 评分请求 | 生成返回Token | 已返回评分Token |
| --- | --- | --- | --- | --- |
| v1 | 9（3+3+3） | 1，格式校验失败 | 26119 | 4097 |
| v2 | 9（3+2+4） | 2 | 34964 | 7686 |

实际共21次SDK请求，低于冻结上限28；Tavily 0次，保存的生成retry_count两组均为0。已返回用量合计72866 Token，包含评分失败响应的4097；这不是完整有效评分的总成本。v1原记录evaluator_total_tokens和total_tokens仍为null，没有把未执行的引用评分填成0。金额未测，28次不是Token或金额上限，剩余额度未消费。

## 本地演示与交付

冻结执行目录：`runs/portfolio-cases/portfolio-20261123-a`。

- 展示首页：`showcase/index.md`，记录全部5题及执行边界。
- 原成绩：`execution/v1/result.json`与`execution/v2/result.json`。完整批次状态incomplete_scoring。
- 原报告：`execution/<v1或v2>/tasks/<task_id>/research/<run_id>/draft.md`；v1报告仍保存，没有完整评分则不生成完整交付包。
- v2交付包：`showcase/v2`，完整评分已打包，整体application_gate=failed；验包成功仅说明材料完整一致。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.delivery verify runs/portfolio-cases/portfolio-20261123-a/showcase/v2
```

打包回执api_requests=0仅指离线打包，不表示本轮没有付费请求。运行产物不提交仓库，上述路径不是公开下载链接。公开提交的是任务、带hash的来源清单、演示脚本、机制测试及本结果摘要。演示与录屏步骤见[案例演示](portfolio-demo-v1.md)。

离线核查已验证：冻结计划未漂移；重复execute在加载凭证前被拒绝；v2交付包验包通过；核查前后整棵执行目录文件hash不变。核查禁用了凭证加载、模型执行及socket，新请求0次。准备／打包的59项定向测试通过，使用注入评分的机制测试不计入真实质量指标。

下一项质量工作应核对后两题的逐句引用及评分器格式失败，先做离线诊断和人工审阅；不因本轮小样本自动修改默认策略。现阶段仍是Phase 3，工程机制和独立研究质量验收分别记录。

后续[零API诊断](portfolio-citation-diagnosis-20261123.md)已定位具体引用与摘录缺口，并导出两组空白人工复核包；尚未修订正文、重新评分或完成独立人工审核。

再后续的[单轮修订结果](portfolio-citation-repair-20261123.md)记录新增2次请求、6592返回Token及两题保留原稿；该独立实验未改变本页原批次成绩。
