"""Free 白名单包使用的基础渲染包编译器。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from .public_artifact_store import PublicJobContext
from .public_types import PublicWorkflowRequest
from .versioning import VERSIONS


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reference_preprocessing import (  # noqa: E402
    PASS,
    apply_preprocessing_prompt,
    lint_preprocessing_prompt,
    preprocess_run_references,
)
from reference_role_isolation import (  # noqa: E402
    apply_reference_role_isolation_to_prompt,
    build_reference_role_isolation,
    lint_reference_role_prompt,
)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _free_prompt(request: PublicWorkflowRequest, analysis: dict[str, Any]) -> str:
    character = analysis.get("character", {}) if isinstance(analysis.get("character"), dict) else {}
    pose = analysis.get("pose_blueprint", {}) if isinstance(analysis.get("pose_blueprint"), dict) else {}
    outfit_rule = (
        "Use the sanitized outfit reference only for garment structure, material, color, decoration, layering and shoes."
        if request.outfit_path is not None
        else "Preserve the character reference outfit without redesign."
    )
    return f"""# Free Generation Prompt

Create one character image from the sanitized references. Generate exactly once.

## Reference authority

- Character reference: identity, face, hair, body proportions, accessories, outfit authority and final visual domain.
- Pose reference: pose, crop, framing, camera, support and occlusion only.
- {outfit_rule}
- Never inherit pose-reference identity, clothing, style, lighting, text, background or scene.

## Visible character facts

{json.dumps(character, ensure_ascii=False, indent=2)}

## Visible pose facts

{json.dumps(pose, ensure_ascii=False, indent=2)}

## User request

{request.user_prompt or "No additional request."}

Preserve reference crop unless the user explicitly requested completion. Do not invent hidden anatomy.
After generation, perform one basic observation. Do not repair, retry or generate another candidate.
"""


def build_free_package(
    *,
    context: PublicJobContext,
    request: PublicWorkflowRequest,
    analysis: dict[str, Any],
    output_root: Path,
) -> Path:
    """编译仅含公共职责隔离、预处理和单次渲染所需资产的包。"""

    run_dir = (output_root / context.job_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    raw_input = {
        "version": VERSIONS.pipeline_schema,
        "character_images": [str(context.character_path)],
        "pose_reference": str(context.pose_path),
        "execution_mode": "FAST",
        "completion_intent": request.completion_intent,
        "workflow_type": request.workflow_kind.value,
        "final_rendering_domain_override": request.final_rendering_domain,
        "pose_composition_interpretation_mode": request.pose_composition_mode,
        "requirements": {
            "completion_intent": request.completion_intent,
            "user_prompt": request.user_prompt,
        },
        "product_versions": VERSIONS.as_dict(),
    }
    isolation = build_reference_role_isolation(raw_input, analysis)
    prompt = apply_reference_role_isolation_to_prompt(_free_prompt(request, analysis), isolation)
    role_violations = lint_reference_role_prompt(prompt, isolation)

    _write(run_dir / "input.normalized.json", raw_input)
    _write(run_dir / "reference_role_isolation.json", isolation)
    _write(
        run_dir / "composition_contract.json",
        {
            "schema_version": VERSIONS.pipeline_schema,
            "completion_intent": request.completion_intent,
            "workflow_type": request.workflow_kind.value,
            "reference_role_policy": "CHARACTER_REFERENCE_DOMINANT",
            "single_generation": True,
            "execution_mode": "FAST",
        },
    )
    _write(
        run_dir / "generation_constraints.yaml",
        {
            "schema_version": VERSIONS.pipeline_schema,
            "route": "FREE_FAST",
            "single_generation": True,
            "repair_allowed": False,
            "retry_allowed": False,
            "character_reference_authority": "IDENTITY_BODY_DOMAIN",
            "pose_reference_authority": "POSE_CROP_CAMERA_OCCLUSION_ONLY",
        },
    )
    (run_dir / "final_prompt.md").write_text(prompt, encoding="utf-8")

    package = run_dir / "render_package"
    references_dir = package / "references"
    references_dir.mkdir(parents=True, exist_ok=True)
    character_suffix = context.character_path.suffix.casefold() or ".png"
    pose_suffix = context.pose_path.suffix.casefold() if context.pose_path else ".png"
    raw_character = references_dir / f"character_original{character_suffix}"
    raw_pose = references_dir / f"pose_original{pose_suffix or '.png'}"
    shutil.copy2(context.character_path, raw_character)
    if context.pose_path is None:
        raise ValueError("Free 渲染包缺少姿势参考。")
    shutil.copy2(context.pose_path, raw_pose)
    _write(
        package / "manifest.json",
        {
            "schema_version": VERSIONS.pipeline_schema,
            "references": [
                {"role": "character", "file": raw_character.relative_to(package).as_posix()},
                {"role": "pose", "file": raw_pose.relative_to(package).as_posix()},
            ],
            "artifacts": ["final_prompt.md", "composition_contract.json"],
            "product_versions": VERSIONS.as_dict(),
        },
    )
    shutil.copy2(run_dir / "final_prompt.md", package / "final_prompt.md")
    shutil.copy2(run_dir / "input.normalized.json", package / "input.normalized.json")

    preprocessing = preprocess_run_references(run_dir, input_base=context.job_dir)
    if preprocessing.get("status") != PASS or preprocessing.get("renderer_handoff_authorized") is not True:
        raise RuntimeError("参考图预处理无法确认，Free 不会继续生成。")
    prompt = apply_preprocessing_prompt(prompt, 1)
    preprocessing_violations = lint_preprocessing_prompt(prompt)
    (run_dir / "final_prompt.md").write_text(prompt, encoding="utf-8")
    shutil.copy2(run_dir / "final_prompt.md", package / "final_prompt.md")

    violations = [*role_violations, *preprocessing_violations]
    _write(
        run_dir / "composition_conflicts.json",
        {
            "schema_version": VERSIONS.pipeline_schema,
            "render_authorized": not violations,
            "violations": violations,
            "route": "FREE_FAST",
        },
    )
    if violations:
        raise RuntimeError("Free 参考职责或预处理 Prompt 检查未通过。")

    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate_provenance_policy"] = {"expected_prompt_sha256": _sha(run_dir / "final_prompt.md")}
    manifest["product_versions"] = VERSIONS.as_dict()
    _write(manifest_path, manifest)
    _write(
        run_dir / "run_manifest.json",
        {
            "schema_version": VERSIONS.pipeline_schema,
            "route": "FREE_FAST",
            "files": sorted(path.relative_to(run_dir).as_posix() for path in run_dir.rglob("*") if path.is_file()),
            "product_versions": VERSIONS.as_dict(),
        },
    )
    return run_dir
