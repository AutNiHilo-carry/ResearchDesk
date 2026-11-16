# 推断判定的离线校准

最近的真实 U6 联调返回全正向标签，但理由声称“同版本仅由同日期推断”，忽略了两份来源及能力摘录里的 Atlas 2.0。程序不能凭结构合法或引用逐字存在证明理由正确。因此，标签一致率与理由正确性必须分开记录。

## 数据

evals/inference-calibration-v1.json 冻结8个虚构开发案例、4对正反例：相同/不同版本，保留/丢失部署条件，资料不足/产品绝对否定，正确/混用工具与任务分母。每对保留明确关联的事实前提，同时改变适用范围或结论。参考字段是项目自编候选，仍待独立人工复核，不是 gold truth 或 held-out benchmark。

evals/inference-live-regression-v1.json 原样归档真实 U6 请求 payload 和结构化模型返回，另存候选参考及 Agent 辅助诊断；模型原输出不覆盖。它专门证明：即使标签与候选参考全部相符，也不能由此接受理由或宣布整体质量达标。

## 离线入口

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inference_calibration prepare evals/inference-calibration-v1.json --output runs/inference-calibrations/<新目录>
.\.venv\Scripts\python.exe -m researchdesk.inference_calibration score runs/inference-calibrations/<目录> --judgments ./saved-output.json --output runs/inference-calibration-scores/<新目录>
```

两条命令均不读凭证、不调用 API。prepare 分开保存 model-input.json 与 references.json，参考标签、理由复核问题不进入模型输入；manifest 固定输入、参考、源码及原有判定提示词哈希。score 只消费已有的原始结构化输出，复用 inference-check-v1 的严格ID、逐字锚点与派生规则，不接受缺项或偷偷补标签，不修改原语义提示词。逻辑发生变更后，保留旧包并生成新包，不能静默给旧包套新口径。

标签指标是 candidate_field_agreement、candidate_case_agreement 和相对于候选参考的 false_support_count，不是经人工确认的准确率。结果固定 overall_quality=null、rationale_review=pending、human_review=pending、affects_original_scores=false；每例保留模型理由与单独的理由复核问题。输入发生变化拒绝评分，已有目录不覆盖，缺少输出不算零分也不算通过。

## 理由复核

实际复核者应分别检查：理由提到的版本/日期/套餐/条件是否在资料中，是否漏掉相反或限制性材料，是否把日期相同当成版本相同，是否把未提及当成不存在，文字保留是否与正向字段冲突。不要使用“只要出现某个词就失败”的规则代替语义判断。

本轮不建立自动的理由真实性判定，不给空白人工字段填值，也不因为多跑几遍能得到漂亮结果就选择其中一份。

## 单次批量真实校准

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inference_calibration prepare-execution runs/inference-calibrations/<新包> --model deepseek-flash --output runs/inference-calibration-plans/<新目录>
.\.venv\Scripts\python.exe -m researchdesk.inference_calibration execute runs/inference-calibration-plans/<计划目录>
```

prepare-execution 同样离线，不读取凭证。它固定模型、完整输入、源码及提示词、候选参考版本、temperature=0、timeout=90秒、max_retries=0、max_tokens=8192，总上限1次SDK调用。binding.json 校验整份请求，防止模型名等字段被意外替换；本地哈希是版本绑定，不是签名或权限证明。新计划必须引用当前逻辑下创建的包，旧实验与原始评分保留。

execute 将8例合并为一次批量请求，复用既有 ModelJudge，只把 model-input 内容发给配置的 DeepSeek API。参考标签与理由复核问题留在本地。执行会产生费用，1次请求不是Token或金额上限；SDK设置不保证服务端 exactly-once。

请求前先核验全部绑定、独占创建 execution 目录并记录 request_started。失败、超时、截断、中断或不确定结果不自动补请求；原目录不可重试。模型原始输出、未接受结构化输出、已知用量和异常类型保留，未知用量为null。收到返回后再校验绑定，资料或候选参考在调用中变化则拒绝接受结果，保留已消费请求的记录。

result.json 与 summary.md 分别保存标签差异、相对候选参考的误支持、模型理由与后续复核问题；整体质量仍未知，人工状态pending，不写原报告或改变默认策略。离线注入测试明确标记 injected_offline，不作为真实模型质量测量。
