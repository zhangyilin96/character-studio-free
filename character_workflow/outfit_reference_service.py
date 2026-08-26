"""一键换装的服装参考净化、职责隔离与渲染包接入。"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reference_preprocessing import PASS, PREPROCESSING_SCHEMA_VERSION, character_cutout  # noqa: E402

from .versioning import VERSIONS


OUTFIT_AUTHORITY = {
    "garment_structure": "HARD",
    "garment_material": "HARD",
    "garment_color": "HARD",
    "garment_decoration": "HARD",
    "garment_layering": "HARD",
    "character_identity": "NONE",
    "face": "NONE",
    "hairstyle": "NONE",
    "earrings_and_identity_accessories": "NONE",
    "body_proportions": "NONE",
    "pose": "NONE",
    "crop_and_framing": "NONE",
    "background": "NONE",
    "scene": "NONE",
    "lighting": "NONE",
    "rendering_domain": "NONE",
}


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"期望 JSON 对象：{path.name}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _outfit_prompt_block(preserve_pose: bool) -> str:
    pose_rule = (
        "Image C is the sanitized original-pose reference and is HARD authority for pose, crop, framing, "
        "occlusion, support and perspective."
        if preserve_pose
        else
        "Image C is a SOFT original-composition guide; keep the person recognizable and change the stance "
        "only as much as garment fitting requires."
    )
    return "\n".join(
        [
            "Reference Preprocessing / Sanitized Input Roles (outfit transfer):",
            "- Image A is the character cutout and the only authority for identity, face, hairstyle, earrings, "
            "body proportions, identity accessories, and final rendering domain.",
            "- Image A is not garment authority in this workflow. Do not preserve its old clothing when it "
            "conflicts with Image B.",
            "- Image B is the sanitized outfit reference and the only authority for garment structure, material, "
            "color, decoration, and layering.",
            "- Image B has no authority over identity, face, hair, body, pose, crop, background, scene, lighting, "
            "or rendering domain.",
            f"- {pose_rule}",
            "- Do not add safety shorts, coats, undershirts, sleeves, or other garment changes unless a renderer "
            "safety policy requires the smallest equivalent adjustment.",
            "- Do not inherit any removed background, text, watermark, unrelated person, or scene semantics.",
            "- Raw full-image references are audit-only and are not renderer inputs.",
            "",
        ]
    )


def _replace_preprocessing_prompt(run_dir: Path, preserve_pose: bool) -> None:
    prompt_path = run_dir / "final_prompt.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    marker = "Reference Preprocessing / Sanitized Input Roles (v1.8.1):"
    anchor = "## Character Identity Lock"
    start = prompt.find(marker)
    end = prompt.find(anchor, start if start >= 0 else 0)
    block = _outfit_prompt_block(preserve_pose)
    if start >= 0 and end >= 0:
        prompt = prompt[:start] + block + "\n" + prompt[end:]
    elif end >= 0:
        prompt = prompt[:end] + block + "\n" + prompt[end:]
    else:
        prompt = block + "\n" + prompt
    prompt_path.write_text(prompt, encoding="utf-8")


def _sync_changed_artifacts(run_dir: Path, artifact_names: list[str]) -> None:
    prompt_path = run_dir / "final_prompt.md"
    prompt_sha = _sha(prompt_path)
    package = run_dir / "render_package"
    shutil.copy2(prompt_path, package / "final_prompt.md")
    for name in artifact_names:
        source = run_dir / name
        if source.is_file():
            shutil.copy2(source, package / name)

    package_manifest_path = package / "manifest.json"
    package_manifest = _read(package_manifest_path)
    artifacts = list(package_manifest.get("artifacts", []))
    for name in artifact_names:
        if name not in artifacts:
            artifacts.append(name)
    package_manifest["artifacts"] = artifacts
    package_manifest["product_versions"] = VERSIONS.as_dict()
    policy = dict(package_manifest.get("candidate_provenance_policy", {}) or {})
    policy["expected_prompt_sha256"] = prompt_sha
    package_manifest["candidate_provenance_policy"] = policy
    _write(package_manifest_path, package_manifest)

    run_manifest_path = run_dir / "run_manifest.json"
    if run_manifest_path.is_file():
        run_manifest = _read(run_manifest_path)
        files = list(run_manifest.get("files", []))
        checksums = dict(run_manifest.get("checksums", {}) or {})
        for name in ["final_prompt.md", *artifact_names]:
            source = run_dir / name
            if source.is_file():
                if name not in files:
                    files.append(name)
                checksums[name] = _sha(source)
        run_manifest["files"] = files
        run_manifest["checksums"] = checksums
        run_manifest["product_versions"] = VERSIONS.as_dict()
        policy = dict(run_manifest.get("candidate_provenance_policy", {}) or {})
        policy["expected_prompt_sha256"] = prompt_sha
        run_manifest["candidate_provenance_policy"] = policy
        _write(run_manifest_path, run_manifest)


def attach_outfit_reference(run_dir: Path, outfit_path: Path, *, preserve_pose: bool) -> dict[str, Any]:
    """净化服装参考并把它插入角色与姿势之间；原图只保留作审计。"""
    run_dir = run_dir.resolve()
    outfit_path = outfit_path.resolve()
    result = character_cutout(outfit_path, run_dir, artifact_prefix="outfit_sanitized")
    meta = deepcopy(result["meta"])
    meta["module"] = "OUTFIT_REFERENCE_SANITIZATION"
    meta["authority"] = deepcopy(OUTFIT_AUTHORITY)
    meta["policy"] = {
        "purpose": "isolate_garment_authority_for_explicit_outfit_transfer",
        "identity_authority": False,
        "pose_authority": False,
        "scene_authority": False,
        "raw_outfit_scene_is_renderer_authority": False,
        "safety_bypass_purpose": False,
    }
    meta["outputs"] = {
        "outfit_sanitized": result["cutout_path"].name,
        "outfit_sanitized_mask": result["mask_path"].name,
        "outfit_sanitized_sha256": _sha(result["cutout_path"]),
        "outfit_sanitized_mask_sha256": _sha(result["mask_path"]),
        "background": "transparent",
        "canvas_preserved": True,
    }
    meta["limitations"] = [
        "服装净化隔离职责，但不能证明渲染器已逐像素复刻服装。",
        "若服装主体分割不可判断，渲染交接会关闭。",
        "安全策略导致的最小等价调整需要由最终结果观察确认；当前不能从请求回执自动证明。",
    ]
    _write(result["meta_path"], meta)
    if meta.get("status") != PASS:
        raise RuntimeError("服装参考主体无法可靠分离，请换用背景更干净的服装图或提供显式遮罩。")

    authority_path = run_dir / "outfit_reference_authority.json"
    _write(
        authority_path,
        {
            "schema_version": PREPROCESSING_SCHEMA_VERSION,
            "workflow_type": "OUTFIT_TRANSFER",
            "preserve_pose": preserve_pose,
            "authority": deepcopy(OUTFIT_AUTHORITY),
            "unrequested_garment_additions": "FORBIDDEN",
            "safety_adjustment_policy": "MINIMUM_EQUIVALENT_ONLY_AND_DISCLOSE_IF_OBSERVED",
            "safety_adjustment_observation": "NOT_ASSESSABLE",
            "product_versions": VERSIONS.as_dict(),
        },
    )

    package = run_dir / "render_package"
    manifest_path = package / "manifest.json"
    manifest = _read(manifest_path)
    references = manifest.get("references", [])
    if not isinstance(references, list) or not references or references[-1].get("representation") != "POSE_SANITIZED":
        raise RuntimeError("服装参考只能接到已有的角色净化图和姿势净化图之间。")
    references_dir = package / "references"
    sanitized_destination = references_dir / "outfit_sanitized.png"
    shutil.copy2(result["cutout_path"], sanitized_destination)
    outfit_record = {
        "role": "outfit",
        "file": sanitized_destination.relative_to(package).as_posix(),
        "representation": "OUTFIT_SANITIZED",
        "preprocessing_meta": result["meta_path"].name,
        "preprocessing_sha256": _sha(result["meta_path"]),
        "authority": deepcopy(OUTFIT_AUTHORITY),
        "source_reference_transmitted": False,
    }
    manifest["references"] = [*references[:-1], outfit_record, references[-1]]

    originals = package / "source_references"
    originals.mkdir(parents=True, exist_ok=True)
    raw_audit = originals / f"outfit_reference_original{outfit_path.suffix.casefold() or '.png'}"
    shutil.copy2(outfit_path, raw_audit)
    source_records = list(manifest.get("source_references", []))
    source_records.append(
        {
            "role": "outfit",
            "file": raw_audit.relative_to(package).as_posix(),
            "sha256": _sha(raw_audit),
            "transmitted_to_renderer": False,
        }
    )
    manifest["source_references"] = source_records
    manifest["workflow_type"] = "OUTFIT_TRANSFER"
    manifest["outfit_reference_authority"] = "outfit_reference_authority.json"
    _write(manifest_path, manifest)

    preprocessing_path = run_dir / "reference_preprocessing.json"
    preprocessing = _read(preprocessing_path)
    preprocessing["outfit_sanitization"] = {
        "status": meta["status"],
        "image": result["cutout_path"].name,
        "mask": result["mask_path"].name,
        "meta": result["meta_path"].name,
        "authority_artifact": authority_path.name,
    }
    preprocessing["renderer_reference_order"] = [
        *preprocessing.get("renderer_reference_order", [])[:-1],
        {"index": len(references), "role": "OUTFIT", "representation": "OUTFIT_SANITIZED", "authority": deepcopy(OUTFIT_AUTHORITY)},
        {
            **preprocessing.get("renderer_reference_order", [])[-1],
            "index": len(references) + 1,
        },
    ]
    _write(preprocessing_path, preprocessing)

    constraints_path = run_dir / "generation_constraints.yaml"
    constraints = _read(constraints_path)
    constraints["workflow_type"] = "OUTFIT_TRANSFER"
    constraints["outfit_transfer_authority"] = {
        "character_reference": "IDENTITY_FACE_HAIR_BODY_BASE_POSE_ONLY",
        "outfit_reference": "GARMENT_STRUCTURE_MATERIAL_COLOR_DECORATION_ONLY",
        "pose_reference": "ORIGINAL_CHARACTER_POSE" if preserve_pose else "SOFT_ORIGINAL_COMPOSITION",
        "unrequested_garment_additions": "FORBIDDEN",
    }
    _write(constraints_path, constraints)
    constraint_set_path = run_dir / "constraint_set.yaml"
    constraint_set = _read(constraint_set_path)
    constraint_set["generation_constraints"] = deepcopy(constraints)
    _write(constraint_set_path, constraint_set)

    conflicts_path = run_dir / "composition_conflicts.json"
    conflicts = _read(conflicts_path)
    conflicts["outfit_reference_role"] = {
        "status": "PASS",
        "identity_authority": "NONE",
        "garment_authority": "HARD",
        "reference_order_valid": True,
    }
    _write(conflicts_path, conflicts)

    _replace_preprocessing_prompt(run_dir, preserve_pose)
    artifacts = [
        result["cutout_path"].name,
        result["mask_path"].name,
        result["meta_path"].name,
        authority_path.name,
        preprocessing_path.name,
        constraints_path.name,
        constraint_set_path.name,
        conflicts_path.name,
    ]
    _sync_changed_artifacts(run_dir, artifacts)
    return {
        "status": "PASS",
        "sanitized_path": result["cutout_path"],
        "mask_path": result["mask_path"],
        "meta_path": result["meta_path"],
        "authority_path": authority_path,
        "renderer_order": ["CHARACTER_CUTOUT", "OUTFIT_SANITIZED", "POSE_SANITIZED"],
    }
