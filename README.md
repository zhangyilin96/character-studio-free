# Character Studio Beta 0.1

这是 Character Studio 的首轮免费公开测试版本，提供角色姿势迁移、一键换装、基础质量检查、结果保存和本地测试信息导出。

默认使用当前用户已经登录的 Codex。开始任务会消耗该账号的 Codex 使用额度；如果账号按 Token 或 Credits 计量，也可能产生相应消耗。

公开 Beta 固定使用低推理、一次生成、快速参考分析和快速候选检查。基础质量检查通过后立即交付；复杂风险、检查失败或无法判断时返回 `ADVANCED_CASE_NOT_SUPPORTED`，不会交付可能存在明显错误的候选图。

测试信息只保存在本地，不会自动上传用户图片或诊断数据。

本地模型尚未接入。当前不会调用 ComfyUI、Ollama、ControlNet 或本地 GPU 模型。

## 安装

电脑需要已经安装 Python 3.10 或更高版本。

普通用户：解压白名单包后，双击：

`Install Character Studio Beta.cmd`

安装程序会检测 Python、验证包内容、复制程序到独立应用目录，并创建桌面快捷方式；如果快捷方式创建失败，会显示可双击的备用启动器位置。安装失败时窗口会保留错误信息。

高级用户排错时，也可以在白名单包根目录运行：

```powershell
python scripts/install_studio.py --json
```

安装器会把白名单包复制到独立应用目录，不依赖下载目录继续存在。重复安装保持幂等，升级不会无提示删除用户输入、结果或任务信息。

当前双击安装仍依赖电脑已有 Python 3.10+。完全零依赖的安装体验需要后续制作包含 Python Runtime 的正式安装包。

## 许可证与素材权利

本项目以 **PolyForm Perimeter License 1.0.1** 作为 source-available Public Beta 发布，不宣称为 OSI Open Source。许可证允许非竞争性用途，但不允许使用本软件向他人提供与 Character Studio 竞争的产品或服务；完整、准确的授权条件以仓库根目录 `LICENSE` 正文为准，若本摘要与正文不一致，以 `LICENSE` 为准。

软件许可证只覆盖本仓库中的代码，并不会自动授予你对输入图片、角色设定、服装素材或其他第三方素材的权利。你需要确保自己拥有输入图片和素材所必需的使用权；生成结果的使用还必须遵守实际生成服务的适用条款。
