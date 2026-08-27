"""Free 公开候选检查：只做布尔型基础检查，不做评分或修复规划。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .quick_reference_analysis import json_response


FREE_HARD_CHECKS = (
    "identity_and_head_traits_consistent",
    "garment_structure_has_no_obvious_error",
    "overall_pose_matches",
    "limb_count_valid",
    "left_right_limb_ownership_valid",
    "body_and_feet_connected",
    "rendering_domain_consistent",
)


def _quick_status(observed: dict[str, Any]) -> tuple[str, list[str]]:
    explicit = observed.get("quick_check", {})
    if isinstance(explicit, dict) and explicit.get("status") in {"PASS", "FAIL", "NOT_ASSESSABLE"}:
        reasons = explicit.get("reasons", [])
        return str(explicit["status"]), [str(value) for value in reasons] if isinstance(reasons, list) else []
    hard = observed.get("hard_constraints", {}) if isinstance(observed.get("hard_constraints"), dict) else {}
    failed = [name for name in FREE_HARD_CHECKS if hard.get(name) is False]
    missing = [name for name in FREE_HARD_CHECKS if hard.get(name) is not True]
    domain = observed.get("character_rendering_domain_observation", {})
    if failed:
        return "FAIL", failed
    if missing or not isinstance(domain, dict) or domain.get("observed_domain") in {None, "UNKNOWN"}:
        return "NOT_ASSESSABLE", [*missing, "rendering_domain_unknown"]
    return "PASS", []


def observe_candidate_quick(
    client: Any,
    model: str,
    character_path: Path,
    pose_path: Path,
    candidate_path: Path,
    run_dir: Path,
    *,
    outfit_path: Path | None = None,
) -> dict[str, Any]:
    candidate_index = 4 if outfit_path is not None else 3
    outfit_rule = (
        "Image 3 controls garment structure, material, color, decoration, layering and shoes only."
        if outfit_path is not None
        else "Image 1 also controls outfit."
    )
    prompt = f"""Quickly inspect image {candidate_index} as the single generated result.
Image 1 controls identity, head traits, body and final visual domain. Image 2 controls pose,
crop, occlusion and camera. {outfit_rule} Return JSON only.

This is an obvious-error delivery screen, not a detailed diagnostic. Required keys:
- quick_check: status PASS, FAIL or NOT_ASSESSABLE plus reasons array.
- character_rendering_domain_observation: observed_domain from PHOTOREALISTIC, ANIME,
  ILLUSTRATION, 3D_RENDER or UNKNOWN, plus short evidence.
- hard_constraints: exactly these boolean-or-null keys: {list(FREE_HARD_CHECKS)}.
- region_results: face, torso, pelvis, arms, hands, legs and feet as true/false/null.
- failures and warnings arrays.

PASS only when every required hard check is visibly true. Use NOT_ASSESSABLE when a core fact
cannot be seen. Do not calculate scores, run a detailed check, create a repair plan, repair the
image or request another generation."""
    images = [character_path, pose_path]
    if outfit_path is not None:
        images.append(outfit_path)
    images.append(candidate_path)
    observed = json_response(client, model, prompt, images, reasoning_effort="low")
    status, reasons = _quick_status(observed)
    observed["quick_check"] = {"status": status, "reasons": reasons}
    observed.update(
        {
            "candidate": candidate_path.name,
            "candidate_id": candidate_path.stem,
            "candidate_kind": "initial",
            "observation_profile": "QUICK",
            "candidate_provenance": {
                "generated_from_current_render_package": True,
                "current_prompt_match": True,
                "preexisting_candidate_reused": False,
            },
        }
    )
    return observed
