# Wheel 安装与交付检查

当前仍处于 Phase 3，研究质量与独立人工标签待验收。本轮补充可安装交付物验证，不新增 Agent 功能，不重复模型评分实验。

## 检查范围

此前 CI 主要验证 editable 安装和仓库内运行。新增 wheel 构建，并在全新虚拟环境安装锁定依赖和 wheel，然后将工作目录放到源码 checkout 之外执行 smoke。

`scripts/smoke_wheel.py` 必须由 wheel 所在环境运行，检查：

- 实际导入位于该 Python 环境中，且不在源码 checkout，拒绝 editable 安装。
- wheel 中所有应用 `.py` 文件与安装后的字节一致；包内不含 `.env`、runs、tests、Git 或本地虚拟环境目录。
- console script 与隔离模式模块入口的 `--help` 可用。
- 固定资料的七项机制演示通过，模型/外部请求数为0。
- 历史评分摘要能离线显示 invalid/unknown 和最终验收 pending，原 JSON 字节不变。
- 安装后的真实验收 CLI 子进程矩阵：短稿应用通过返回0、模型通过但超长返回3、同一超长稿显式模型口径返回0、旧批次缺检查返回3/unknown、被改动的长度缓存返回1且无验收结论。
- 安装后的独立正文检查 CLI 能拒绝213/200的固定超长正文；逐文件核对所有评测输入未变。
- 安装后的事实分类门槛 CLI 子进程矩阵：同文同输入分类不变返回0／passed，排除未覆盖事实而提高评分返回3／review_required，缺失评分返回3／unknown；校验 gate.json 与审计内容哈希绑定，人工验收保持pending。

机制和摘要模块以 Python `-I` 启动，忽略 PYTHONPATH/当前目录；Python audit hook 拒绝这些子进程中的 socket connect/DNS 事件。console help 在清理 PYTHONPATH/PYTHONHOME 的环境中运行。此检查不覆盖所有系统网络机制，不是操作系统沙箱；wheel 构建和依赖安装仍需联网下载。

验收矩阵同样使用 `-I`，每次启动独立进程，task_eval 与 evaluation_audit 子进程还将 dotenv 加载函数替换为明确拒绝。样例由注入的 runner、rubric 和证据标签创建：原有矩阵故意让模型口径全通过以暴露长度拦截；事实分类矩阵则保留完全相同的两行正文，将未覆盖行排除，使注入评分的任务通过率从0变成1，确认新门槛仍拦截。不会把 fixture 的 passed 当作真实模型或人工质量结论。

两组矩阵逐文件核对评测目录和数据集未变。事实分类回执另存于 smoke result.json 的 factuality_gates，标记 execution_mode=injected_offline、model_requests=0、search_requests=0，同时写入摘要。原有 evaluation_gates 字段及检查保留，继续使用已有 CI artifact 白名单归档；不上传fixture原始目录。

## 本地复现（PowerShell）

在仓库根目录运行。以下示例目录须不存在，且 wheel 环境与输出位于源码 checkout 之外：

```powershell
python -m pip wheel --no-deps --wheel-dir artifacts/wheel .
python -m venv ../researchdesk-wheel-env
../researchdesk-wheel-env/Scripts/python.exe -m pip install -r requirements.lock.txt
../researchdesk-wheel-env/Scripts/python.exe -m pip install --no-deps artifacts/wheel/researchdesk-0.1.0-py3-none-any.whl
../researchdesk-wheel-env/Scripts/python.exe -I -m pip check
../researchdesk-wheel-env/Scripts/python.exe -I scripts/smoke_wheel.py --wheel artifacts/wheel/researchdesk-0.1.0-py3-none-any.whl --output ../researchdesk-wheel-smoke
```

已有同名 wheel 时使用独立构建目录，避免选错产物；版本变化后调整文件名。此流程不发布 PyPI 或 GitHub Release。

2026-11-17 本地验证使用此前独立安装锁定依赖的 Python 3.11.6 环境，移除该环境中的 editable 项目后安装 wheel；主开发环境不变。6项 smoke 检查通过，46份应用 Python 文件与 wheel 字节一致。输出在本地 `C:/Users/pankesdsa/Documents/ChatGPT/project1/wheel-validation-20261117/`，未提交。本轮零模型/搜索调用，固定样例不代表真实研究质量通过。

## CI 交付物与限制

现有 Windows/Python3.11 CI 在全量测试后构建 wheel；新建无 system-site-packages 的虚拟环境，重新安装锁定依赖与 wheel，并运行同一 smoke。只归档 wheel 和其 `result.json`、`summary.md`，不上传整个环境或本地历史 runs，保留7天。

运行依赖按 requirements.lock.txt 安装；构建后端仍按 pyproject 的 hatchling 声明在隔离构建环境解析，尚未锁定构建工具链，不能声称 wheel 位级可复现。未验证全部 CLI 子命令、Linux/macOS 或其他 Python 版本；源码中的 evals、测试和文档仍通过仓库获取，本检查没有声称它们随 wheel 分发。

`status=passed` 只表示本次安装与固定机制检查通过，不说明模型密钥有效、真实研究成功、语义评分正确或已完成人工审核。
