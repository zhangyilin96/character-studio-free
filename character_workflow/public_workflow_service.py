"""Public Beta 独立快速工作流：一次生成、基础检查、严格交付。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import sys
from typing import Any, Callable

from .free_delivery import finalize_free_candidate
from .free_pipeline import build_free_package
from .outfit_reference_service import attach_outfit_reference
from .public_artifact_store import PublicArtifactStore, PublicJobContext
from .public_types import (
    PublicWorkflowKind,
    PublicWorkflowRequest,
    PublicWorkflowResult,
    PublicWorkflowState,
)


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from renderer_adapter_v18 import compile_renderer_request_v18, verify_v18_attempt  # noqa: E402


StatusCallback = Callable[[str], None]


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"期望 JSON 对象：{path.name}")
    return value


def _risk_reasons(analysis: dict[str, Any], request: PublicWorkflowRequest) -> list[str]:
    reasons: list[str] = []
    if request.completion_intent == "FULLBODY_EXPANSION":
        reasons.append("fullbody_expansion")
    if request.pose_composition_mode == "STYLIZED_POSE_GRAMMAR":
        reasons.append("stylized_pose_grammar")
    preflight = analysis.get("preflight", {}) if isinstance(analysis.get("preflight"), dict) else {}
    for key in (
        "occlusion_risk",
        "left_right_ambiguity",
        "support_contact_complexity",
        "camera_extremity",
        "hidden_region_risk",
    ):
        if str(preflight.get(key, "")).casefold() == "high":
            reasons.append(key)
    flags = preflight.get("flags", {}) if isinstance(preflight.get("flags"), dict) else {}
    for key in (
        "strong_perspective",
        "extreme_foreshortening",
        "crossed_limbs",
        "hidden_lower_body",
        "foreground_limb_enlargement",
    ):
        if flags.get(key) is True:
            reasons.append(key)
    return list(dict.fromkeys(reasons))


def _quick_status(observation: dict[str, Any]) -> tuple[str, list[str]]:
    quick = observation.get("quick_check", {})
    if not isinstance(quick, dict):
        return "NOT_ASSESSABLE", ["quick_check_missing"]
    status = str(quick.get("status", "NOT_ASSESSABLE"))
    if status not in {"PASS", "FAIL", "NOT_ASSESSABLE"}:
        status = "NOT_ASSESSABLE"
    reasons = quick.get("reasons", [])
    return status, [str(value) for value in reasons] if isinstance(reasons, list) else []


class PublicCharacterWorkflow:
    """没有精细入口或第二次生成能力的公开编排器。"""

    def __init__(self, app_root: Path, logger: logging.Logger | None = None):
        self.store = PublicArtifactStore(app_root)
        self.logger = logger or logging.getLogger("character_public_workflow")

    def _set_state(
        self,
        context: PublicJobContext,
        state: PublicWorkflowState,
        notify: StatusCallback,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.store.set_state(context, state, details)
        notify(state.value)

    def _analysis(self, context: PublicJobContext, provider: Any, calls: dict[str, int]) -> dict[str, Any]:
        cache_kind = "reference-analysis-quick"
        cached = self.store.read_cache(context, cache_kind)
        if cached is not None:
            return cached
        value = provider.analyze_references(
            context.character_path,
            context.pose_path,
            detail="quick",
            outfit=context.outfit_path,
        )
        calls["reference_analysis"] += 1
        value["analysis_profile"] = "QUICK"
        self.store.write_cache(context, cache_kind, value)
        return value

    def _package(
        self,
        context: PublicJobContext,
        request: PublicWorkflowRequest,
        analysis: dict[str, Any],
    ) -> Path:
        run_dir = self.store.outputs_root / context.job_id
        if run_dir.is_dir():
            conflicts = _read_object(run_dir / "composition_conflicts.json")
            if conflicts.get("render_authorized") is not True:
                raise RuntimeError("已保存的流水线包未授权渲染。")
            return run_dir
        run_dir = build_free_package(
            context=context,
            request=request,
            analysis=analysis,
            output_root=self.store.outputs_root,
        )
        if request.workflow_kind is PublicWorkflowKind.OUTFIT_TRANSFER:
            if context.outfit_path is None:
                raise ValueError("一键换装需要服装参考图。")
            attach_outfit_reference(run_dir, context.outfit_path, preserve_pose=request.preserve_pose)
        return run_dir

    @staticmethod
    def _existing_attempt(run_dir: Path) -> tuple[Path, Path] | None:
        attempts = run_dir / "renderer_attempts"
        if not attempts.is_dir():
            return None
        for receipt_path in sorted(attempts.glob("*/renderer_request_receipt.json"), reverse=True):
            try:
                receipt = _read_object(receipt_path)
                candidate = Path(str(receipt.get("candidate_output", ""))).resolve()
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if receipt.get("status") == "COMPLETED" and receipt.get("execution_proof") is True and candidate.is_file():
                return receipt_path, candidate
        return None

    def _render(
        self,
        context: PublicJobContext,
        request: PublicWorkflowRequest,
        provider: Any,
        run_dir: Path,
        calls: dict[str, int],
    ) -> tuple[Path, Path]:
        existing = self._existing_attempt(run_dir)
        if existing is not None:
            return existing
        capabilities = provider.capabilities()
        attempt_dir, _request = compile_renderer_request_v18(
            run_dir,
            capabilities.renderer_backend,
            "candidate_workflow_01.png",
            completion_intent_override=request.completion_intent,
            model=capabilities.model_id,
            quality="high",
            output_format="png",
        )
        verify_v18_attempt(attempt_dir)
        receipt = provider.render_reference_edit(attempt_dir)
        calls["renderer_generation"] += 1
        receipt_path = attempt_dir / "renderer_request_receipt.json"
        candidate = Path(str(receipt.get("candidate_output", ""))).resolve()
        if not receipt_path.is_file() or not candidate.is_file():
            raise RuntimeError("生成服务未产生可验证的 receipt 和候选图。")
        return receipt_path, candidate

    def _observe(
        self,
        context: PublicJobContext,
        provider: Any,
        candidate: Path,
        run_dir: Path,
        calls: dict[str, int],
    ) -> tuple[dict[str, Any], Path]:
        path = run_dir / f"{candidate.stem}.quick.observed.json"
        if path.is_file():
            return _read_object(path), path
        value = provider.observe_candidate(
            context.character_path,
            context.pose_path,
            candidate,
            run_dir,
            detail="quick",
            outfit=context.outfit_path,
        )
        calls["quick_candidate_observation"] += 1
        self.store.write_json(path, value)
        return value, path

    def _unsupported(
        self,
        context: PublicJobContext,
        notify: StatusCallback,
        calls: dict[str, int],
        reasons: list[str],
        *,
        artifact_path: Path | None = None,
        candidate_generated: bool = False,
    ) -> PublicWorkflowResult:
        self._set_state(
            context,
            PublicWorkflowState.ADVANCED_CASE_NOT_SUPPORTED,
            notify,
            {
                "reasons": reasons,
                "candidate_generated": candidate_generated,
                "candidate_returned": False,
            },
        )
        self.store.audit(
            context.job_dir,
            "ADVANCED_CASE_NOT_SUPPORTED",
            {
                "reasons": reasons,
                "candidate_generated": candidate_generated,
                "candidate_returned": False,
                "generation_count": calls["renderer_generation"],
            },
        )
        return PublicWorkflowResult(
            context.job_id,
            PublicWorkflowState.ADVANCED_CASE_NOT_SUPPORTED,
            "当前 Beta 暂时无法可靠处理这个输入",
            "这个案例没有通过当前 Beta 的基础质量检查，因此没有直接交付可能存在明显错误的结果。",
            context.job_dir,
            artifact_path=artifact_path or context.job_dir,
            retryable=True,
            request_id=context.job_id,
            details=tuple(reasons),
            evidence={
                "path": "PUBLIC_BETA_BOUNDARY",
                "failure_reason": "ADVANCED_CASE_NOT_SUPPORTED",
                "model_calls": calls,
                "candidate_returned": False,
            },
        )

    def run(
        self,
        request: PublicWorkflowRequest,
        provider: Any,
        on_status: StatusCallback | None = None,
    ) -> PublicWorkflowResult:
        notify = on_status or (lambda _status: None)
        capabilities = provider.capabilities()
        context = self.store.prepare(request, capabilities.provider_id, capabilities.model_id)
        calls = {
            "reference_analysis": 0,
            "renderer_generation": 0,
            "quick_candidate_observation": 0,
        }
        self._set_state(context, PublicWorkflowState.RECEIVED, notify, {"resumed": context.resumed})
        try:
            health = provider.health_check()
            if not health.available:
                return PublicWorkflowResult(
                    context.job_id,
                    PublicWorkflowState.FAILED,
                    "生成服务不可用",
                    health.user_message,
                    context.job_dir,
                    retryable=health.retryable,
                    request_id=context.job_id,
                    evidence={"provider_code": health.diagnostic_code, "model_calls": calls},
                )
            completed = context.job_dir / "result.png"
            if context.resumed and completed.is_file():
                self._set_state(context, PublicWorkflowState.COMPLETED, notify, {"reused_completed_result": True})
                return PublicWorkflowResult(
                    context.job_id,
                    PublicWorkflowState.COMPLETED,
                    "已完成",
                    "已恢复之前通过交付检查的结果。",
                    context.job_dir,
                    completed,
                    completed,
                    request_id=context.job_id,
                    evidence={"resumed": True, "model_calls": calls},
                )

            self._set_state(context, PublicWorkflowState.FAST_GENERATING, notify)
            analysis = self._analysis(context, provider, calls)
            reasons = _risk_reasons(analysis, request)
            if reasons:
                return self._unsupported(context, notify, calls, reasons)

            run_dir = self._package(context, request, analysis)
            receipt_path, candidate = self._render(context, request, provider, run_dir, calls)
            self._set_state(context, PublicWorkflowState.FAST_CHECKING, notify)
            observation, observation_path = self._observe(context, provider, candidate, run_dir, calls)
            quick_status, quick_reasons = _quick_status(observation)
            if quick_status != "PASS":
                return self._unsupported(
                    context,
                    notify,
                    calls,
                    quick_reasons or [quick_status],
                    artifact_path=run_dir,
                    candidate_generated=True,
                )
            finalized, delivery = finalize_free_candidate(
                provider=provider,
                run_dir=run_dir,
                candidate=candidate,
                observation_path=observation_path,
                receipt_path=receipt_path,
            )
            final_path = run_dir / "final.png"
            if delivery.get("candidate_returned") is not True or not final_path.is_file():
                return self._unsupported(
                    context,
                    notify,
                    calls,
                    ["strict_delivery_rejected"],
                    artifact_path=run_dir,
                    candidate_generated=True,
                )
            self._set_state(context, PublicWorkflowState.FAST_PASSED, notify)
            result_path = context.job_dir / "result.png"
            shutil.copy2(final_path, result_path)
            self._set_state(context, PublicWorkflowState.COMPLETED, notify, {"early_exit": True})
            return PublicWorkflowResult(
                context.job_id,
                PublicWorkflowState.COMPLETED,
                "已完成",
                "基础检查通过，已完成严格交付。",
                run_dir,
                result_path,
                final_path,
                request_id=context.job_id,
                evidence={
                    "path": "FAST_EARLY_EXIT",
                    "model_calls": calls,
                    "delivery": delivery,
                    "finalizer": finalized,
                },
            )
        except Exception as exc:
            self.logger.exception("public workflow failed job_id=%s", context.job_id)
            self._set_state(context, PublicWorkflowState.FAILED, notify, {"error_type": type(exc).__name__})
            return PublicWorkflowResult(
                context.job_id,
                PublicWorkflowState.FAILED,
                "生成失败",
                "任务未完成。已保留本地诊断，可调整输入后重试。",
                context.job_dir,
                artifact_path=context.job_dir,
                retryable=True,
                request_id=context.job_id,
                details=(type(exc).__name__,),
                evidence={"model_calls": calls},
            )


def build_public_pose_request(
    character: Path,
    pose: Path,
    mode_label: str,
    *,
    user_prompt: str = "",
    resume_job_id: str | None = None,
) -> PublicWorkflowRequest:
    modes = {
        "自动": ("STRICT_REFERENCE_REPLICA", None),
        "严格参考": ("STRICT_REFERENCE_REPLICA", None),
        "完整身体": ("FULLBODY_EXPANSION", None),
        "标准复刻（推荐）": ("STRICT_REFERENCE_REPLICA", None),
        "完整补全": ("FULLBODY_EXPANSION", None),
    }
    try:
        completion_intent, composition_mode = modes[mode_label]
    except KeyError as exc:
        raise ValueError("请选择有效的生成模式。") from exc
    return PublicWorkflowRequest(
        character_path=character,
        pose_path=pose,
        workflow_kind=PublicWorkflowKind.POSE_TRANSFER,
        user_mode=mode_label,
        completion_intent=completion_intent,
        pose_composition_mode=composition_mode,
        user_prompt=user_prompt,
        resume_job_id=resume_job_id,
    )


def build_public_outfit_request(
    character: Path,
    outfit: Path,
    *,
    user_prompt: str = "",
    preserve_pose: bool = True,
    resume_job_id: str | None = None,
) -> PublicWorkflowRequest:
    return PublicWorkflowRequest(
        character_path=character,
        pose_path=character,
        outfit_path=outfit,
        workflow_kind=PublicWorkflowKind.OUTFIT_TRANSFER,
        user_mode="一键换装",
        user_prompt=user_prompt,
        preserve_pose=preserve_pose,
        resume_job_id=resume_job_id,
    )
