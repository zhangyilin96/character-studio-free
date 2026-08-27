#!/usr/bin/env python3
"""验证 Public Beta staging manifest、文件哈希和禁止目录。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys


MANIFEST_NAME = "free-export-manifest.json"
MANIFEST_SHA_NAME = "free-export-manifest.sha256"
FORBIDDEN_DIRS = {"pro", "private_beta", "license", "tests", ".git", ".venv", "__pycache__", "inputs", "outputs", "cache", "logs", "tmp"}
FORBIDDEN_PATHS = {
    "studio/app.py",
    "studio/service.py",
    "studio/pro_loader.py",
    "contracts/pro_extension.py",
    "config/product.py",
    "character_workflow/workflow_service.py",
    "character_workflow/workflow_types.py",
    "bridge/codex_bridge.py",
}
REQUIRED_PATHS = {
    "INSTALL_WITH_CODEX.md",
    "LICENSE",
    "bridge/public_codex_bridge.py",
    "config/public_product.py",
    "contracts/public_execution.py",
    "studio/public_beta.py",
    "studio/public_service.py",
    "studio/public_types.py",
    "studio/server.py",
    "studio/windows_entry.py",
    "assets/icons/character-studio.ico",
    "assets/icons/favicon.png",
    "character_workflow/public_workflow_service.py",
    "character_workflow/public_types.py",
    "scripts/install_manifest.py",
}
FORBIDDEN_MARKERS = (
    "from license",
    "import license",
    "LicenseManager",
    "start_trial",
    "activate_license",
    "TrialExhaustedError",
    "BETA-",
    "PAID-",
    "UPGRADE-",
    "load_pro_extension",
    "import pro",
    "from pro",
    "PRO_DETAILED_CHECK_REQUIRED",
    "FEATURE_CATALOG",
    "pro_repair",
    "pose_expert_modules",
    "experimental_local_repair",
    "repair_mask_generator",
    "repair_failure_diagnosis",
    "repair_with_mask",
    "LOCAL_REPAIR_REQUIRED",
    "stable_generation",
    "validator_router",
    "routed_geometry_validator",
    "observe_candidate_full",
    "Routed v1.7 geometry validator",
    "expert_escalation_threshold",
)
FORBIDDEN_IMPORT_ROOTS = {"pro", "private_beta", "license"}
FORBIDDEN_IMPORT_MODULES = {
    "studio.app",
    "studio.service",
    "studio.pro_loader",
    "contracts.pro_extension",
    "config.product",
}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(root: Path, *, repository_worktree: bool = False) -> list[str]:
    errors: list[str] = []
    root = root.resolve()
    manifest_path = root / MANIFEST_NAME
    manifest_sha_path = root / MANIFEST_SHA_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"manifest 无法读取：{exc}"]
    try:
        expected_manifest_sha = manifest_sha_path.read_text(encoding="ascii").strip().split()[0].casefold()
    except (OSError, UnicodeError, IndexError) as exc:
        errors.append(f"manifest SHA-256 无法读取：{exc}")
    else:
        if expected_manifest_sha != sha256(manifest_path):
            errors.append("manifest 自身 SHA-256 不匹配。")
    if manifest.get("package") != "character-studio-public-beta":
        errors.append("manifest package 不是 character-studio-public-beta。")
    if manifest.get("distribution_mode") != "public_beta":
        errors.append("manifest distribution_mode 不是 public_beta。")
    expected = {item["path"]: item for item in manifest.get("files", []) if isinstance(item, dict) and item.get("path")}
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in {MANIFEST_NAME, MANIFEST_SHA_NAME}
        and "__pycache__" not in path.relative_to(root).parts
        and path.suffix.casefold() not in {".pyc", ".pyo"}
        and not (repository_worktree and ".git" in path.relative_to(root).parts)
    }
    if set(expected) != set(actual):
        errors.append("manifest 文件集合与 staging 不一致。")
    for relative, record in expected.items():
        path = actual.get(relative)
        if path is None:
            continue
        if path.stat().st_size != record.get("size") or sha256(path) != record.get("sha256"):
            errors.append(f"manifest 哈希或大小不匹配：{relative}")
    missing_required = REQUIRED_PATHS - set(actual)
    if missing_required:
        errors.append("缺少 Public Beta 必需文件：" + ", ".join(sorted(missing_required)))
    leaked_paths = FORBIDDEN_PATHS & set(actual)
    if leaked_paths:
        errors.append("包含私有或混合源码：" + ", ".join(sorted(leaked_paths)))
    for relative, path in actual.items():
        relative_parts = Path(relative).parts
        if any(part in FORBIDDEN_DIRS for part in relative_parts[:-1]) or path.name == ".git":
            errors.append(f"包含禁止目录或运行文件：{relative}")
        if path.suffix.casefold() not in {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".cmd"} and path.name != ".gitignore":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        # 本文件必须携带禁止词表才能离线校验；不把词表定义误报为实现泄漏。
        if relative != "scripts/validate_free_package.py":
            for marker in FORBIDDEN_MARKERS:
                if marker in text:
                    errors.append(f"包含私有实现标记 {marker!r}：{relative}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"疑似包含真实密钥：{relative}")
        if path.suffix.casefold() == ".py":
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError as exc:
                errors.append(f"Python 无法解析：{relative}:{exc.lineno}")
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if any(
                    name in FORBIDDEN_IMPORT_MODULES
                    or name.split(".", 1)[0] in FORBIDDEN_IMPORT_ROOTS
                    for name in names
                ):
                    errors.append(f"存在私有静态 import：{relative}:{node.lineno}")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Character Studio Public Beta staging。")
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--repository-worktree",
        action="store_true",
        help="复核独立 Public clone 时忽略该 clone 自身的 .git 元数据。",
    )
    args = parser.parse_args()
    errors = validate(args.root, repository_worktree=args.repository_worktree)
    if errors:
        print("PUBLIC_BETA_PACKAGE_VALIDATION_FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PUBLIC_BETA_PACKAGE_VALIDATION_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
