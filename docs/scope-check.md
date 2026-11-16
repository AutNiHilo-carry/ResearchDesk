# 作用域语义检查合同v1

`scope-check-v1`是显式启用的模型结果导入合同，分别检查整行声明完整性、断言条件保留和带条件断言的证据支持。不修改原有引用评分、默认策略或人工标签。

```powershell
python -m researchdesk.scope_check prepare RUN --trace TRACE.json --output NEW_INPUT_DIR
python -m researchdesk.scope_check import RUN --trace TRACE.json --judgment REVIEW_ENVELOPE.json --output NEW_RESULT_DIR
```

两个命令均离线，不加载密钥、不调用模型。prepare保存版本化Prompt、完整输入和哈希；import重新检查当前报告、语料和侧车，校验结果绑定后导入。实际模型执行需单独授权、固定模型与调用预算，当前没有自动请求或补跑入口。

## 判断维度

- 每个原始报告行必须有一个declarations判断：complete/incomplete/uncertain。评分器要检查包括context/nonfactual在内的所有原文，寻找遗漏的事实或条件，不能只看已声明断言。
- 每个assertion必须有一个conditions判断：preserved/missing/unnecessary/uncertain。
- 每个assertion必须有一个support判断：supported/partial/contradicted/insufficient，针对断言与其链接条件共同判断。
- 每个绑定证据ID必须恰有一个quote锚点，非空引用必须逐字属于证据摘录；supported至少需要一个非空锚点。锚点匹配不证明语义支持。

结果信封必须恰有payload_sha256、prompt_sha256、judgment三个键，前两个对应prepare结果。judgment包括parents与assertions，精确字段见scope_check.PROMPT及严格Schema。错误/重复/缺失ID、虚构引文、空白理由、错误哈希直接拒绝，不产生部分有效分数。

原文位置与条件/引用结构依旧先由scope_trace检查。所在行任何结构待处理项都会阻断该断言的model_support_candidate，包括未分类、孤立条件和引用作用域未解决。模型全正面不能覆盖这些阻断项。

model_support_candidate仅为正面的模型候选结论；human_review始终pending，affects_original_scores为false，promotion不执行。不输出覆盖率或将该结论换算为旧门槛成绩。

## 限制与验证状态

本轮20个新增测试使用离线测试桩，覆盖正面合同、遗漏条件、过度声明、证据不足、ID/引用/哈希错误、孤立条件与候选池阻断。它们验证输入输出合同和控制逻辑，不证明模型能识别语义错误，尚未有本合同真实模型质量实验或独立人工gold。

模型仍可能错误地将遗漏条件判为complete；把所有内容声明成非事实也需模型或人工审查。当前输入只提供已引用证据摘录，缺少更广上下文时应保留uncertain/insufficient。哈希只绑定声明的输入，不证明模型实际处理过该输入，也不是提供方执行证明。

数据包包含完整报告文本和证据摘录，不属于脱敏元数据。真实报告仅生成输入包供审阅，没有外发或伪造模型结果；准备路径为runs/report-policy-live/wal-v2-20261115/scope-check-input-v1。侧车仍为未分类模板，因此不能据此报告语义检查通过。


## JSON请求格式修正（2026-11-16）

首轮虚构资料试验以OpenAIInvalidRequestError结束，未产生有效语义结果，Token用量未返回，未重试。检查发现v1 Prompt没有json字样；DeepSeek官方JSON Output说明要求system或user包含该指令。因为未记录服务端详细错误，不能认定这是该错误的唯一原因。

原v1 Prompt和旧输入哈希保持不变。新增显式 `--judge-prompt v2`，仅追加严格JSON输出指令；prepare结果记录judge_prompt_version及新哈希。import也必须指定同版本，v1/v2结果不可交叉导入。默认仍v1以保持离线历史回放兼容；后续JSON模式执行应显式选择v2并核对请求格式。

```powershell
python -m researchdesk.scope_check prepare RUN --trace TRACE.json --judge-prompt v2 --output NEW_V2_INPUT_DIR
python -m researchdesk.scope_check import RUN --trace TRACE.json --judgment V2_REVIEW.json --judge-prompt v2 --output NEW_V2_RESULT_DIR
```

本轮仅准备v2新请求包，没有补发模型请求，不能宣称端到端已修复。失败记录与虚构资料位于runs/scope-check-evals/synthetic-20261116，详细说明见该目录findings.md。

官方依据：https://api-docs.deepseek.com/zh-cn/guides/json_mode/


## 声明ID边界修正（2026-11-16）

v2单次虚构资料调用成功返回JSON，但模型把role=nonfactual的A5新增到assertions结果中；预期5个声明断言，返回6个，完整性校验拒绝全部结果。保留未接受输出，不过滤A5后重算、不把其中的判断当作有效评分。

新增显式judge-prompt v3，强调误分类只能在父级declarations中报告，断言结果ID严格限于role=assertion；同时区分遗漏条件与新增但无证据的能力。v1/v2原Prompt保持不变；v3单独绑定新哈希，默认仍v1。可在prepare/import命令显式使用--judge-prompt v3。

ID失败诊断新增预期/实际、缺失/额外/重复数量，不输出任意原文或提供方错误正文。v3本轮只做离线测试及请求准备，未调用模型，不能宣称新Prompt已通过真实验证。实验目录：runs/scope-check-evals/synthetic-v2-20261116。


## v3真实先导验证（2026-11-16）

v3在相同六个虚构案例上以一次请求完成严格导入，6个父级/5个声明断言，实际2,714 Token。A1成为模型支持候选；A2/A3条件缺失、A4证据不足、A5误分类、A6候选池均未被放行。结果离线重放一致，旧失败保留。

该结果仅证明本合同在这组开发案例上跑通，尚无独立留出集或真实报告语义验收；默认仍不变。详见runs/scope-check-evals/synthetic-v3-20261116/findings.md。
