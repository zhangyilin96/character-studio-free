#!/usr/bin/env python3
"""Validate, download, and verify the fixed Character Studio release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import BinaryIO, Callable
from urllib.parse import urlparse
from urllib.request import urlopen


OFFICIAL_REPOSITORY = "https://github.com/zhangyilin96/character-studio-free"
OFFICIAL_MANIFEST_URL = (
    "https://raw.githubusercontent.com/zhangyilin96/character-studio-free/main/install-manifest.json"
)
INSTALLER_NAME = "CharacterStudioBeta-Setup.exe"
PUBLIC_CHANNEL = "public-beta"
RELEASE_VERSION = "0.1.0-beta.3"
RELEASE_TAG = f"v{RELEASE_VERSION}"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest(manifest: dict[str, object]) -> dict[str, object]:
    if manifest.get("schema_version") != 1:
        raise ManifestError("安装清单版本不受支持。")
    if manifest.get("repository") != OFFICIAL_REPOSITORY:
        raise ManifestError("安装清单不是来自唯一允许的官方仓库。")
    if manifest.get("product") != "Character Studio Beta":
        raise ManifestError("安装清单产品名称不匹配。")
    if manifest.get("channel") != PUBLIC_CHANNEL:
        raise ManifestError("安装清单发布通道不匹配。")
    if manifest.get("version") != RELEASE_VERSION or manifest.get("release_tag") != RELEASE_TAG:
        raise ManifestError("安装清单版本或发布标签不匹配。")
    installer = manifest.get("installer")
    if not isinstance(installer, dict):
        raise ManifestError("安装清单缺少安装包信息。")
    if installer.get("asset_name") != INSTALLER_NAME:
        raise ManifestError("安装包文件名不匹配。")
    try:
        size = int(installer.get("size", 0))
    except (TypeError, ValueError) as exc:
        raise ManifestError("安装包大小无效。") from exc
    expected_hash = str(installer.get("sha256", "")).casefold()
    if size <= 0 or not SHA256_PATTERN.fullmatch(expected_hash):
        raise ManifestError("安装包大小或 SHA-256 无效。")
    url = str(installer.get("url", ""))
    parsed = urlparse(url)
    prefix = "/zhangyilin96/character-studio-free/releases/download/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
        or not parsed.path.endswith("/" + INSTALLER_NAME)
    ):
        raise ManifestError("安装包地址不属于允许的 GitHub Release。")
    release_tag = str(manifest.get("release_tag", ""))
    expected_path = f"{prefix}{release_tag}/{INSTALLER_NAME}"
    if not release_tag or parsed.path != expected_path:
        raise ManifestError("安装包地址与发布版本不匹配。")
    return manifest


def verify_installer(path: Path, manifest: dict[str, object]) -> None:
    validate_manifest(manifest)
    installer = manifest["installer"]
    assert isinstance(installer, dict)
    if path.stat().st_size != int(installer["size"]):
        raise ManifestError("安装包大小与清单不一致，已停止。")
    if _sha256(path) != str(installer["sha256"]).casefold():
        raise ManifestError("安装包 SHA-256 与清单不一致，已停止。")


def download_installer(
    manifest: dict[str, object],
    target_dir: Path,
    *,
    opener: Callable[..., BinaryIO] = urlopen,
) -> Path:
    validate_manifest(manifest)
    installer = manifest["installer"]
    assert isinstance(installer, dict)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / INSTALLER_NAME
    with opener(str(installer["url"]), timeout=120) as response, target.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    try:
        verify_installer(target, manifest)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def run_installer(path: Path) -> None:
    result = subprocess.run(
        [str(path.resolve()), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        timeout=1200,
        check=False,
    )
    if result.returncode != 0:
        raise ManifestError(f"安装程序未完成，退出代码为 {result.returncode}。")


def main() -> int:
    parser = argparse.ArgumentParser(description="安全校验 Character Studio 安装包。")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    try:
        value = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ManifestError("安装清单必须是 JSON 对象。")
        validate_manifest(value)
        if args.installer:
            installer = args.installer.resolve()
            verify_installer(installer, value)
        else:
            temp_dir = Path(tempfile.mkdtemp(prefix="character-studio-install-"))
            installer = download_installer(value, temp_dir)
        if args.install:
            run_installer(installer)
    except (OSError, ValueError, json.JSONDecodeError, ManifestError) as exc:
        print(f"安装失败：{exc}")
        return 2
    print("安装包来源、大小和 SHA-256 校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
