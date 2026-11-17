# 快速开始：先体验离线机制

已验证环境是Windows与Python 3.11。首次远程CI证据见[运行#1](https://github.com/AutNiHilo-carry/ResearchDesk/actions/runs/43837421928)；它不等于模型质量认证。

## 安装

在PowerShell中确认 `python --version` 为3.11.x，然后执行：

```powershell
git clone https://github.com/AutNiHilo-carry/ResearchDesk.git
cd ResearchDesk
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip check
```

此步骤需要从包仓库下载依赖。运行依赖有锁定版本；构建后端依赖范围见pyproject，不保证逐字节重建。

## 运行无需密钥的演示

```powershell
.\.venv\Scripts\python.exe -m researchdesk.acceptance_demo
```

命令自动创建新的 `runs/offline-acceptance/<ID>/`，打印summary.md路径。查看顺序：

1. summary.md：七项预期机制检查是否通过。
2. research/report.md：固定报告及证据台账。
3. fixture-quality.json：固定标签下的缺口定位。
4. result.json：预算、回执复用计数和演示范围。

预期为mechanism_checks_passed；质量覆盖率50%是故意保留的无引用事实，不是演示失败，也不是模型实测成绩。演示不加载.env、不需要API Key。它只验证应用SQLite关闭/重开的回执复用，不能替代LangGraph进程中断恢复测试。

若指定 `--output`，必须使用不存在的新目录；再次运行时不要删除失败现场来重用同一个目录。详见[演示合同](offline-acceptance.md)。

## 可选：运行全部测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前首轮远程CI通过507项测试；测试数量可能随后续代码变化。它们使用固定资料、测试桩和故障场景，不代表业务成功率。

## 可选：真实研究

复制.env.example到本地.env后填写密钥，配置存在性可用researchdesk --doctor检查。不要把真实密钥提交到Git。真实研究会调用配置的模型和搜索服务，请使用明确请求预算：

```powershell
.\.venv\Scripts\python.exe -m researchdesk.workflow "仅依据SQLite官方文档解释WAL是否支持多个写入者同时写入" --max-model-calls 10 --max-provider-requests 4 --max-tool-calls 12 --tool-retries 0
```

上限是请求次数，不是Token或金额上限。结果可能是needs_review；生成报告或返回门槛通过都不等于独立人工验收。更多恢复与选择语义见[工作流](checked-workflow.md)。

## 材料可用范围

源码、固定evals资料和测试随仓库发布，可按命令重建演示。真实网页历史运行、原始模型输出、审阅模板和本地审计在runs下被Git忽略，没有随首次提交公开。其他文档提到这些路径时应理解为本地实验索引，不能当成公开附件下载链接。

CI仅上传JUnit和选定的固定演示结果，保留7天；过期后可在新环境重新运行。它不打包完整历史runs。当前质量边界见[阶段验收](status-20261116.md)。

## 可安装交付检查

除 editable 开发安装外，现有 [wheel 安装检查](wheel-installation.md) 验证普通安装、源码目录外运行、固定机制演示与历史摘要。它不代表真实研究质量或人工验收完成。
