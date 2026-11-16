# 持久化模型调用次数预算

有预算的报告修订另有[尾部验证预留](tail-verification-budget.md)：预留不是已发请求，但会限制生成可用额度；候选验证使用它后，失败或未知结果照常占额度。总额度不变。

工具调用预算不限制模型请求数量。本功能在共享模型实例的同步回调层增加应用侧准入：调用供应商handler前，先以SQLite事务预留一次额度。主调用与当前已安装Deep Agents的自动摘要调用都经过该模型实例；摘要触发、压缩及重试仍由框架负责。它是ResearchDesk的请求策略，不是重写模型runtime，也不是Token、金额或全局时长预算。

```powershell
.\.venv\Scripts\researchdesk.exe "研究问题" --local-only --document ./notes.md --max-model-calls 12
# 恢复只使用保存的额度，不能另传上限
.\.venv\Scripts\researchdesk.exe --resume <RunID>
```

--max-model-calls允许0到1000，0用于验证请求前拦截。未指定时保持现有行为（仍有框架步数与模型超时限制），不自动给历史运行假造预算。demo不能配置模型预算。

模型遥测也统一使用该回调层，即使没有配置额度也能观测共享模型发出的摘要请求；未启用额度时只观测，不执行准入限制。既有日志不会被重写，也不会凭空补齐历史摘要用量。

## 预留与恢复语义

每个请求使用独立ID，事务检查总预留数小于上限后才写入reservation。并发调用也不能同时占用最后一个额度。相同请求ID不能用来再次发送远程请求；框架恢复后若需要重发模型调用，必须占用新的额度。

预留后状态为reserved，收到结果后更新为completed，调用异常标记failed。三种状态都占额度：超时不证明远端没有处理或计费，进程在预留后退出也不退款。预留成功但尚未真正发送就被kill，可能浪费一个名额；这是避免不确定请求导致超额调用的保守取舍。

上限与请求表保存在application.sqlite3的可选model_call_policy/model_call_reservations表中，不改变已有工具计数器。原user_version=1资料和回执结构保持兼容；未启用的运行不创建额外预算表。旧运行已有model_started事件时，不允许通过补一份manifest给历史调用重新分配一个空预算。

恢复校验manifest与SQLite保存的上限一致，不能通过CLI换额度、改上限或删manifest策略来清零。这里是应用不变量校验，不声称能防御有权直接改写数据库的攻击者。

## 修订与评分范围

修订fork复制原应用数据库，因此复制已消耗额度；候选生成、框架摘要与AuditedJudge的修订验证共享候选运行剩余预算，父运行不因候选消耗而变化。generation角色包含原生成与修订生成，context_summarization记录框架摘要，repair_verification记录修订验证。原稿已有冻结验证结果而不发请求时不占新额度。

批次TaskJudge、独立quality CLI或judge_stability等实验评分器不是某个研究运行的请求，不计入该运行预算；它们继续按各自的调用次数上限与Token统计单独记录。不能把一个运行的额度当作整个实验或账号总费用上限。

模型输出成功但预算终态/审计事件尚未提交、或响应在网络中丢失的窗口，仍可能存在未知用量。预算不退款，但Token总量仍按现有事件完整性判断，不因为占了一个名额就编造精确Token数。不保证远程请求exactly-once。本轮只支持当前同步研究路径；未来更换框架、独立摘要模型、SubAgent、缓存或异步执行前需重新验证覆盖范围。模型缓存命中也可能占准入名额，额度是保守的执行准入数，不是供应商计费次数。

## 耗尽后的行为

请求前拒绝会写model_budget_denied事件，不写model_started，不调用模型handler。普通研究CLI以4退出，run状态为budget_exhausted；保留证据、回执和checkpoint，不生成一份伪造的“完成报告”。再次resume仍受相同上限约束。

修订验证若在准入阶段被拒绝，其远程调用并未开始，不标成verification_outcome_uncertain。真正已经发出但结果未持久化的验证仍维持原来的不确定结果保护。

```powershell
.\.venv\Scripts\python.exe -m researchdesk.inspect_run <运行目录>
```

诊断会显示预留数、上限、剩余数、角色/状态分布、额度耗尽和未记录终态的请求。reserved不等于请求已收费，completed也不等于供应商用量元数据完整。

## 验证边界

离线测试覆盖并发准入、失败/未知请求不退款、真实进程终止后的持久化、正常CLI/Deep Agents图的耗尽与恢复、修订复制预算及拒绝验证不冒充未知响应。使用脚本模型计数，不把它当作真实研究质量评测。额外的零额度CLI探针使用实际模型适配器初始化，但handler在请求前被阻断，不为验证拦截新增付费调用。
