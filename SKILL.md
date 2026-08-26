---
name: character-consistency-pipeline
description: 用于图一角色图二参考姿势的身份锁定与角色一致性快速生成，也用于一键换装、基础质量检查，以及安装或启动本地 Character Studio Beta。
---

# Character Consistency Pipeline Public Beta

## 输入角色

- CHARACTER_REFERENCE：身份、脸、发型、身材、身体比例、服装和最终画风。
- POSE_REFERENCE：姿势、裁切、构图、遮挡、透视和支撑；不提供身份、服装或画风。
- OUTFIT_REFERENCE：一键换装时仅提供服装结构、材质、颜色、装饰和层次。
- 正式渲染只传预处理后的参考图；原图仅供本地任务使用。

## 工作流

- 姿势迁移：读取 [references/pose-transfer.md](references/pose-transfer.md)。
- 一键换装：读取 [references/outfit-transfer.md](references/outfit-transfer.md)。
- 交付检查：读取 [references/validation-policy.md](references/validation-policy.md)。

固定执行低推理强度、一次生成、quick analysis 和 quick observation。通过来源、安全与严格交付检查后立即 Early Exit。

复杂风险、检查失败或无法判断时返回 `ADVANCED_CASE_NOT_SUPPORTED`；不得执行完整分析、完整观察、修复或重试，也不得返回未通过交付门的候选图。

## 必须保护的规则

- 身份、脸、头发、身体比例和最终画风来自角色参考。
- 姿势图只提供空间结构；服装图只在换装任务中提供服装权威。
- API 密钥不得进入任务、缓存、日志或 Git。
- 测试信息只导出到用户本地，不自动上传图片或诊断数据。

默认路径是 Studio → Local Bridge → 用户自己的 Codex。本地模型尚未实现。安装与接口说明见 [references/studio-installation.md](references/studio-installation.md) 和 [references/provider-contract.md](references/provider-contract.md)。
