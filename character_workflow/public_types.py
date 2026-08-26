"""Public Beta 快速工作流的独立输入、状态与输出类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class PublicWorkflowKind(StrEnum):
    POSE_TRANSFER = "POSE_TRANSFER"
    OUTFIT_TRANSFER = "OUTFIT_TRANSFER"


class PublicWorkflowState(StrEnum):
    RECEIVED = "已接收"
    FAST_GENERATING = "快速生成中"
    FAST_CHECKING = "快速检查中"
    FAST_PASSED = "快速检查通过"
    ADVANCED_CASE_NOT_SUPPORTED = "当前 Beta 暂时无法可靠处理这个输入"
    COMPLETED = "已完成"
    FAILED = "已失败"


@dataclass(frozen=True)
class PublicWorkflowRequest:
    character_path: Path
    pose_path: Path
    outfit_path: Path | None = None
    workflow_kind: PublicWorkflowKind = PublicWorkflowKind.POSE_TRANSFER
    user_mode: str = "标准复刻（推荐）"
    execution_mode: str = "FAST"
    completion_intent: str = "STRICT_REFERENCE_REPLICA"
    final_rendering_domain: str | None = None
    pose_composition_mode: str | None = None
    user_prompt: str = ""
    preserve_pose: bool = True
    resume_job_id: str | None = None

    def __post_init__(self) -> None:
        if self.execution_mode != "FAST":
            raise ValueError("Public Beta 只允许 FAST 执行模式。")


@dataclass(frozen=True)
class PublicWorkflowResult:
    job_id: str
    state: PublicWorkflowState
    status: str
    message: str
    output_dir: Path
    result_path: Path | None = None
    artifact_path: Path | None = None
    retryable: bool = False
    request_id: str | None = None
    details: tuple[str, ...] = field(default_factory=tuple)
    evidence: dict[str, Any] = field(default_factory=dict)
