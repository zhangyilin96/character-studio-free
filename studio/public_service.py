"""Public Beta Studio 的独立执行服务。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Callable

from bridge.public_codex_bridge import PublicBridgeHealth, PublicBridgeRequest, PublicBridgeResult, PublicCodexBridge
from character_workflow.versioning import VERSIONS
from contracts.public_execution import public_beta_profile

from .public_types import PublicStudioResult


StatusCallback = Callable[[str], None]
PUBLIC_BACKEND = "codex_exec"
PUBLIC_FAILURE_REASON = "ADVANCED_CASE_NOT_SUPPORTED"


class PublicStudioService:
    def __init__(
        self,
        app_root: Path,
        logger: logging.Logger | None = None,
        *,
        bridge: PublicCodexBridge | None = None,
        skill_root: Path | None = None,
    ):
        self.app_root = app_root.resolve()
        self.outputs_root = self.app_root / "outputs"
        self.logger = logger or logging.getLogger("character_studio_public_beta")
        self.outputs_root.mkdir(parents=True, exist_ok=True)
        self.bridge = bridge or PublicCodexBridge(self.app_root, skill_root=skill_root)

    def codex_health(self) -> PublicBridgeHealth:
        return self.bridge.health_check()

    def codex_status(self) -> tuple[bool, str]:
        health = self.codex_health()
        return health.available, health.message

    def cancel_active(self) -> bool:
        return self.bridge.cancel_active()

    @staticmethod
    def _bridge_result(result: PublicBridgeResult) -> PublicStudioResult:
        unsupported = result.status == "blocked" or result.failure_reason == PUBLIC_FAILURE_REASON
        if unsupported:
            return PublicStudioResult(
                "当前 Beta 暂时无法可靠处理这个输入",
                "这个案例没有通过当前 Beta 的基础质量检查，因此没有直接交付可能存在明显错误的结果。",
                result.output_dir,
                None,
                result.artifact_path,
                result.request_id,
                True,
                result.checks,
                PUBLIC_FAILURE_REASON,
            )
        if result.status == "completed" and result.result_path is not None:
            return PublicStudioResult(
                "生成完成",
                "基础质量检查与严格交付检查已通过。",
                result.output_dir,
                result.result_path,
                result.artifact_path,
                result.request_id,
                False,
                result.checks,
                "",
            )
        if result.status == "cancelled":
            status = "生成已停止"
        else:
            status = "生成失败"
        return PublicStudioResult(
            status,
            result.message,
            result.output_dir,
            None,
            result.artifact_path,
            result.request_id,
            result.retryable,
            result.checks,
            result.failure_reason or "PUBLIC_TASK_FAILED",
        )

    def export_beta_diagnostic(self, result: PublicStudioResult) -> Path:
        """只导出非图片、无用户路径的本地测试信息。"""

        base = self.app_root / "jobs" / "public-beta-diagnostics"
        base.mkdir(parents=True, exist_ok=True)
        identifier = result.job_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_identifier = "".join(value for value in identifier if value.isalnum() or value in "-_")[:80] or "diagnostic"
        path = base / f"beta-diagnostic-{safe_identifier}.json"
        payload = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "distribution_mode": "public_beta",
            "versions": VERSIONS.as_dict(),
            "request_id": result.job_id or "",
            "status": result.status,
            "failure_reason": result.failure_reason,
            "backend": result.backend,
            "retryable": result.retryable,
            "checks": list(result.checks),
            "privacy": "No image data, image names, or user file paths are included. Nothing was uploaded.",
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _input_error(character_path: str | Path | None, secondary_path: str | Path | None, noun: str) -> PublicStudioResult | None:
        if not character_path or not secondary_path:
            return PublicStudioResult("需要输入", f"请上传一张角色图和一张{noun}。")
        character = Path(character_path).resolve()
        secondary = Path(secondary_path).resolve()
        if not character.is_file() or not secondary.is_file():
            return PublicStudioResult("需要输入", "上传的图片已失效，请重新选择。")
        return None

    def run(
        self,
        character_path: str | Path | None,
        pose_path: str | Path | None,
        mode_label: str,
        *,
        user_prompt: str = "",
        on_status: StatusCallback | None = None,
    ) -> PublicStudioResult:
        error = self._input_error(character_path, pose_path, "姿势参考图")
        if error is not None:
            return error
        if mode_label not in {"自动", "严格参考", "完整身体"}:
            return PublicStudioResult("设置无效", "请选择有效的迁移模式。")
        result = self.bridge.run(
            PublicBridgeRequest(
                Path(character_path).resolve(),  # type: ignore[arg-type]
                Path(pose_path).resolve(),  # type: ignore[arg-type]
                "pose_transfer",
                mode_label,
                user_prompt,
                public_beta_profile("pose_transfer"),
            ),
            on_status,
        )
        return self._bridge_result(result)

    def run_outfit(
        self,
        character_path: str | Path | None,
        outfit_path: str | Path | None,
        *,
        user_prompt: str = "",
        preserve_pose: bool = True,
        on_status: StatusCallback | None = None,
    ) -> PublicStudioResult:
        error = self._input_error(character_path, outfit_path, "服装参考图")
        if error is not None:
            return error
        prompt = user_prompt + ("；保持角色原图姿势" if preserve_pose else "")
        result = self.bridge.run(
            PublicBridgeRequest(
                Path(character_path).resolve(),  # type: ignore[arg-type]
                Path(outfit_path).resolve(),  # type: ignore[arg-type]
                "outfit_transfer",
                "一键换装",
                prompt,
                public_beta_profile("outfit_transfer"),
            ),
            on_status,
        )
        return self._bridge_result(result)
