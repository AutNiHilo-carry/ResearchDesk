# 原报告完整批次：两次固定输入复放

2026-11-20，继[六条构造案例探针](factuality-boundary-probe.md)之后，对已有 v1 两题报告的原始完整引用评分输入做2次独立评分。实际2次 SDK 请求、6529返回 Token；两轮有效，事实性与引用标签均彼此一致，且与历史保存标签相同。不能据此确认标签正确，也没有解决跨报告的事实性分类争议。

## 输入与控制

来源为[计数合同两题对照](counting-contract-pilot.md)的 `runs/generation-contract-plans/5746f5fdbf8547d3abe02f453d26ffd4/execution/v1/`，包含 version_scope 与 wal_default_claim 两份完整报告，共12行、8条引用证据。保留所有标题、格式、未引用行和批次顺序，没有抽出争议句单独评分。

先用 saved_quality 确定性复算历史标签与指标，并用 build_packet 核对任务集、冻结语料、报告和证据，再经 prepare 重建输入；哈希与原评分的 quality_input_sha256 完全相同：

`a82af3b777b7a0bf35fe931f0cc03836a65c7f628bd5815668f24d35579e0a7c`

固定原 deepseek-flash 别名、report-quote-v2 Prompt、temperature=0、max_tokens=8192、timeout=90、max_retries=0、JSON输出和 thinking=disabled。历史标签不进入评分请求。请求前冻结原目录逐文件哈希、资料、Prompt、相关源码及最多2次请求；两轮是预定独立重复，失败不补轮。

本地计划与结果位于 `runs/original-batch-stability/74f14ecdc8dd438a97cfc85ed4e98b60/`，包括 input、原评分副本、两轮输出及 historical-comparison.json；原始运行数据不公开提交。本次复用现有 run_stability Python API，没有修改运行时代码。

## 观察

| 项目 | 新轮次之间 | 各新轮次与历史标签 |
| --- | --- | --- |
| 12条事实性标签 | 12/12相同 | 各12/12相同 |
| 8条引用标签 | 8/8相同 | 各8/8相同 |
| 有效评分轮次 | 2/2 | 历史结果单列，不并入新轮次统计 |

比较的是 factual 和引用 verdict，不要求模型理由文字逐字相同。以下争议行在历史与两次新评分中均被判为 factual=true：

- version_scope:U1：“结论：不能确认17版所有锁行为与18版完全相同。”（原文保留加粗格式）
- version_scope:U4：`故“全部相同”仅为推断，17版未核实；建议逐项比对两版锁章节后再定论。`
- wal_default_claim:U8：资料未固定 SQLite 发布版本，因此不保证适用于所有版本的范围说明。

两轮引用指标均与历史结果一致：版本题精度1.0、覆盖率0.5；WAL题精度4/6、覆盖率0.8。它们只是引用评分，没有重新生成报告、调用业务评分器、颁发新的任务验收结果或改变应用门槛。

| 轮次 | 输入 Token | 输出 Token | 返回总 Token | 评分耗时 |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2565 | 661 | 3226 | 4.906秒 |
| 2 | 2565 | 738 | 3303 | 2.969秒 |
| 合计 | 5130 | 1399 | 6529 | — |

无新增搜索、工具请求、自动重试或报告生成，金额未测量。历史实验用量未再次计入。原目录全部文件哈希保持不变；离线核对两轮输入、标签确定性复算及汇总一致，重复编排在加载凭证／构建模型前拒绝，计划与结果未变。这不是服务商 exactly-once 或进程强杀恢复实验。

## 零 API 核验入口

拥有本地原运行目录时，可通过以下代码重建完整评分输入，不加载凭证或请求模型：

```python
import json
from pathlib import Path
from researchdesk.review import build_packet
from researchdesk.task_eval import saved_quality
from researchdesk.verification import prepare

evaluation = Path('runs/generation-contract-plans/5746f5fdbf8547d3abe02f453d26ffd4/execution/v1')
dataset = Path('evals/technical-selection-v1.json')
result = json.loads((evaluation / 'result.json').read_text(encoding='utf-8'))
quality, binding = saved_quality(evaluation, dataset, result)
packet = build_packet(evaluation, dataset)
units, evidence, sources = [], {}, {}
for task in packet['tasks']:
    units.extend(task.get('units', []))
    evidence.update(task.get('evidence', {}))
    sources.update(task.get('sources', {}))
payload, input_sha256 = prepare(units, evidence, sources)
assert input_sha256 == binding['input_sha256'] == result['quality_input_sha256']
assert len(payload['units']) == 12 and len(payload['evidence']) == 8
```

此批次使用 body-length-v1 生成合同，而当前 judge_stability CLI 的 frozen_input 复用 legacy 配对修订校验，会拒绝该生成后缀；因此本轮采用上述现有绑定校验与 run_stability API，没有放宽原配对修订限制。重复付费复放应另行冻结输入、设置、次数及新目录，不能覆盖本次记录。

## 解释与后续边界

这两轮没有观察到相同输入的标签波动，不能估计总体方差或证明评分器稳定正确。构造探针中的保留结论被标为非事实，原批次中的近义结论则仍为事实；两批有文字、格式、证据和上下文差异，尚不能确定哪一个因素造成变化。模型别名相同也不证明历史与当前后端快照一致。

这是看到争议后选择的开发批次，不是未见评测集，没有独立人工参考。保留新旧标签及差异为零的结果，不选择更漂亮的一轮替换历史，也不按多数表决建立真值；human_review 和 final_acceptance 继续 pending。

下一步如需定位原因，应事前声明只改变哪一个因素及请求上限，其他条件固定，再做对照；不能从本次结果直接宣称“加粗导致误判”或“完整上下文一定更差”。在没有质量依据前，默认提示词与策略保持不变。
