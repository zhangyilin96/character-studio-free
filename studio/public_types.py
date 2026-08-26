"""Public Beta Studio 对 UI 暴露的最小结果对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PublicStudioResult:
    status: str
    message: str
    output_dir: Path | None = None
    result_path: Path | None = None
    artifact_path: Path | None = None
    job_id: str | None = None
    retryable: bool = False
    checks: tuple[str, ...] = field(default_factory=tuple)
    failure_reason: str = ""
    backend: str = "codex_exec"
