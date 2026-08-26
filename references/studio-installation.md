# Public Beta Studio 安装

安装器只接受带有效 `free-export-manifest.json` 和 manifest SHA-256 校验文件的 Public Beta 白名单包，并将程序文件复制到独立版本目录。

普通用户双击包根目录的 `Install Character Studio Beta.cmd`。入口会检测 `py` 或 Python 3.10+，显示清楚的成功或失败原因，不要求管理员权限。高级用户仍可直接运行 `python scripts/install_studio.py --json` 排错。

配置固定写入 `distribution_mode = public_beta`，首次启动直接进入工作室。重复安装保持幂等；升级或卸载不得无提示删除 `inputs`、`outputs`、`jobs` 或 `cache`。

当前双击安装仍要求电脑已有 Python 3.10+。完全零依赖安装需要后续制作包含 Python Runtime 的正式安装包。
