"""Public Beta 通过 ``codex exec`` 调用用户自己的 Codex 会话。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Callable, Sequence
import uuid

from PIL import Image, ImageOps

from character_workflow.versioning import VERSIONS
from contracts.public_execution import PublicExecutionProfile, public_beta_profile
from scripts.reference_preprocessing import PASS, character_cutout


StatusCallback = Callable[[str], None]
Runner = Callable[..., subprocess.CompletedProcess[str]]
_PROGRESS_PREFIX = "__studio_progress__|"
CODEX_INSTALL_URL = "https://developers.openai.com/codex/cli"
_ALLOWED_FAILURE_REASONS = {
    "",
    "ADVANCED_CASE_NOT_SUPPORTED",
    "CODEX_EXEC_FAILED",
    "CODEX_AUTH_REQUIRED",
    "CODEX_NETWORK_FAILED",
    "CODEX_QUOTA_EXHAUSTED",
    "CODEX_RESULT_INVALID",
    "CODEX_START_FAILED",
    "CODEX_TIMEOUT",
    "OUTFIT_PREPROCESSING_FAILED",
    "PUBLIC_TASK_FAILED",
    "USER_CANCELLED",
}


@dataclass(frozen=True)
class PublicBridgeHealth:
    available: bool
    message: str
    executable: str | None = None
    diagnostic_code: str = ""


@dataclass(frozen=True)
class PublicBridgeRequest:
    character_path: Path
    secondary_path: Path
    workflow: str = "pose_transfer"
    mode_label: str = "自动"
    user_prompt: str = ""
    execution_profile: PublicExecutionProfile | None = None

    def __post_init__(self) -> None:
        if self.workflow not in {"pose_transfer", "outfit_transfer"}:
            raise ValueError("Public Beta 不支持该工作流。")
        if self.execution_profile is None:
            object.__setattr__(self, "execution_profile", public_beta_profile(self.workflow))


@dataclass(frozen=True)
class PublicBridgeResult:
    status: str
    message: str
    output_dir: Path
    result_path: Path | None = None
    artifact_path: Path | None = None
    request_id: str = ""
    retryable: bool = False
    checks: tuple[str, ...] = ()
    failure_reason: str = ""


PUBLIC_RESULT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "message",
        "result_path",
        "artifact_path",
        "request_id",
        "retryable",
        "checks",
        "failure_reason",
    ],
    "properties": {
        "status": {"type": "string", "enum": ["completed", "blocked", "failed"]},
        "message": {"type": "string"},
        "result_path": {"type": "string"},
        "artifact_path": {"type": "string"},
        "request_id": {"type": "string"},
        "retryable": {"type": "boolean"},
        "checks": {"type": "array", "items": {"type": "string"}},
        "failure_reason": {"type": "string"},
    },
}


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _within(root: Path, value: str) -> Path | None:
    if not value.strip():
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _bundled_codex_cli() -> str | None:
    roots: list[Path] = []
    configured_home = os.getenv("CODEX_HOME", "").strip()
    if configured_home:
        roots.append(Path(configured_home))
    roots.append(Path.home() / ".codex")
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = root.expanduser()
        if root in seen:
            continue
        seen.add(root)
        releases = root / "packages" / "standalone" / "releases"
        for name in ("codex.exe", "codex"):
            candidates.extend(path for path in releases.glob(f"*/bin/{name}") if path.is_file())
    if not candidates:
        return None
    return str(max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path))))


def _resolve_codex_executable(explicit: str | None) -> str | None:
    return (
        (explicit or "").strip()
        or os.getenv("CHARACTER_STUDIO_CODEX_EXECUTABLE", "").strip()
        or _bundled_codex_cli()
        or shutil.which("codex")
    )


class PublicCodexBridge:
    """把公开 Studio 请求封装为一次低强度、结构化、路径受限的 Codex 任务。"""

    def __init__(
        self,
        app_root: Path,
        *,
        executable: str | None = None,
        skill_root: Path | None = None,
        runner: Runner = subprocess.run,
        timeout_seconds: int = 1200,
    ):
        self.app_root = Path(app_root).resolve()
        self.jobs_root = self.app_root / "bridge" / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.executable = _resolve_codex_executable(executable)
        self.skill_root = Path(skill_root).resolve() if skill_root else None
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._cancel_requested = threading.Event()

    def health_check(self) -> PublicBridgeHealth:
        if not self.executable:
            return PublicBridgeHealth(
                False,
                "未找到可供 Character Studio 调用的 Codex。请打开官方安装说明，完成安装和登录后重新检测。",
                diagnostic_code="CODEX_NOT_FOUND",
            )
        try:
            result = self.runner(
                [self.executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=_creation_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            windows_store_desktop = "\\windowsapps\\openai.codex_" in self.executable.casefold()
            return PublicBridgeHealth(
                False,
                (
                    "已检测到 Codex 桌面应用，但本地桥接还需要可从命令行运行的 Codex CLI。"
                    "请打开 Codex 官方安装说明完成安装与登录，然后重新检测。"
                    if windows_store_desktop
                    else "已找到 Codex，但 Character Studio 无法调用它。请打开官方安装说明检查安装与登录。"
                ),
                self.executable,
                "CODEX_DESKTOP_ONLY" if windows_store_desktop else "CODEX_NOT_EXECUTABLE",
            )
        if result.returncode != 0:
            return PublicBridgeHealth(
                False,
                "Codex 尚未就绪。请按官方说明完成登录或修复安装，然后重新检测。",
                self.executable,
                "CODEX_HEALTH_FAILED",
            )
        version = (result.stdout or result.stderr or "Codex").strip().splitlines()[0]
        try:
            login = self.runner(
                [self.executable, "login", "status"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=_creation_flags(),
            )
        except (OSError, subprocess.SubprocessError):
            login = None
        if login is None or login.returncode != 0:
            return PublicBridgeHealth(
                False,
                (
                    f"已安装 {version}，但尚未完成或无法确认 ChatGPT 登录。"
                    "请打开官方登录说明完成登录，然后点击重新检测。"
                ),
                self.executable,
                "CODEX_LOGIN_UNVERIFIED",
            )
        return PublicBridgeHealth(True, f"用户 Codex 已就绪：{version}", self.executable, "CODEX_READY")

    @staticmethod
    def _copy_input(source: Path, target_dir: Path, name: str) -> Path:
        if not source.is_file():
            raise FileNotFoundError(source)
        suffix = source.suffix.lower() if source.suffix else ".png"
        target = target_dir / f"{name}{suffix}"
        shutil.copy2(source, target)
        return target

    @staticmethod
    def _prepare_outfit(source: Path, job_dir: Path) -> Path:
        prepared_dir = job_dir / "prepared"
        prepared_dir.mkdir(parents=True, exist_ok=True)
        normalized_path = prepared_dir / "outfit_normalized.png"
        with Image.open(source) as opened:
            normalized = ImageOps.exif_transpose(opened)
            if normalized.mode not in {"RGB", "RGBA"}:
                normalized = normalized.convert("RGBA")
            normalized.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            normalized.save(normalized_path, format="PNG", optimize=True)
        result = character_cutout(normalized_path, prepared_dir, artifact_prefix="outfit_sanitized")
        meta = dict(result["meta"])
        if meta.get("status") != PASS or meta.get("renderer_eligible") is not True:
            raise ValueError("服装图背景无法安全分离")
        meta["module"] = "OUTFIT_SANITIZATION"
        meta["authority"] = {
            "garment_structure": "HARD",
            "material": "HARD",
            "color": "HARD",
            "decoration": "HARD",
            "layering": "HARD",
            "character_identity": "NONE",
            "face": "NONE",
            "hair": "NONE",
            "body": "NONE",
            "pose": "NONE",
            "background": "NONE",
            "rendering_domain": "NONE",
        }
        meta["policy"] = {
            "route": "PUBLIC_BETA_FAST_OUTFIT",
            "raw_outfit_scene_is_renderer_authority": False,
            "additional_preprocessing_required": False,
            "single_render_expected": True,
        }
        result["meta_path"].write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result["cutout_path"]

    def _prompt(self, request: PublicBridgeRequest, job_dir: Path, character: Path, secondary: Path) -> str:
        workflow_text = "姿势迁移" if request.workflow == "pose_transfer" else "一键换装"
        authority = (
            "第一张图只负责角色身份、脸、发型、身材、比例、服装和最终画风；第二张图只负责姿势、构图、裁切、遮挡和透视。"
            if request.workflow == "pose_transfer"
            else "第一张图负责角色身份、身体、姿势、背景和最终画风；第二张图只负责服装结构、材质、颜色、装饰和层次。"
        )
        profile = request.execution_profile or public_beta_profile(request.workflow)
        skill_entry = self.skill_root / "SKILL.md" if self.skill_root else None
        if skill_entry and skill_entry.is_file():
            skill_instruction = f"先完整读取并遵循此公开 Skill：{skill_entry}"
        else:
            skill_instruction = "使用已安装的 $character-consistency-pipeline"
        return f"""{skill_instruction}，完成一次 {workflow_text}，不要向用户追问。

