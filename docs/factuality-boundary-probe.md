# 固定上下文：事实性边界开发探针

2026-11-20，使用现有 judge_stability.run_stability 对同一批6条构造案例做2次独立评分。保留 report-quote-v2 提示词，未改模型评分规则或默认策略。实际2次 SDK 请求，3975返回 Token，无搜索、工具请求或自动重试；两轮格式有效，事实性与引用标签均一致。

## 数据与冻结范围

公开数据位于 [factuality-boundary-dev-v1.json](../evals/factuality-boundary-dev-v1.json)。参考标签由 Agent 根据现有评分合同编写，label_status 明确为 agent_authored_candidate_references_not_human_gold；没有将此前聊天意见转换成这些案例的人工标签。

其中3条只陈述未完成核实／保留结论／建议，3条包含锁行为或资料范围的可核查陈述。事实行所引用的摘录来自已有 [PostgreSQL 18 项目摘记](../evals/fixtures/postgresql18-locks-selection-v1.md)，包含版本范围与冲突锁规则。来源是项目编写的局部中文摘记，不是本轮重新获取的官方网页快照。

经 benchmark_inputs 与 prepare 构建6个中性单元 ID 和3条有效证据；模型只收到 units 与 evidence。参考标签、类别说明及参考理由单独保存，不进入请求。两轮固定同一批次、顺序、证据、deepseek-flash、temperature=0、max_tokens=8192、timeout=90、max_retries=0、JSON输出与 thinking=disabled；供应商别名不等于后端模型快照固定。

请求前保存资料／提示词／输入及相关应用源码哈希，声明最多2次请求；失败也占预定轮次，不补轮。最终冻结计划位于本地 `runs/factuality-boundary-probes/d85faee979864d5989f893fcc0183ff1/`，原始输出不随仓库发布。早期含描述性 ID 的草案仅作准备，未执行请求。

## 两轮观察

| 单元 | 开发侧关注点 | 候选 factual | 第一轮 | 第二轮 |
| --- | --- | --- | --- | --- |
| case01 | 本报告尚未核实两版是否相同 | false | false | false |
| case02 | 根据目前资料无法确认两版完全相同 | false | false | false |
| case03 | 建议核对17版文档后再作结论 | false | false | false |
| case04 | 18版的冲突锁不能由不同事务同时持有 | true | true | true |
| case05 | 摘记范围为18，并保留对17版的结论 | true | true | true |
| case06 | 冲突锁事实作为建议的前提 | true | true | true |

三条有引用的事实行，两轮均将各自引用判为 supported。6个事实性判断的两轮一致率为1.0，3个引用判断的一致率为1.0；两轮各6个标签都与开发侧候选参考相同。这是候选参考符合情况，不是独立人工准确率，也不把两次批量请求中的12行当成12次独立实验。

| 轮次 | 输入 Token | 输出 Token | 返回总 Token | 评分耗时 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1644 | 348 | 1992 | 5.297秒 |
| 2 | 1644 | 339 | 1983 | 1.969秒 |
| 合计 | 3288 | 687 | 3975 | — |

用量均有返回；两轮缓存命中不同，不从耗时差异推断性能优势，金额未测量。编排入口再次执行时在加载凭证或创建模型前拒绝；离线核对计划与执行文件未变。该检查只证明这次一次性编排的重复执行拒绝，不是供应商端 exactly-once，也不新增通用恢复能力。

## 零 API 重建

在已安装项目的环境中可重建输入，参考标签与 payload 分开：

```python
import json
from pathlib import Path
from researchdesk.evaluation import benchmark_inputs
from researchdesk.verification import prepare

data = json.loads(Path('evals/factuality-boundary-dev-v1.json').read_text(encoding='utf-8'))
units, evidence, sources, candidate_references = benchmark_inputs(data)
payload, input_sha256 = prepare(units, evidence, sources)
assert len(payload['units']) == 6
assert all(e['integrity'] == 'valid' for e in payload['evidence'])
assert input_sha256 == '8351dbaedf07a1ddb081031dbf688543585f1a1b1c9db318c5476ef7eee08c05'
print(input_sha256)
```

这段代码不加载凭证或调用模型。重复付费实验可使用现有 run_stability API，须重新事前声明上限、保留新目录并固定同一提示词／设置；不要覆盖本次结果或把失败轮次补跑成通过。现有 judge_stability CLI 面向原评测完整输入，不直接接受此构造数据集，因此本轮只复用其 Python API，没有新增 CLI 或框架。

## 工程决定

本次小样本没有复现事实性分类分歧，支持继续保留当前评分合同；不能证明原报告中的分歧已解决。原案例有更长正文、其他任务、格式及不同证据上下文，本次是新的构造批次，没有改变这些变量做因果对照，也没有估计总体稳定性。

不修改或重算历史分数，不晋升策略，不认定 Phase 3 完成。下一步若调查原分歧，应复放原评测的完整冻结输入，或先获得实际人工复核，再事前设计更换上下文的受控比较；不能用本次6个容易构造案例替代真实报告质量验收。原8组复核仍 pending。
