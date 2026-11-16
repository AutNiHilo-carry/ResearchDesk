# 离线持续集成

配置：`.github/workflows/offline-ci.yml`。推送main、提交PR或手动触发后，在GitHub托管Windows环境使用Python 3.11检查依赖、运行全部pytest与零API机制演示。

该文件目前只在本地添加，尚未推送或在GitHub执行。本地验证不能替代实际托管Runner的安装和运行结果。推送前仓库还需包含源码、测试、evals固定资料、pyproject和依赖锁；只推工作流文件会缺少输入。

## 执行与结果

- 先安装requirements.lock.txt中的运行/测试依赖，再以no-deps安装本项目。运行依赖有锁定版本，构建隔离使用pyproject中的hatchling范围；不能声称构建环境完全逐字节可复现。
- pip check失败会使任务失败；pytest失败不会因随后演示通过而变成成功。若安装完成，演示仍会在测试失败后尝试执行，便于区分代码回归与演示问题。
- 最终保存JUnit和明确列出的固定演示结果，保留7天。不会整目录上传runs、.env、SQLite或真实研究报告。
- 工作流不注入API Secret，模型与Tavily环境变量为空，测试/演示使用本地固定资料或测试桩。依赖安装、Actions运行及上传结果本身仍需要网络，并可能消耗GitHub Actions额度；“零API”仅指不调用付费模型/搜索服务。
- 任务超时20分钟，同分支的新运行取消旧运行。contents仅read，checkout不保留凭证；这不是恶意仓库代码的通用网络沙箱。

Artifacts中先查看pytest.xml，再看summary.md。通过仅证明固定回归和演示机制，不代表研究质量或人工参考标签已验收，也不自动修改默认策略。

本地可使用项目虚拟环境运行相同测试与演示命令；演示目录必须全新。完整依赖安装和托管Actions服务尚需首个远程运行确认。当前阶段限制见[阶段验收记录](status-20261116.md)。

官方用法依据：[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[upload-artifact](https://github.com/actions/upload-artifact)。本配置使用官方当前示例中的v7 major标签；不声称Action代码已固定到不可变提交。


## 本地验证记录（2026-11-16）

工作流YAML结构检查通过；本地运行/测试依赖版本与锁一致，pip check通过。空API环境变量下507项pytest全部通过（142.74秒），零API演示的七项机制检查通过。未建立新的洁净Runner安装环境，未运行远程Actions，故依赖下载、托管环境与Artifact上传还需首次远程执行确认。

记录：runs/ci-local-validation-20261116.json；JUnit：runs/ci-local-tests-20261116.xml；演示：runs/ci-local-acceptance-20261116/summary.md。


## 全新虚拟环境验证（2026-11-16）

在独立Python 3.11.6虚拟环境按同一锁文件重新下载安装依赖，再no-deps安装项目；用户site禁用，旧项目虚拟环境不在导入路径。锁定依赖版本逐项匹配，pip check通过，507项测试全部通过、无失败/错误/跳过，零API机制演示通过。

这确认了本机新虚拟环境安装与运行，不等于洁净操作系统或GitHub托管Runner验证；远程Actions、包下载环境和Artifact上传仍未实际运行。构建后端仍使用pyproject的范围依赖，不声明逐字节可复现。

[本地新环境审计](../runs/ci-clean-validation-20261116/audit.json) · [JUnit](../runs/ci-clean-validation-20261116/pytest.xml) · [验收演示](../runs/ci-clean-validation-20261116/acceptance/summary.md)。依赖安装日志保留在任务专用环境目录，不随结果复制或上传。