输入图片：
- 第一张：{character}
- 第二张：{secondary}

图片职责：{authority}
生成模式：{request.mode_label}
补充要求：{request.user_prompt or '无'}

{profile.instruction}

所有生成、检查与交付资产只能写入当前任务目录：{job_dir}
可交付图片固定写为 result.png。没有通过严格交付时，不要创建或返回正常结果图；
请返回 blocked 和 ADVANCED_CASE_NOT_SUPPORTED。最后只按给定 JSON Schema 返回状态，
路径必须使用相对于当前任务目录的相对路径。
"""

    @staticmethod
    def _failure_from_stderr(stderr: str) -> tuple[str, str]:
        normalized = stderr.casefold()
        quota_markers = (
            "insufficient_quota",
            "quota exceeded",
            "usage limit",
            "credit balance",
            "credits exhausted",
        )
        auth_markers = (
            "not logged in",
            "login required",
            "sign in required",
            "authentication required",
            "unauthorized",
            "invalid authentication",
        )
        network_markers = (
            "network is unreachable",
            "connection refused",
            "connection reset",
            "temporary failure in name resolution",
            "dns",
            "proxy error",
            "tls handshake",
            "offline",
        )
        if any(marker in normalized for marker in quota_markers):
            return "CODEX_QUOTA_EXHAUSTED", "Codex 当前额度不足或已达到使用上限。请稍后重试或检查该账号的使用状态。"
        if any(marker in normalized for marker in auth_markers):
            return "CODEX_AUTH_REQUIRED", "Codex 登录已失效或尚未完成。请按官方登录说明完成登录后重新检测。"
        if any(marker in normalized for marker in network_markers):
            return "CODEX_NETWORK_FAILED", "Codex 当前无法连接网络。请检查网络连接后重试。"
        return "CODEX_EXEC_FAILED", "Codex 没有完成本次任务。详细诊断已保留在本地任务目录。"

    def _command(
        self,
        job_dir: Path,
        schema_path: Path,
        response_path: Path,
        image_paths: Sequence[Path],
    ) -> list[str]:
        if not self.executable:
            raise RuntimeError("Codex executable unavailable")
        command = [
            self.executable,
            "exec",
            "--json",
            "-c",
            'model_reasoning_effort="low"',
            "--skip-git-repo-check",
            "--approve-for-me",
            "--cd",
            str(job_dir),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(response_path),
        ]
        for path in image_paths:
            command.extend(["--image", str(path)])
        command.append("-")
        return command

    @staticmethod
    def _artifact_stage(job_dir: Path) -> tuple[str, float, float]:
        names = {path.name for path in job_dir.rglob("*") if path.is_file()}
        if "bridge-result.json" in names:
            return "正在整理最终结果", 0.98, 0.995
        if "result.png" in names:
            return "已生成结果，正在严格交付", 0.94, 0.98
        if {"strict-delivery.json", "strict_replica_delivery.json"} & names:
            return "正在执行交付检查", 0.88, 0.94
        if "candidate-observation.json" in names or any(name.endswith(".quick.observed.json") for name in names):
            return "正在检查生成结果", 0.76, 0.88
        if any(name.startswith("candidate_") and name.endswith(".png") for name in names):
            return "图片已生成，正在校验", 0.68, 0.76
        if "renderer_request_receipt.json" in names or "manifest.json" in names:
            return "正在调用 ImageGen 生成图片", 0.50, 0.68
        if "final_prompt.md" in names or "bridge-prompt.md" in names:
            return "正在组装生成指令", 0.40, 0.50
        if {"reference_preprocessing.json", "outfit_sanitized_meta.json"} & names:
            return "正在分析与预处理参考图", 0.28, 0.40
        return "正在启动用户自己的 Codex", 0.18, 0.28

    @classmethod
    def _progress_message(
        cls,
        job_dir: Path,
        *,
        elapsed: float,
        target_seconds: float,
        previous_fraction: float,
        stage_started: float,
    ) -> tuple[str, float]:
        label, floor, ceiling = cls._artifact_stage(job_dir)
        stage_span = max(1.0, target_seconds * max(ceiling - floor, 0.02))
        stage_fraction = min(0.88, max(0.0, (time.monotonic() - stage_started) / stage_span))
        fraction = max(previous_fraction, floor + (ceiling - floor) * stage_fraction)
        projected_total = max(target_seconds, elapsed / max(fraction, 0.05))
        remaining = max(0.0, projected_total - elapsed)
        return f"{_PROGRESS_PREFIX}{fraction:.4f}|{label}|{int(elapsed)}|{int(remaining)}", fraction

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=_creation_flags(),
            )
            return
        process.terminate()

    def cancel_active(self) -> bool:
        with self._process_lock:
            process = self._active_process
            if process is None or process.poll() is not None:
                return False
            self._cancel_requested.set()
        self._terminate_process_tree(process)
        return True

    def _run_streaming(
        self,
        command: list[str],
        prompt: str,
        job_dir: Path,
        request: PublicBridgeRequest,
        notify: StatusCallback,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        events_path = job_dir / "codex-events.jsonl"
        stderr_path = job_dir / "codex-stderr.log"
        started = time.monotonic()
        profile = request.execution_profile or public_beta_profile(request.workflow)
        previous_fraction = 0.18
        previous_stage = ""
        stage_started = started
        outcome = "completed"
        self._cancel_requested.clear()
        with (
            events_path.open("w", encoding="utf-8") as events_stream,
            stderr_path.open("w", encoding="utf-8") as stderr_stream,
        ):
            process = subprocess.Popen(
                command,
                cwd=job_dir,
                stdin=subprocess.PIPE,
                stdout=events_stream,
                stderr=stderr_stream,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_creation_flags(),
            )
            with self._process_lock:
                self._active_process = process
            try:
                if process.stdin is not None:
                    process.stdin.write(prompt)
                    process.stdin.close()
                while process.poll() is None:
                    elapsed = time.monotonic() - started
                    if self._cancel_requested.is_set():
                        outcome = "cancelled"
                        self._terminate_process_tree(process)
                    elif elapsed >= self.timeout_seconds:
                        outcome = "timeout"
                        self._terminate_process_tree(process)
                    label = self._artifact_stage(job_dir)[0]
                    if label != previous_stage:
                        previous_stage = label
                        stage_started = time.monotonic()
                    marker, previous_fraction = self._progress_message(
                        job_dir,
                        elapsed=elapsed,
                        target_seconds=profile.expected_seconds,
                        previous_fraction=previous_fraction,
                        stage_started=stage_started,
                    )
                    notify(marker)
                    if outcome != "completed":
                        break
                    time.sleep(1.0)
                if self._cancel_requested.is_set():
                    outcome = "cancelled"
                try:
                    return_code = process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    return_code = process.wait(timeout=5)
            finally:
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        return subprocess.CompletedProcess(command, return_code, "", stderr), outcome

    def run(self, request: PublicBridgeRequest, on_status: StatusCallback | None = None) -> PublicBridgeResult:
        notify = on_status or (lambda _value: None)
        health = self.health_check()
        if not health.available and health.diagnostic_code != "CODEX_LOGIN_UNVERIFIED":
            return PublicBridgeResult(
                "failed",
                health.message,
                self.jobs_root,
                retryable=True,
                failure_reason=health.diagnostic_code or "PUBLIC_TASK_FAILED",
            )
        job_id = f"studio-{uuid.uuid4().hex[:12]}"
        job_dir = self.jobs_root / job_id
        inputs_dir = job_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=False)
        character = self._copy_input(request.character_path.resolve(), inputs_dir, "character")
        secondary_name = "pose" if request.workflow == "pose_transfer" else "outfit"
        secondary = self._copy_input(request.secondary_path.resolve(), inputs_dir, secondary_name)
        schema_path = job_dir / "bridge-result-schema.json"
        response_path = job_dir / "bridge-result.json"
        prompt_path = job_dir / "bridge-prompt.md"
        receipt_path = job_dir / "bridge-receipt.json"
        renderer_secondary = secondary
        if request.workflow == "outfit_transfer":
            notify("正在准备换装输入")
            try:
                renderer_secondary = self._prepare_outfit(secondary, job_dir)
            except (OSError, ValueError):
                receipt_path.write_text(
                    json.dumps(
                        {"status": "OUTFIT_PREPROCESSING_FAILED", "job_id": job_id, "versions": VERSIONS.as_dict()},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return PublicBridgeResult(
                    "failed",
                    "服装图无法安全完成快速预处理。请裁剪到主体清晰、背景简单的服装图后重试。",
                    job_dir,
                    artifact_path=job_dir,
                    request_id=job_id,
                    retryable=True,
                    failure_reason="OUTFIT_PREPROCESSING_FAILED",
                )
        schema_path.write_text(json.dumps(PUBLIC_RESULT_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        prompt = self._prompt(request, job_dir, character, renderer_secondary)
        prompt_path.write_text(prompt, encoding="utf-8")
        command = self._command(job_dir, schema_path, response_path, (character, renderer_secondary))
        notify("正在调用用户自己的 Codex")
        try:
            if self.runner is subprocess.run:
                process, outcome = self._run_streaming(command, prompt, job_dir, request, notify)
                if outcome == "cancelled":
                    return PublicBridgeResult(
                        "cancelled",
                        "已停止当前任务。已生成的中间文件保留在本地任务目录。",
                        job_dir,
                        artifact_path=job_dir,
                        request_id=job_id,
                        retryable=True,
                        failure_reason="USER_CANCELLED",
                    )
                if outcome == "timeout":
                    raise subprocess.TimeoutExpired(command, self.timeout_seconds)
            else:
                process = self.runner(
                    command,
                    input=prompt,
                    cwd=job_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                    creationflags=_creation_flags(),
                )
        except subprocess.TimeoutExpired:
            return PublicBridgeResult(
                "failed",
                "Codex 任务运行超时。任务文件已保留，可以重试。",
                job_dir,
                artifact_path=job_dir,
                request_id=job_id,
                retryable=True,
                failure_reason="CODEX_TIMEOUT",
            )
        except (OSError, subprocess.SubprocessError):
            return PublicBridgeResult(
                "failed",
                "Codex 任务未能启动。请确认 Codex 已登录并可正常运行。",
                job_dir,
                artifact_path=job_dir,
                request_id=job_id,
                retryable=True,
                failure_reason="CODEX_START_FAILED",
            )
        receipt = {
            "schema_version": 1,
            "job_id": job_id,
            "backend": "codex_exec",
            "exit_code": process.returncode,
            "versions": VERSIONS.as_dict(),
            "response_present": response_path.is_file(),
        }
        stderr_excerpt = (process.stderr or "").strip()
        if stderr_excerpt:
            receipt["stderr_excerpt"] = stderr_excerpt[:4000]
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if process.returncode != 0 or not response_path.is_file():
            failure_reason, failure_message = self._failure_from_stderr(process.stderr or "")
            return PublicBridgeResult(
                "failed",
                failure_message,
                job_dir,
                artifact_path=job_dir,
                request_id=job_id,
                retryable=True,
                failure_reason=failure_reason,
            )
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("bridge result must be an object")
        except (OSError, ValueError, json.JSONDecodeError):
            return PublicBridgeResult(
                "failed",
                "Codex 已返回，但结果摘要无法读取。任务文件已保留。",
                job_dir,
                artifact_path=job_dir,
                request_id=job_id,
                retryable=True,
                failure_reason="CODEX_RESULT_INVALID",
            )
        status = str(payload.get("status", "failed"))
        result_path = _within(job_dir, str(payload.get("result_path", "")))
        artifact_path = _within(job_dir, str(payload.get("artifact_path", ""))) or job_dir
        if result_path is not None and not result_path.is_file():
            result_path = None
        if status == "completed" and result_path is None:
            status = "blocked"
        if status == "blocked":
            result_path = None
            failure_reason = "ADVANCED_CASE_NOT_SUPPORTED"
        else:
            raw_reason = str(payload.get("failure_reason", ""))
            failure_reason = raw_reason if raw_reason in _ALLOWED_FAILURE_REASONS else "PUBLIC_TASK_FAILED"
        notify("Codex 任务已返回")
        return PublicBridgeResult(
            status,
            str(payload.get("message") or ("生成完成" if status == "completed" else "任务未完成")),
            job_dir,
            result_path,
            artifact_path,
            str(payload.get("request_id") or job_id),
            bool(payload.get("retryable")),
            tuple(str(item) for item in payload.get("checks", []) if str(item).strip()),
            failure_reason,
        )


def public_bridge_request_dict(request: PublicBridgeRequest) -> dict[str, object]:
    """只读诊断，不包含图片内容。"""

    return asdict(request)
