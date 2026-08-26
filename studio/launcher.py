"""No-console launcher with single-instance and dynamic-port behavior."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
import webbrowser


class StartupError(RuntimeError):
    pass


def load_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise StartupError("工作室配置无效。")
    return value


def build_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"character_consistency_studio_launcher:{path}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def probe_url(url: str, timeout: float = 0.7) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:
            return 200 <= int(getattr(response, "status", 200)) < 500
    except (OSError, URLError, TimeoutError):
        return False


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_url(app_root: Path) -> str | None:
    state_path = app_root / "studio-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        url = str(state["url"])
        pid = int(state["pid"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return url if pid_running(pid) and probe_url(url) else None


def choose_port(start: int, end: int) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR, 1)
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise StartupError(f"本机端口 {start} 到 {end} 均不可用。")


@contextmanager
def launcher_lock(app_root: Path, timeout: float = 20.0):
    path = app_root / "launcher.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    started = time.monotonic()
    locked = False
    try:
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() - started >= timeout:
                    raise StartupError("另一个工作室窗口仍在启动，请稍后再试。")
                time.sleep(0.2)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def spawn_server(config_path: Path, config: dict, port: int):
    pythonw = Path(config["pythonw"]).resolve()
    skill_root = Path(config["skill_root"]).resolve()
    server_log = Path(config["logs_dir"]) / "studio-server-console.log"
    server_log.parent.mkdir(parents=True, exist_ok=True)
    stream = server_log.open("ab", buffering=0)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            [str(pythonw), "-m", "studio.server", "--config", str(config_path), "--port", str(port)],
            cwd=skill_root,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            creationflags=flags,
            close_fds=True,
        )
    except Exception:
        stream.close()
        raise
    stream.close()
    return process


def wait_ready(url: str, process, timeout: float = 55.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            return False
        if probe_url(url):
            return True
        time.sleep(0.25)
    return False


def open_browser(url: str) -> None:
    if not webbrowser.open(url, new=2, autoraise=True):
        raise StartupError(f"工作室已启动，但无法打开默认浏览器。请手动打开 {url}。")


def launch_once(config_path: Path) -> str:
    config = load_config(config_path)
    app_root = Path(config["app_root"]).resolve()
    logger = build_logger(Path(config["logs_dir"]) / "studio-launch.log")
    try:
        with launcher_lock(app_root):
            existing = running_url(app_root)
            if existing:
                logger.info("existing server detected url=%s", existing)
                open_browser(existing)
                logger.info("browser launch existing url=%s", existing)
                return existing
            port = choose_port(int(config.get("port_start", 7860)), int(config.get("port_end", 7890)))
            logger.info("starting server port=%s", port)
            process = spawn_server(config_path, config, port)
            url = f"http://127.0.0.1:{port}"
            if not wait_ready(url, process):
                logger.error("server startup failed port=%s exit_code=%s", port, process.poll())
                raise StartupError("工作室无法启动。详细原因请查看 logs/studio-server-console.log。")
            server_pid = process.pid
            try:
                state = json.loads((app_root / "studio-state.json").read_text(encoding="utf-8"))
                if int(state.get("port", -1)) == port:
                    server_pid = int(state.get("pid", server_pid))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
            logger.info("server ready port=%s pid=%s", port, server_pid)
            open_browser(url)
            logger.info("browser launch url=%s", url)
            return url
    finally:
        close_logger(logger)


def show_error(message: str, app_root: Path | None = None) -> None:
    if app_root:
        try:
            (app_root / "studio-startup-error.txt").write_text(message + "\n", encoding="utf-8")
        except OSError:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "角色一致性工作室", 0x10)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打开角色一致性工作室。")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    app_root: Path | None = None
    try:
        config = load_config(args.config.resolve())
        app_root = Path(config["app_root"]).resolve()
        launch_once(args.config.resolve())
        return 0
    except Exception as exc:
        if app_root:
            logger = build_logger(Path(config["logs_dir"]) / "studio-launch.log")
            logger.exception("fatal launcher error")
            close_logger(logger)
        show_error(str(exc), app_root)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
