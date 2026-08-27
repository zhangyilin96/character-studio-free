"""Public Beta 的本地 Gradio 服务入口。"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time

from character_workflow.versioning import VERSIONS
from config.public_product import DISTRIBUTION_PUBLIC_BETA, require_public_beta

from . import STUDIO_VERSION
from .public_beta import build_public_beta_demo
from .public_service import PublicStudioService


def _read_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("工作室配置必须是一个对象。")
    return value


def _logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("character_studio_public_server")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 Character Studio Beta。")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args(argv)
    config = _read_config(args.config.resolve())
    distribution_mode = require_public_beta(config.get("distribution_mode", DISTRIBUTION_PUBLIC_BETA))
    app_root = Path(config["app_root"]).resolve()
    skill_root = Path(config.get("skill_root") or Path(__file__).resolve().parents[1]).resolve()
    favicon_path = Path(config.get("icon_png") or skill_root / "assets" / "icons" / "favicon.png")
    logger = _logger(Path(config["logs_dir"]) / "studio-server.log")
    service = PublicStudioService(app_root, logger, skill_root=skill_root)
    demo = build_public_beta_demo(app_root, logger, service=service, favicon_path=favicon_path)
    if args.smoke_test:
        demo.close()
        logger.info("public studio smoke test passed version=%s", STUDIO_VERSION)
        print("STUDIO_SMOKE_TEST_PASS")
        return 0
    if args.port is None:
        raise ValueError("启动服务时必须提供 --port。")
    state_path = app_root / "studio-state.json"
    state = {
        "studio_version": STUDIO_VERSION,
        "distribution_mode": distribution_mode,
        "versions": VERSIONS.as_dict(),
        "pid": os.getpid(),
        "port": args.port,
        "url": f"http://127.0.0.1:{args.port}",
        "started_at": time.time(),
    }
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def cleanup() -> None:
        try:
            current = json.loads(state_path.read_text(encoding="utf-8"))
            if current.get("pid") == os.getpid():
                state_path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass

    atexit.register(cleanup)
    try:
        demo.queue(default_concurrency_limit=1, max_size=2).launch(
            server_name="127.0.0.1",
            server_port=args.port,
            share=False,
            inbrowser=False,
            prevent_thread_lock=False,
            show_error=False,
            allowed_paths=[
                str(app_root / "outputs"),
                str(app_root / "jobs"),
                str(app_root / "bridge" / "jobs"),
            ],
            theme=demo._studio_theme,
            css=demo._studio_css,
            favicon_path=demo._studio_favicon,
        )
    except Exception:
        logger.exception("fatal public server error port=%s", args.port)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
