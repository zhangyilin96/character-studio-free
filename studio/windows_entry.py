"""Frozen Windows entry point for Character Studio Beta."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from character_workflow.versioning import VERSIONS
from config.public_product import DISTRIBUTION_PUBLIC_BETA


CONFIG_NAME = "studio-config.json"


def installed_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[1]


def app_data_root() -> Path:
    override = os.getenv("CHARACTER_STUDIO_APP_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    # Reuse the beta.2 data root so upgrades retain inputs, outputs, jobs, logs,
    # configuration, history, and other legal local state.
    return (base / "CharacterConsistencyStudio").resolve()


def ensure_config() -> Path:
    install_root = installed_root()
    skill_root = install_root / "skill" if getattr(sys, "frozen", False) else install_root
    app_root = app_data_root()
    for relative in ("logs", "inputs", "outputs", "jobs", "cache", "bridge/jobs"):
        (app_root / relative).mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": VERSIONS.studio_interface,
        "product_versions": VERSIONS.as_dict(),
        "distribution_mode": DISTRIBUTION_PUBLIC_BETA,
        "app_name": "Character Studio Beta",
        "app_root": str(app_root),
        "skill_root": str(skill_root.resolve()),
        "logs_dir": str((app_root / "logs").resolve()),
        "outputs_dir": str((app_root / "outputs").resolve()),
        "protected_user_data": [
            str((app_root / name).resolve())
            for name in ("inputs", "outputs", "jobs", "logs", "cache", "bridge/jobs")
        ],
        "default_execution_backend": "codex_exec",
        "port_start": 7860,
        "port_end": 7890,
        "server_executable": str(Path(sys.executable).resolve()) if getattr(sys, "frozen", False) else "",
        "keep_launcher_alive": bool(getattr(sys, "frozen", False)),
        "icon_png": str((skill_root / "assets" / "icons" / "favicon.png").resolve()),
    }
    path = app_root / CONFIG_NAME
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Character Studio Beta")
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    config_path = (args.config or ensure_config()).resolve()

    if args.server or args.smoke_test:
        from studio.server import main as server_main

        server_args = ["--config", str(config_path)]
        if args.smoke_test:
            server_args.append("--smoke-test")
        else:
            if args.port is None:
                raise ValueError("server mode requires --port")
            server_args.extend(["--port", str(args.port)])
        return server_main(server_args)

    from studio.launcher import main as launcher_main

    return launcher_main(["--config", str(config_path)])


if __name__ == "__main__":
    raise SystemExit(main())
