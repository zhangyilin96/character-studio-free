# Character Studio Beta 0.1

这是 Character Studio 的首轮免费公开测试版本，提供角色姿势迁移、一键换装、基础质量检查、结果保存和本地测试信息导出。

> **未签名 Beta 提示：** 当前 Windows 安装包未进行代码签名，首次运行时可能显示“未知发布者”或“不常下载”提示。请仅从本项目官方 GitHub Release 或官方安装清单下载并核对 SHA-256；是否继续由你决定，请勿关闭或绕过 Windows 安全保护。

默认使用当前用户已经登录的 Codex。开始任务会消耗该账号的 Codex 使用额度；如果账号按 Token 或 Credits 计量，也可能产生相应消耗。

公开 Beta 固定使用低推理、一次生成、快速参考分析和快速候选检查。基础质量检查通过后立即交付；复杂风险、检查失败或无法判断时返回 `ADVANCED_CASE_NOT_SUPPORTED`，不会交付可能存在明显错误的候选图。

测试信息只保存在本地，不会自动上传用户图片或诊断数据。

本地模型尚未接入。当前不会调用 ComfyUI、Ollama、ControlNet 或本地 GPU 模型。

## 安装

普通用户不需要安装 Python、pip 或 Git，也不需要打开命令行。

下载官方 Release 中的：

`CharacterStudioBeta-Setup.exe`

双击后按当前用户安装。安装包内含固定版本的 Python 运行时和全部运行依赖，不使用系统 Python，不在安装时运行 pip，也不在线下载 Python 包。安装完成会自动启动，并创建桌面和开始菜单入口。

重复安装可用于修复或覆盖升级。程序文件安装在当前用户目录；`inputs`、`outputs`、`jobs`、`logs`、用户配置和本地历史与程序目录分离，升级和卸载不会默认删除这些数据。

如果你希望让自己的 Codex 自动完成下载、校验、安装和启动，请阅读 [让 Codex 自动安装](INSTALL_WITH_CODEX.md)。

Private 母仓库中的旧 `.cmd` 和 Python 安装脚本只保留给兼容测试，不进入 Public 白名单导出。

## 许可证与素材权利

本项目以 **PolyForm Perimeter License 1.0.1** 作为 source-available Public Beta 发布，不宣称为 OSI Open Source。许可证允许非竞争性用途，但不允许使用本软件向他人提供与 Character Studio 竞争的产品或服务；完整、准确的授权条件以仓库根目录 `LICENSE` 正文为准，若本摘要与正文不一致，以 `LICENSE` 为准。

软件许可证只覆盖本仓库中的代码，并不会自动授予你对输入图片、角色设定、服装素材或其他第三方素材的权利。你需要确保自己拥有输入图片和素材所必需的使用权；生成结果的使用还必须遵守实际生成服务的适用条款。
