#!/usr/bin/env python3
"""为当前 Windows 用户幂等安装角色一致性工作室。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import textwrap
import venv


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from character_workflow.versioning import VERSIONS  # noqa: E402
from config.public_product import DISTRIBUTION_PUBLIC_BETA  # noqa: E402
from scripts.validate_free_package import validate as validate_public_package  # noqa: E402

APP_NAME = "Character Studio Beta"
CONFIG_NAME = "studio-config.json"
MARKER_NAME = "dependency-state.json"
PACKAGE_MANIFEST = "free-export-manifest.json"
PACKAGE_MANIFEST_SHA = "free-export-manifest.sha256"


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstallResult:
    app_root: Path
    package_root: Path
    config_path: Path
    launcher_path: Path
    shortcut_path: Path | None
    shortcut_error: str | None
    dependencies_changed: bool


def default_app_root() -> Path:
    base = os.getenv("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "CharacterConsistencyStudio"


def _logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"character_consistency_studio_installer:{path}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_package_manifest(package_root: Path, *, repository_worktree: bool = False) -> dict:
    """Verify an explicit-whitelist Public Beta package before installing it."""
    package_root = package_root.resolve()
    validation_errors = validate_public_package(
        package_root,
        repository_worktree=repository_worktree,
    )
    if validation_errors:
        raise InstallError("Public Beta 包验证失败：" + validation_errors[0])
    manifest_path = package_root / PACKAGE_MANIFEST
    manifest_sha_path = package_root / PACKAGE_MANIFEST_SHA
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallError("安装源不是有效的 Public Beta 白名单包。请先运行导出器。") from exc
    try:
        expected_manifest_sha = manifest_sha_path.read_text(encoding="ascii").strip().split()[0].casefold()
    except (OSError, UnicodeError, IndexError) as exc:
        raise InstallError("Public Beta 包缺少 manifest SHA-256 校验文件。") from exc
    if expected_manifest_sha != _sha256(manifest_path):
        raise InstallError("Public Beta 包 manifest SHA-256 校验失败。")
    if (
        manifest.get("package") != "character-studio-public-beta"
        or manifest.get("distribution_mode") != DISTRIBUTION_PUBLIC_BETA
        or manifest.get("source_policy") != "explicit_whitelist"
    ):
        raise InstallError("Public Beta 包 manifest 的包名、发行模式或导出策略无效。")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise InstallError("Public Beta 包 manifest 没有文件清单。")
    forbidden = {"tests", ".git", "inputs", "outputs", "cache", "tmp", "logs"}
    expected_paths: set[str] = set()
    for record in files:
        if not isinstance(record, dict):
            raise InstallError("Public Beta 包 manifest 含无效记录。")
        relative = Path(str(record.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts or any(part in forbidden for part in relative.parts):
            raise InstallError(f"Public Beta 包 manifest 含禁止路径：{relative}")
        expected_paths.add(relative.as_posix())
        source = (package_root / relative).resolve()
        if package_root not in source.parents or not source.is_file():
            raise InstallError(f"Public Beta 包文件不存在或越界：{relative}")
        if source.stat().st_size != record.get("size") or _sha256(source) != record.get("sha256"):
            raise InstallError(f"Public Beta 包文件校验失败：{relative}")
    actual_paths = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file()
        and path.name not in {PACKAGE_MANIFEST, PACKAGE_MANIFEST_SHA}
        and "__pycache__" not in path.relative_to(package_root).parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
        and not (repository_worktree and ".git" in path.relative_to(package_root).parts)
    }
    if expected_paths != actual_paths:
        raise InstallError("Public Beta 包文件集合与 manifest 不一致。")
    return manifest


def package_identity(package_root: Path, manifest: dict) -> tuple[str, str]:
    manifest_sha = _sha256(package_root / PACKAGE_MANIFEST)
    version = str(manifest.get("versions", {}).get("product") or "unknown")
    return version, manifest_sha


def install_package_source(app_root: Path, package_root: Path, manifest: dict) -> Path:
    """Copy only manifest-listed application files; user data lives outside this directory."""
    version, manifest_sha = package_identity(package_root, manifest)
    applications = (app_root / "application").resolve()
    applications.mkdir(parents=True, exist_ok=True)
    destination = (applications / f"public-beta-{version}-{manifest_sha[:12]}").resolve()
    if applications not in destination.parents:
        raise InstallError("拒绝写入应用目录之外的安装目标。")
    complete_marker = destination / PACKAGE_MANIFEST
    if complete_marker.is_file() and _sha256(complete_marker) == manifest_sha:
        return destination
    if destination.exists():
        # 只替换经验证位于 application 下的程序文件，不碰用户输入、结果和任务目录。
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for record in manifest["files"]:
        relative = Path(record["path"])
        source = package_root / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(package_root / PACKAGE_MANIFEST, complete_marker)
    shutil.copy2(package_root / PACKAGE_MANIFEST_SHA, destination / PACKAGE_MANIFEST_SHA)
    return destination


def requirements_hash(package_root: Path) -> str:
    return _sha256(package_root / "studio" / "requirements.txt")


def venv_paths(app_root: Path) -> tuple[Path, Path]:
    root = app_root / ".venv"
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "pythonw.exe"
    return root / "bin" / "python", root / "bin" / "python"


def dependency_smoke(python_executable: Path) -> bool:
    if not python_executable.is_file():
        return False
    result = subprocess.run(
        [str(python_executable), "-c", "import gradio, PIL; assert int(gradio.__version__.split('.')[0]) == 6"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def dependencies_current(app_root: Path, package_root: Path) -> bool:
    python_executable, _pythonw = venv_paths(app_root)
    marker = app_root / MARKER_NAME
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return state.get("requirements_sha256") == requirements_hash(package_root) and dependency_smoke(python_executable)


def ensure_runtime(app_root: Path, package_root: Path, logger: logging.Logger) -> bool:
    python_executable, pythonw = venv_paths(app_root)
    changed = False
    if not python_executable.is_file() or not pythonw.is_file():
        logger.info("creating isolated runtime source=%s version=%s", sys.executable, platform.python_version())
        venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(app_root / ".venv")
        changed = True
    if dependencies_current(app_root, package_root):
        logger.info("dependency status=current")
        return changed
    requirements = package_root / "studio" / "requirements.txt"
    logger.info("dependency status=install_or_repair requirements_sha256=%s", requirements_hash(package_root))
    command = [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--requirement",
        str(requirements),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
        check=False,
    )
    if result.returncode != 0:
        logger.error("dependency installation failed exit_code=%s", result.returncode)
        raise InstallError("无法安装工作室依赖。详细原因请查看 logs/studio-install.log。")
    if not dependency_smoke(python_executable):
        raise InstallError("安装完成后的依赖检查未通过。")
    (app_root / MARKER_NAME).write_text(
        json.dumps(
            {
                "requirements_sha256": requirements_hash(package_root),
                "python": str(python_executable),
                "python_version": platform.python_version(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    logger.info("dependency installation complete")
    return True


def write_config(app_root: Path, package_root: Path) -> Path:
    python_executable, pythonw = venv_paths(app_root)
    config = {
        "schema_version": VERSIONS.studio_interface,
        "product_versions": VERSIONS.as_dict(),
        "distribution_mode": DISTRIBUTION_PUBLIC_BETA,
        "app_name": APP_NAME,
        "app_root": str(app_root.resolve()),
        "skill_root": str(package_root.resolve()),
        "python": str(python_executable.resolve()),
        "pythonw": str(pythonw.resolve()),
        "logs_dir": str((app_root / "logs").resolve()),
        "outputs_dir": str((app_root / "outputs").resolve()),
        "protected_user_data": [
            str((app_root / "inputs").resolve()),
            str((app_root / "outputs").resolve()),
            str((app_root / "jobs").resolve()),
        ],
        "default_execution_backend": "codex_exec",
        "port_start": 7860,
        "port_end": 7890,
    }
    path = app_root / CONFIG_NAME
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_launcher(app_root: Path, package_root: Path, config_path: Path) -> Path:
    _python, pythonw = venv_paths(app_root)

    def vbs(value: Path) -> str:
        return str(value.resolve()).replace('"', '""')

    launcher = app_root / f"{APP_NAME}.vbs"
    launcher.write_text(
        textwrap.dedent(
            f'''\
            Option Explicit
            Dim shell, command
            Set shell = CreateObject("WScript.Shell")
            shell.CurrentDirectory = "{vbs(package_root)}"
            command = Chr(34) & "{vbs(pythonw)}" & Chr(34) & " -m studio.launcher --config " & Chr(34) & "{vbs(config_path)}" & Chr(34)
            shell.Run command, 0, False
            '''
        ),
        encoding="utf-16",
    )
    return launcher


def desktop_path() -> Path:
    if os.name != "nt":
        raise InstallError("只有 Windows 可以创建 .lnk 桌面快捷方式。")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();[Environment]::GetFolderPath('Desktop')",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise InstallError("Windows 未返回桌面文件夹路径。")
    return Path(value)


def create_shortcut(launcher_path: Path, app_root: Path) -> Path:
    shortcut = desktop_path() / f"{APP_NAME}.lnk"
    icon = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "imageres.dll"
    script = (
        "$shell=New-Object -ComObject WScript.Shell;"
        f"$shortcut=$shell.CreateShortcut('{str(shortcut).replace("'", "''")}');"
        f"$shortcut.TargetPath='{str(launcher_path.resolve()).replace("'", "''")}';"
        f"$shortcut.WorkingDirectory='{str(app_root.resolve()).replace("'", "''")}';"
        f"$shortcut.IconLocation='{str(icon).replace("'", "''")},70';"
        "$shortcut.Description='角色一致性工作室';"
        "$shortcut.Save()"
    )
    encoded = script.encode("utf-16-le")
    import base64

    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(encoded).decode("ascii")]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not shortcut.is_file():
        raise InstallError("Windows 无法创建桌面快捷方式。")
    return shortcut


def studio_smoke_test(config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    package_root = Path(config["skill_root"])
    result = subprocess.run(
        [config["python"], "-m", "studio.server", "--config", str(config_path), "--smoke-test"],
        cwd=package_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if result.returncode != 0 or "STUDIO_SMOKE_TEST_PASS" not in result.stdout:
        raise InstallError("工作室页面启动检查未通过。详细原因请查看 logs/studio-install.log。")


def install_studio(
    app_root: Path,
    *,
    package_root: Path = SKILL_ROOT,
    runtime_installer=ensure_runtime,
    smoke_test=studio_smoke_test,
    shortcut_creator=create_shortcut,
    repository_worktree: bool = False,
) -> InstallResult:
    if os.name != "nt":
        raise InstallError("当前安装程序仅支持 Windows，其他平台尚未验证。")
    if sys.version_info < (3, 10):
        raise InstallError("角色一致性工作室需要 Python 3.10 或更高版本。")
    app_root = app_root.resolve()
    package_root = package_root.resolve()
    manifest = load_package_manifest(
        package_root,
        repository_worktree=repository_worktree,
    )
    app_root.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "inputs", "outputs", "jobs", "cache", "bridge/jobs"):
        (app_root / name).mkdir(parents=True, exist_ok=True)
    logger = _logger(app_root / "logs" / "studio-install.log")
    try:
        logger.info(
            "install start product=%s studio_interface=%s os=%s runtime=%s skill_root=%s",
            VERSIONS.product,
            VERSIONS.studio_interface,
            platform.platform(),
            sys.executable,
            package_root,
        )
        installed_package = install_package_source(app_root, package_root, manifest)
        dependencies_changed = runtime_installer(app_root, installed_package, logger)
        config_path = write_config(app_root, installed_package)
        launcher_path = write_launcher(app_root, installed_package, config_path)
        smoke_test(config_path)
        logger.info("smoke test=PASS")
        shortcut_path: Path | None = None
        shortcut_error: str | None = None
        try:
            shortcut_path = shortcut_creator(launcher_path, app_root)
            logger.info("shortcut created path=%s", shortcut_path)
        except Exception as exc:
            shortcut_error = str(exc)
            logger.error("shortcut creation failed reason=%s fallback=%s", shortcut_error, launcher_path)
        logger.info("install complete dependencies_changed=%s", dependencies_changed)
        return InstallResult(app_root, installed_package, config_path, launcher_path, shortcut_path, shortcut_error, dependencies_changed)
    finally:
        _close_logger(logger)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键安装角色一致性工作室。")
    parser.add_argument("--app-root", type=Path, default=default_app_root())
    parser.add_argument("--package-root", type=Path, default=SKILL_ROOT, help="Public Beta 白名单包根目录")
    parser.add_argument(
        "--repository-worktree",
        action="store_true",
        help="从独立 Public Git clone 安装时忽略该 clone 自身的 .git 元数据。",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install_studio(
            args.app_root,
            package_root=args.package_root,
            repository_worktree=args.repository_worktree,
        )
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"安装失败：{exc}", file=sys.stderr)
        return 2
    payload = {
        "status": "INSTALLED",
        "app_root": str(result.app_root),
        "package_root": str(result.package_root),
        "desktop_shortcut": str(result.shortcut_path) if result.shortcut_path else None,
        "fallback_launcher": str(result.launcher_path),
        "shortcut_error": result.shortcut_error,
        "dependencies_changed": result.dependencies_changed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("角色一致性工作室已安装。")
        if result.shortcut_path:
            print(f"桌面快捷方式：{result.shortcut_path}")
        else:
            print(f"未能创建快捷方式：{result.shortcut_error}")
            print(f"可双击此备用启动器：{result.launcher_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
