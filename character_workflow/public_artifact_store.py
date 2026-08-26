"""Public Beta 的路径安全、输入快照、缓存键和本地审计存储。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid
from typing import Any

from .public_types import PublicWorkflowRequest, PublicWorkflowState
from .versioning import VERSIONS


_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PublicJobContext:
    job_id: str
    job_dir: Path
    source_dir: Path
    character_path: Path
    pose_path: Path
    outfit_path: Path | None
    character_sha256: str
    pose_sha256: str
    outfit_sha256: str | None
    cache_key: str
    resumed: bool


class PublicArtifactStore:
    def __init__(self, app_root: Path):
        self.app_root = app_root.resolve()
        self.jobs_root = self.app_root / "jobs"
        self.outputs_root = self.app_root / "outputs"
        self.cache_root = self.app_root / "cache" / "public-workflow"
        for path in (self.jobs_root, self.outputs_root, self.cache_root):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_job_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("job-%Y%m%d-%H%M%S-")
        return stamp + uuid.uuid4().hex[:10]

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError("任务编号格式无效。")
        return job_id

    @staticmethod
    def _copy_source(source: Path, target_dir: Path, role: str) -> Path:
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"{role} 图片不存在。")
        suffix = source.suffix.casefold() or ".png"
        target = target_dir / f"{role}{suffix}"
        if not target.is_file() or sha256_file(target) != sha256_file(source):
            shutil.copy2(source, target)
        return target

    def prepare(self, request: PublicWorkflowRequest, provider_id: str, model_id: str) -> PublicJobContext:
        resumed = bool(request.resume_job_id)
        job_id = self._validate_job_id(request.resume_job_id) if resumed else self.new_job_id()
        job_dir = self.jobs_root / job_id
        source_dir = job_dir / "source_inputs"
        source_dir.mkdir(parents=True, exist_ok=True)
        request_record: dict[str, Any] | None = None

        if resumed:
            request_record = self.read_json(job_dir / "request.json")
            character = job_dir / request_record["saved_inputs"]["character"]
            pose = job_dir / request_record["saved_inputs"]["pose"]
            outfit_value = request_record["saved_inputs"].get("outfit")
            outfit = job_dir / outfit_value if outfit_value else None
        else:
            character = self._copy_source(request.character_path, source_dir, "character")
            pose = self._copy_source(request.pose_path, source_dir, "pose")
            outfit = self._copy_source(request.outfit_path, source_dir, "outfit") if request.outfit_path else None

        character_hash = sha256_file(character)
        pose_hash = sha256_file(pose)
        outfit_hash = sha256_file(outfit) if outfit else None
        identity = {
            "character_sha256": character_hash,
            "pose_sha256": pose_hash,
            "outfit_sha256": outfit_hash,
            "workflow_type": request.workflow_kind.value,
            "user_mode": request.user_mode,
            "execution_mode": "FAST",
            "completion_intent": request.completion_intent,
            "rendering_domain": request.final_rendering_domain,
            "pose_composition_mode": request.pose_composition_mode,
            "user_prompt_sha256": hashlib.sha256(request.user_prompt.encode("utf-8")).hexdigest(),
            "preserve_pose": request.preserve_pose,
            "prompt_schema_version": VERSIONS.skill_interface,
            "pipeline_schema_version": VERSIONS.pipeline_schema,
            "provider": provider_id,
            "model": model_id,
        }
        cache_key = _json_hash(identity)
        if resumed and request_record is not None:
            if request_record.get("cache_identity") != identity:
                self.audit(job_dir, "RESUME_REJECTED", {"reason": "cache_identity_changed"})
                raise ValueError("继续任务时的输入、模式、Provider 或版本与原任务不一致。")
            cache_key = str(request_record.get("cache_key") or cache_key)
        else:
            self.write_json(
                job_dir / "request.json",
                {
                    "job_id": job_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "request": {
                        **asdict(request),
                        "character_path": None,
                        "pose_path": None,
                        "outfit_path": None,
                        "workflow_kind": request.workflow_kind.value,
                        "resume_job_id": None,
                    },
                    "saved_inputs": {
                        "character": character.relative_to(job_dir).as_posix(),
                        "pose": pose.relative_to(job_dir).as_posix(),
                        "outfit": outfit.relative_to(job_dir).as_posix() if outfit else None,
                    },
                    "cache_identity": identity,
                    "cache_key": cache_key,
                    "versions": VERSIONS.as_dict(),
                },
            )
        return PublicJobContext(
            job_id,
            job_dir,
            source_dir,
            character,
            pose,
            outfit,
            character_hash,
            pose_hash,
            outfit_hash,
            cache_key,
            resumed,
        )

    @staticmethod
    def write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"期望 JSON 对象：{path.name}")
        return value

    def set_state(
        self,
        context: PublicJobContext,
        state: PublicWorkflowState,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "job_id": context.job_id,
            "state": state.name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "details": details or {},
            "versions": VERSIONS.as_dict(),
        }
        self.write_json(context.job_dir / "state.json", payload)
        self.audit(context.job_dir, "STATE_CHANGED", {"state": state.name, **(details or {})})

    def audit(self, job_dir: Path, event: str, details: dict[str, Any] | None = None) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details or {},
        }
        with (job_dir / "audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def cache_path(self, cache_key: str, analysis_kind: str) -> Path:
        safe_kind = re.sub(r"[^A-Za-z0-9_-]", "_", analysis_kind)
        return self.cache_root / cache_key[:2] / cache_key / f"{safe_kind}.json"

    def read_cache(self, context: PublicJobContext, analysis_kind: str) -> dict[str, Any] | None:
        path = self.cache_path(context.cache_key, analysis_kind)
        if not path.is_file():
            self.audit(context.job_dir, "CACHE_MISS", {"kind": analysis_kind})
            return None
        try:
            value = self.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            self.audit(context.job_dir, "CACHE_MISS", {"kind": analysis_kind, "reason": "invalid"})
            return None
        self.audit(context.job_dir, "CACHE_HIT", {"kind": analysis_kind})
        return value

    def write_cache(self, context: PublicJobContext, analysis_kind: str, value: dict[str, Any]) -> Path:
        path = self.cache_path(context.cache_key, analysis_kind)
        self.write_json(path, value)
        self.audit(context.job_dir, "CACHE_SAVED", {"kind": analysis_kind})
        return path
