# 正式质量检查的分句策略选择

质量检查入口新增verify专用参数 `--clause-policy`，可选clause-spans-v1/v2/v3。默认行为不变：默认逐行，指定分句但不指定版本时仍为v1。v2/v3尚未晋升默认策略。

```powershell
python -m researchdesk.quality verify RUN_ID --output runs --granularity clause --clause-policy clause-spans-v3 --judge-prompt v2
```

注意：这是实际模型评分命令，正常执行会调用配置的模型一次，SDK重试0；本轮开发仅用离线测试桩验证，未执行付费评分。

只有 `--granularity clause` 可以指定clause-policy，v2/v3还必须显式指定judge-prompt v2，使评分器使用原文位置和父上下文的约定。不支持的参数组合在加载环境和创建模型之前拒绝；分句后的单位数和字符预算在模型创建前检查，不静默截断。

每次验证结果是独立目录。result.json与summary.md记录实际分句版本；input.json在请求模型前保存精确评分输入，result中的input_sha256按规范JSON序列化绑定其内容。不同版本具有不同片段元数据与输入指纹，不能静默复用旧分句标签。模型调用失败时保留冻结输入，结果无有效指标，不自动补跑。

该入口只运行一次显式检查，不改原报告、研究工作流、修订选择、旧评测或门槛；现有benchmark和三组comparison实验保持原合同。默认逐行评分Prompt及分句v1合同均不改变。

input.json包含报告和证据摘录，是研究数据，不是脱敏元数据。新增文件用于可追溯性，未改变传给模型的数据范围。运行前需确认这些内容适合发送给配置的模型。

分句版本与模型评分Prompt版本是两个不同维度。覆盖率分母和引用绑定可能变化，应比较具体片段及来源范围，不能凭更高覆盖率认定报告质量提高。返回码0仍只表示评分流程完成，不代表人工审核或任务质量验收通过。
