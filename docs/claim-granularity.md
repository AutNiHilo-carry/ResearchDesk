# Source-anchored Claim Candidates（Phase 3 改进）

## 改动
增加可选的保守分句策略和 v2 judge Prompt，保留 line/v1 默认行为及原始 evidence-v1 数据集。
本版不是完备的原子 claim 抽取：只处理明确的句号、分号等边界，不让模型改写断言。
“系统支持日志和自动备份”这类无分隔符的多结论暂不拆分，避免凭空补写主语或改变语义。

每个候选保存 parent_id、原始行号、source_line_text、span_start / span_end。
offset 是相对 source_line_text 的 Python Unicode 码点索引，末端不包含。
source_line_text 是去掉行首尾空白后的原行，offset 不是 UTF-16 或原文件字节位置。
校验每个片段逐字对应、无重叠/遗漏、所有父行都存在，并保留原报告不变。

URL、小数、行内代码、括号、引号中的标点不作为拆分边界。
检测到条件限定时保留整行，以免把条件与结果拆散。规则可能保守地漏拆。
不通过关键词或章节标题强制把“未验证”标成非事实；仍由 judge 判断语义。
v2 Prompt 增加限制声明、建议中事实、版本范围等边界的规则。

## 引用范围
片段带显式引用时，按原片段绑定；不会把前一片段的显式引用传给未引用的后文。
所有引用都集中在父行最后一个片段时，各片段继承父行引用池，
标记 parent_final_candidate_pool，并保留 direct_citations。
这是候选关系，不能证明作者把每个引用都赋给了每个子句；需人工复核。
因此 clause 模式中的 Citation Precision 是候选配对支持率，
不能和 line 模式的字面引用指标直接视为同一统计口径。

## 命令
```powershell
# 候选策略：可定位分句 + v2 judge
.\.venv\Scripts\python.exe -m researchdesk.quality verify 9e359ea792374eed84afb6c74b4569b9 --granularity clause --judge-prompt v2

# 保留的旧行为
.\.venv\Scripts\python.exe -m researchdesk.quality verify 9e359ea792374eed84afb6c74b4569b9 --granularity line --judge-prompt v1

# 同一回归集、同一模型/参数，三组对照；共最多三次模型调用
.\.venv\Scripts\python.exe -m researchdesk.quality compare

# 用旧数据集检查 v2 Prompt 的回归
.\.venv\Scripts\python.exe -m researchdesk.quality benchmark --judge-prompt v2
```

compare 在 runs/comparisons/<check_id>/ 另存 JSON 和 Markdown。
每组记录 Prompt 版本、unitization 版本、输入哈希、模型用量、耗时和参考标签差异。
所有组预算和锚点校验都在第一次付费调用之前完成。
默认行为不自动切换；compare 返回 0 只说明候选通过小型参考集门槛，不是自动发布。
每组完成后立即保存独立 JSON。某组 schema/解析失败则保留拒绝的模型 JSON、字段位置和错误类型，
该组 metrics/benchmark 为 null，其他组独立继续；整个比较标 partial 并返回 3，不能当完整对照。
不会填补缺失字段或将该组的部分单元拿来计分，也不会自动重试付费请求。
诊断只保存生成的 JSON/文本，不保存隐藏推理或 HTTP 响应体。

## 三组实验
1. baseline_line_v1
2. prompt_only_line_v2：与第一组输入完全相同，只改 Prompt。
3. candidate_clause_v2：与第二组 Prompt 相同，改分句及引用绑定策略。

不把 Prompt 和粒度同时变化后的差别全归因于其中一个因素。
第二、三组参考单元和引用配对数量不同：
不能因为 coverage 上升就说报告质量提高，必须看具体断言、参考标签和统计分母。

evals/claims-v2.json 是 13 个固定回归场景，包含已发现的限制声明误分类、
免责声明中仍包含事实、多结论、条件作用域、版本、URL 与引号等。
这些数据用于指导本次改动，非独立 held-out 集，也未独立人工复核。
旧 evals/evidence-v1.json 内容不变。标签不会发送给 judge。
离线测试只验证分句/计分/控制变量实现，真实语义效果另有实际模型实验。

## 结果阅读
summary.md 显示需要证据的片段，也显式列出被判为 nonfactual 的排除项，
便于发现模型通过错误排除事实而提高 coverage 的问题。
数字不是事实认证；完整研究任务成功率、标注者一致性、泛化能力仍未评测。
下一步应复核争议样例，并在独立研究任务集上验证，而不是继续针对同一小集调分数。

