# Public Beta Provider 契约

默认产品路径只通过 Local Bridge 调用用户自己的 Codex。公开安装包不要求用户配置额外的生成服务。

API Key 只允许在进程内和 Windows 凭据管理器使用，不得进入配置、任务、缓存或日志。

未来可以增加本地 Provider 扩展点，但当前没有实现 ComfyUI、ControlNet、OpenPose、Depth、CUDA 或本地模型下载。
