# 离线持续集成

配置：`.github/workflows/offline-ci.yml`。推送main、提交PR或手动触发后，在GitHub托管Windows环境使用Python 3.11检查依赖、运行全部pytest与零API机制演示。

## 已确认的远程结果

[首次运行#1](https://github.com/AutNiHilo-carry/ResearchDesk/actions/runs/43837421928)在提交`30ae839`上成功：

- 锁文件依赖安装、项目安装和pip check通过。
- 507项测试通过，pytest报告耗时32.08秒。
- 固定资料机制演示通过。
- 产物offline-verification-windows-py311上传成功。

该结果只对应这一提交及该次托管环境，不保证未来依赖下载和运行稳定，也不证明研究质量或人工参考标签已验收。[全部运行](https://github.com/AutNiHilo-carry/ResearchDesk/actions/workflows/offline-ci.yml)可查看后续状态。

## 执行与归档

先安装requirements.lock.txt，再以no-deps安装本项目。运行依赖有锁定版本，构建隔离仍使用pyproject中的hatchling范围；不是逐字节可复现构建。

pytest失败不会因随后演示通过而使任务成功。若项目安装成功，测试失败后仍尝试演示，便于区分回归与演示问题。任务超时20分钟，同分支新运行会取消旧运行。

不注入模型或搜索Secret，两项API环境变量为空。测试与演示使用固定资料/测试桩；包下载、Actions服务及归档仍需要网络，并可能消耗Actions额度。“零API”仅指不调用付费模型/搜索服务。

contents权限为read，checkout不保留凭证。此配置不是执行恶意仓库代码的通用网络沙箱。Artifact只包含JUnit和明确列出的固定演示结果，保留7天；不上传.env、SQLite或完整runs。

## 复现与本地记录

复现步骤见[快速开始](quickstart.md)。本地已有环境通过507项测试；另一个独立Python 3.11.6虚拟环境重新安装70项锁定依赖、安装项目、pip check、507项回归和演示也全部通过。后者禁用了用户site且未导入旧项目虚拟环境，但不代表全新操作系统。

本地记录（不随仓库发布）：

- `runs/ci-local-validation-20261116.json`
- `runs/ci-clean-validation-20261116/audit.json`
- `runs/publication-20261116.json`

原始安装日志及虚拟环境不随结果复制或上传。当前阶段限制见[阶段验收](status-20261116.md)。

官方用法：[checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[upload-artifact](https://github.com/actions/upload-artifact)。使用v7 major标签，不声称Action已固定到不可变提交。
