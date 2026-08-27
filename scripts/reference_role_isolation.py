#!/usr/bin/env python3
"""v1.8 reference-role isolation and character rendering-domain gate."""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any, Iterable


SCHEMA_VERSION = "1.8.0"

CHARACTER_REFERENCE_DOMINANT = "CHARACTER_REFERENCE_DOMINANT"
PHOTOREALISTIC = "PHOTOREALISTIC"
ANIME = "ANIME"
ILLUSTRATION = "ILLUSTRATION"
THREE_D_RENDER = "3D_RENDER"
UNKNOWN = "UNKNOWN"
FINAL_RENDERING_DOMAINS = (PHOTOREALISTIC, ANIME, ILLUSTRATION, THREE_D_RENDER, UNKNOWN)

STYLIZED_POSE_GRAMMAR = "STYLIZED_POSE_GRAMMAR"
REALISTIC_POSE_GRAMMAR = "REALISTIC_POSE_GRAMMAR"
REFERENCE_LITERAL_GEOMETRY = "REFERENCE_LITERAL_GEOMETRY"
POSE_COMPOSITION_MODES = (
    STYLIZED_POSE_GRAMMAR,
    REALISTIC_POSE_GRAMMAR,
    REFERENCE_LITERAL_GEOMETRY,
)

CHARACTER_AUTHORITY_FIELDS = (
    "character_identity",
    "face_morphology",
    "rendering_domain",
    "realism_level",
    "skin_rendering",
    "hair_rendering",
    "body_proportions",
    "shoulder_width",
    "torso_mass",
    "waist_hip_proportion",
    "limb_thickness",
    "garment_identity",
    "garment_structure",
    "material_language",
    "accessories",
)

POSE_ALLOWED_FIELDS = (
    "pose_topology",
    "joint_arrangement",
    "limb_direction",
    "silhouette",
    "crop",
    "framing",
    "occlusion",
    "perspective",
    "foreshortening",
    "camera_angle",
    "foreground_background_ordering",
    "torso_pelvis_tilt",
    "action_energy",
    "visible_range_relationship",
)

POSE_FORBIDDEN_FIELDS = (
    "face_style",
    "rendering_domain",
    "skin_style",
    "character_identity",
    "body_mass",
    "sex_linked_proportions",
    "garment_identity",
    "material_style",
    "color_palette",
    "lighting_style",
    "scene_semantics",
    "text_captions",
    "background_style",
)

REFERENCE_ROLE_LEAKAGE = {
    "body_mass_leakage": "pose_reference_body_mass_leakage",
    "sex_linked_proportion_leakage": "pose_reference_sex_linked_proportion_leakage",
    "rendering_domain_leakage": "pose_reference_rendering_domain_leakage",
    "garment_identity_leakage": "pose_reference_garment_identity_leakage",
    "material_style_leakage": "pose_reference_material_style_leakage",
    "scene_style_leakage": "pose_reference_scene_style_leakage",
}

VALIDATION_PRIORITY = (
    "character_domain_and_identity_integrity",
    "gross_topology_integrity",
    "pose_and_composition_adherence",
    "local_limb_quality",
    "visual_quality",
)

DOMAIN_FAILURE_FLAGS = (
    "anime_face",
    "oversized_anime_eyes",
    "illustration_shading",
    "cel_shading",
    "plastic_figure_rendering",
    "manga_facial_morphology",
)

_DOMAIN_ALIASES = {
    "PHOTOREALISTIC": PHOTOREALISTIC,
    "PHOTOGRAPHIC": PHOTOREALISTIC,
    "PHOTO": PHOTOREALISTIC,
    "LIVE_ACTION": PHOTOREALISTIC,
    "REALISTIC_PHOTO": PHOTOREALISTIC,
    "真人": PHOTOREALISTIC,
    "写实摄影": PHOTOREALISTIC,
    "ANIME": ANIME,
    "MANGA": ANIME,
    "MANGA_ANIME": ANIME,
    "二次元": ANIME,
    "漫画": ANIME,
    "ILLUSTRATION": ILLUSTRATION,
    "ILLUSTRATED": ILLUSTRATION,
    "PAINTERLY": ILLUSTRATION,
    "插画": ILLUSTRATION,
    "3D": THREE_D_RENDER,
    "3D_RENDER": THREE_D_RENDER,
    "CGI": THREE_D_RENDER,
    "FIGURE": THREE_D_RENDER,
    "手办": THREE_D_RENDER,
    "人体模型": THREE_D_RENDER,
}

_EXPLICIT_TRANSFORM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (ANIME, re.compile(r"(?:convert|transform|restyle|render)\s+(?:the\s+)?(?:final\s+)?(?:output\s+)?(?:to|as)\s+(?:manga|anime)|(?:最终输出|最终渲染域|风格).{0,8}(?:转换|改成|设为).{0,8}(?:漫画|二次元|ANIME)", re.IGNORECASE)),
    (PHOTOREALISTIC, re.compile(r"(?:convert|transform|restyle|render)\s+(?:the\s+)?(?:final\s+)?(?:output\s+)?(?:to|as)\s+(?:photorealistic|photographic|live[ -]?action)|(?:最终输出|最终渲染域|风格).{0,8}(?:转换|改成|设为).{0,8}(?:真人|写实摄影|PHOTOREALISTIC)", re.IGNORECASE)),
    (ILLUSTRATION, re.compile(r"(?:convert|transform|restyle|render)\s+(?:the\s+)?(?:final\s+)?(?:output\s+)?(?:to|as)\s+(?:an?\s+)?illustration|(?:最终输出|风格)(?:转换|改成|设为)插画", re.IGNORECASE)),
    (THREE_D_RENDER, re.compile(r"(?:convert|transform|restyle|render)\s+(?:the\s+)?(?:final\s+)?(?:output\s+)?(?:to|as)\s+(?:a\s+)?3d\s+render|(?:最终输出|风格)(?:转换|改成|设为)(?:3d|手办)", re.IGNORECASE)),
)


def _profile(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _walk_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_text(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_text(item)


def normalize_rendering_domain(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    direct = _DOMAIN_ALIASES.get(raw.upper().replace("-", "_").replace(" ", "_"))
    if direct:
        return direct
    text = raw.casefold()
    # Character-source photographic evidence outranks a later translation phrase.
    if re.search(r"photoreal|photographic|live[ -]?action|真人|写实摄影|照片", text):
        return PHOTOREALISTIC
    if re.search(r"(?:^|\W)3d(?:\W|$)|cgi|plastic|figure|anatomical model|手办|人体模型|塑料质感", text):
        return THREE_D_RENDER
    if re.search(r"anime|manga|cel[- ]?shad|二次元|漫画", text):
        return ANIME
    if re.search(r"illustrat|painterly|watercolou?r|插画|水彩", text):
        return ILLUSTRATION
    return None


def _domain_from_profile(value: Any) -> tuple[str, list[str]]:
    profile = _profile(value)
    for key in ("resolved_domain", "rendering_domain", "domain", "final_rendering_domain", "mode"):
        domain = normalize_rendering_domain(profile.get(key))
        if domain:
            return domain, [f"{key}={profile.get(key)}"]
    evidence = list(_walk_text(value))
    for item in evidence:
        domain = normalize_rendering_domain(item)
        if domain:
            return domain, [item]
    return UNKNOWN, []


def _first_known_domain(sources: list[tuple[str, Any]]) -> dict[str, Any]:
    for source, value in sources:
        domain, evidence = _domain_from_profile(value)
        if domain != UNKNOWN:
            return {"resolved_domain": domain, "source": source, "evidence": evidence}
    return {"resolved_domain": UNKNOWN, "source": "unresolved_reference_observation", "evidence": []}


def _explicit_domain_override(raw_input: dict[str, Any]) -> dict[str, Any] | None:
    requirements = _profile(raw_input.get("requirements"))
    generation = _profile(raw_input.get("generation"))
    supplied = (
        raw_input.get("final_rendering_domain_override")
        or requirements.get("final_rendering_domain_override")
        or generation.get("final_rendering_domain_override")
    )
    if supplied is not None:
        domain = normalize_rendering_domain(supplied)
        if domain is None:
            raise ValueError(f"Unsupported final rendering-domain override: {supplied}")
        return {"resolved_domain": domain, "source": "explicit_structured_user_override", "evidence": [str(supplied)]}

    for field in (raw_input.get("request"), raw_input.get("instruction"), raw_input.get("prompt")):
        for text in _walk_text(field):
            matches = [(domain, match.group(0)) for domain, pattern in _EXPLICIT_TRANSFORM_PATTERNS if (match := pattern.search(text))]
            selected = sorted({item[0] for item in matches})
            if len(selected) > 1:
                raise ValueError(f"Conflicting explicit final rendering-domain overrides: {', '.join(selected)}")
            if selected:
                return {"resolved_domain": selected[0], "source": "explicit_natural_language_style_transform", "evidence": [matches[0][1]]}
    return None


def resolve_pose_composition_interpretation(raw_input: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    requirements = _profile(raw_input.get("requirements"))
    explicit = raw_input.get("pose_composition_interpretation_mode") or requirements.get("pose_composition_interpretation_mode")
    if explicit:
        mode = str(explicit).strip().upper()
        if mode not in POSE_COMPOSITION_MODES:
            raise ValueError(f"Unsupported pose/composition interpretation mode: {explicit}")
        return {"mode": mode, "source": "explicit_user_input", "legacy_pose_intent": None}

    geometry = _profile(analysis.get("geometry_evidence"))
    pose_intent = _profile(geometry.get("pose_intent_profile"))
    legacy = str(
        pose_intent.get("mode")
        or pose_intent.get("pose_interpretation")
        or _profile(analysis.get("preflight")).get("pose_interpretation")
        or "PHYSICAL_ERGONOMIC"
    ).strip().upper()
    mode = STYLIZED_POSE_GRAMMAR if legacy == "STYLIZED_REFERENCE_DOMINANT" else REALISTIC_POSE_GRAMMAR
    return {"mode": mode, "source": "pose_reference_geometry_evidence" if pose_intent else "compatibility_default", "legacy_pose_intent": legacy}


def build_reference_role_isolation(raw_input: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    geometry = _profile(analysis.get("geometry_evidence"))
    character = _profile(analysis.get("character"))
    character_identity = _profile(character.get("identity"))
    character_domain = _first_known_domain([
        ("geometry_evidence.character_rendering_domain_profile", geometry.get("character_rendering_domain_profile")),
        ("character.character_rendering_domain_profile", character.get("character_rendering_domain_profile")),
        ("character.art_style", character.get("art_style")),
        ("character.identity.rendering_domain", character_identity.get("rendering_domain")),
        ("character.identity.art_style", character_identity.get("art_style")),
        ("raw_input.character_rendering_domain", raw_input.get("character_rendering_domain")),
        ("raw_input.requirements.character", _profile(raw_input.get("requirements")).get("character")),
    ])
    pose_domain = _first_known_domain([
        ("geometry_evidence.pose_reference_rendering_domain_profile", geometry.get("pose_reference_rendering_domain_profile")),
        ("analysis.pose_reference_rendering_domain_profile", analysis.get("pose_reference_rendering_domain_profile")),
        ("raw_input.pose_reference_rendering_domain", raw_input.get("pose_reference_rendering_domain")),
        ("raw_input.requirements.pose", _profile(raw_input.get("requirements")).get("pose")),
    ])
    override = _explicit_domain_override(raw_input)
    final_domain = override or {
        **character_domain,
        "source": "derived_from_character_reference",
        "evidence_source": character_domain["source"],
    }
    pose_mode = resolve_pose_composition_interpretation(raw_input, analysis)
    legacy_style = raw_input.get("render_style_mode") or raw_input.get("render_style")
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": CHARACTER_REFERENCE_DOMINANT,
        "character_reference": {
            "authority": "HARD_AND_SOLE",
            "authority_fields": list(CHARACTER_AUTHORITY_FIELDS),
            "rendering_domain": character_domain,
        },
        "pose_reference": {
            "authority": "STRUCTURE_AND_SPACE_ONLY",
            "allowed_fields": list(POSE_ALLOWED_FIELDS),
            "forbidden_fields": list(POSE_FORBIDDEN_FIELDS),
            "observed_rendering_domain": pose_domain,
            "rendering_domain_transfer": "FORBIDDEN",
        },
        "pose_composition_interpretation": pose_mode,
        "final_rendering_domain": {
            "policy": CHARACTER_REFERENCE_DOMINANT,
            "resolved_domain": final_domain["resolved_domain"],
            "source": final_domain["source"],
            "evidence": final_domain.get("evidence", []),
            "explicit_user_style_transform": override is not None,
            "pose_reference_may_override": False,
            "legacy_render_style_request": legacy_style,
            "legacy_render_style_is_domain_authority": False,
        },
        "reference_role_leakage": deepcopy(REFERENCE_ROLE_LEAKAGE),
        "validation_priority": list(VALIDATION_PRIORITY),
        "action_specific": False,
    }


def _domain_instruction(domain: str) -> str:
    if domain == PHOTOREALISTIC:
        return (
            "Render the character as photographic/photorealistic: preserve real human facial morphology, natural eye scale, "
            "skin response, hair response, garment material response, and camera-coherent surface detail. Forbid anime face, "
            "oversized anime eyes, illustration or cel shading, manga facial morphology, and plastic/figure rendering."
        )
    if domain == ANIME:
        return "Keep the character in the anime/manga domain established by the CHARACTER_REFERENCE; a photographic pose donor changes pose and composition only."
    if domain == ILLUSTRATION:
        return "Keep the illustration domain and material language established by the CHARACTER_REFERENCE; the pose donor contributes no shading or scene style."
    if domain == THREE_D_RENDER:
        return "Keep the 3D-render domain and material language established by the CHARACTER_REFERENCE; the pose donor contributes no surface material or renderer style."
    return "Derive the final rendering domain visually from the CHARACTER_REFERENCE; unresolved evidence must not be filled from the POSE_REFERENCE."


def strip_legacy_runtime_mode_block(prompt_text: str) -> str:
    lines = prompt_text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip().startswith("Runtime Mode Interface (v1.7.6):")), None)
    if start is None:
        return prompt_text
    # The v1.7 injector does not place a blank line between its inventory and
    # the retained first heading, so stop at the semantic heading, not whitespace.
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip() == "## Character Identity Lock"),
        None,
    )
    if end is None:
        end = start + 1
        while end < len(lines) and lines[end].strip():
            end += 1
    while end < len(lines) and not lines[end].strip():
        end += 1
    return "\n".join(lines[:start] + lines[end:]).rstrip() + "\n"


def apply_reference_role_isolation_to_prompt(prompt_text: str, isolation: dict[str, Any]) -> str:
    base = strip_legacy_runtime_mode_block(prompt_text)
    domain = str(_profile(isolation.get("final_rendering_domain")).get("resolved_domain", UNKNOWN))
    pose_mode = str(_profile(isolation.get("pose_composition_interpretation")).get("mode", REALISTIC_POSE_GRAMMAR))
    block = [
        "Reference Role Isolation / Final Rendering Domain (v1.8.0):",
        f"- Final rendering-domain policy: {CHARACTER_REFERENCE_DOMINANT}",
        f"- Final rendering domain: {domain}",
        f"- Pose/composition interpretation: {pose_mode}",
        "- The CHARACTER_REFERENCE is the sole HARD authority for character identity, face morphology, rendering domain, realism level, skin and hair rendering, canonical body build, garment identity/structure/material language, and accessories.",
        "- The POSE_REFERENCE contributes pose topology, joint arrangement, limb direction, silhouette, crop, framing, occlusion, perspective, foreshortening, camera angle, depth ordering, torso/pelvis tilt, action energy, and visible-range relationships only.",
        "- POSE_REFERENCE may provide manga pose grammar, but must not provide manga rendering domain.",
        "- Do not inherit the POSE_REFERENCE face style, skin style, body mass, sex-linked proportions, garments, material style, palette, lighting, scene semantics, text/captions, or background style.",
        f"- Final-domain instruction: {_domain_instruction(domain)}",
        "- STYLIZED_REFERENCE_DOMINANT is a legacy compatibility label for spatial/action preservation only; it never authorizes pose-reference visual-style transfer.",
        "",
    ]
    marker = "# Final Generation Prompt\n\n"
    if base.startswith(marker):
        return marker + "\n".join(block) + base[len(marker):]
    return "\n".join(block) + base


def lint_reference_role_prompt(prompt_text: str, isolation: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    final_domain = _profile(isolation.get("final_rendering_domain"))
    pose_domain = _profile(_profile(isolation.get("pose_reference")).get("observed_rendering_domain"))
    domain = final_domain.get("resolved_domain")
    if final_domain.get("explicit_user_style_transform") is not True and domain != _profile(isolation.get("character_reference")).get("rendering_domain", {}).get("resolved_domain"):
        violations.append({"code": "FINAL_DOMAIN_NOT_CHARACTER_DERIVED", "stage": "reference_role_isolation"})
    if final_domain.get("explicit_user_style_transform") is not True and domain != UNKNOWN and domain == pose_domain.get("resolved_domain") and domain != _profile(isolation.get("character_reference")).get("rendering_domain", {}).get("resolved_domain"):
        violations.append({"code": "POSE_REFERENCE_RENDERING_DOMAIN_LEAK", "stage": "reference_role_isolation"})
    for forbidden in ("Render style: 漫画／二次元 (MANGA_ANIME)", "Render in manga/anime language"):
        if forbidden in prompt_text and final_domain.get("explicit_user_style_transform") is not True:
            violations.append({"code": "LEGACY_MANGA_RENDER_DIRECTIVE", "stage": "prompt", "value": forbidden})
    required = (
        "Final rendering-domain policy: CHARACTER_REFERENCE_DOMINANT",
        "POSE_REFERENCE may provide manga pose grammar, but must not provide manga rendering domain.",
        "The CHARACTER_REFERENCE is the sole HARD authority",
    )
    for text in required:
        if text not in prompt_text:
            violations.append({"code": "REFERENCE_ROLE_PROMPT_LOCK_MISSING", "stage": "prompt", "value": text})
    return violations


def _contains_positive_pose_style_directive(text: str) -> bool:
    lowered = text.casefold()
    return bool(
        re.search(r"(?:render|rendering|rendered|presentation).{0,24}(?:manga|anime|3d|figure|illustrat)|(?:manga|anime|3d|figure|illustrat).{0,24}(?:render|rendering|presentation)", lowered)
        or re.search(r"(?:漫画|二次元|手办|插画).{0,12}(?:渲染|画风|质感)", lowered)
    )


def sanitize_sources_for_reference_roles(
    raw_input: dict[str, Any],
    analysis: dict[str, Any],
    isolation: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Remove legacy pose-style authority before the retained v1.7 compiler runs."""

    clean_input = deepcopy(raw_input)
    clean_analysis = deepcopy(analysis)
    overrides: list[dict[str, Any]] = []
    explicit_transform = _profile(isolation.get("final_rendering_domain")).get("explicit_user_style_transform") is True
    if not explicit_transform:
        for field in ("render_style_mode", "render_style"):
            if clean_input.get(field) not in (None, "REFERENCE_INHERIT"):
                overrides.append({"field": field, "from": clean_input.get(field), "to": "REFERENCE_INHERIT", "reason": "legacy_style_mode_is_not_final_domain_authority"})
            if field in clean_input:
                clean_input[field] = "REFERENCE_INHERIT"
        requirements = _profile(clean_input.get("requirements"))
        background = requirements.get("background")
        if isinstance(background, str) and _contains_positive_pose_style_directive(background):
            requirements["background"] = (
                "Use a neutral background compatible with the CHARACTER_REFERENCE rendering domain. Preserve action energy only; "
                "do not inherit POSE_REFERENCE palette, lighting, scene semantics, text/captions, or background style."
            )
            overrides.append({"field": "requirements.background", "reason": "pose_reference_scene_style_authority_removed"})
        constraints = clean_input.get("constraints")
        if isinstance(constraints, list):
            kept = [
                item for item in constraints
                if not (
                    isinstance(item, str)
                    and ("manga_anime_presentation" in item.casefold() or _contains_positive_pose_style_directive(item))
                )
            ]
            if len(kept) != len(constraints):
                overrides.append({"field": "constraints", "reason": "pose_rendering_domain_directive_removed"})
            clean_input["constraints"] = kept

        character = _profile(clean_analysis.get("character"))
        identity = _profile(character.get("identity"))
        domain = _profile(isolation.get("final_rendering_domain")).get("resolved_domain", UNKNOWN)
        if identity:
            old_style = identity.get("art_style")
            identity["art_style"] = _domain_instruction(str(domain))
            if old_style != identity["art_style"]:
                overrides.append({"field": "analysis.character.identity.art_style", "reason": "character_domain_reasserted"})
        preserve = character.get("preserve")
        if isinstance(preserve, list):
            character["preserve"] = [
                item for item in preserve
                if not (isinstance(item, str) and re.search(r"manga|anime|3d|figure|illustrat|漫画|二次元|手办|插画", item, re.IGNORECASE))
            ]

    geometry = _profile(clean_analysis.setdefault("geometry_evidence", {}))
    geometry["character_rendering_domain_profile"] = deepcopy(_profile(isolation.get("character_reference")).get("rendering_domain", {}))
    geometry["pose_reference_rendering_domain_profile"] = deepcopy(_profile(isolation.get("pose_reference")).get("observed_rendering_domain", {}))
    geometry["reference_role_isolation"] = {
        "policy": CHARACTER_REFERENCE_DOMINANT,
        "pose_rendering_domain_transfer": "FORBIDDEN",
    }
    clean_input["version"] = SCHEMA_VERSION
    return clean_input, clean_analysis, overrides


def evaluate_character_rendering_domain(
    isolation: dict[str, Any],
    candidate_observation: dict[str, Any],
    *,
    strict_reference_replica: bool = True,
) -> dict[str, Any]:
    expected = str(_profile(isolation.get("final_rendering_domain")).get("resolved_domain", UNKNOWN))
    observed_profile = _profile(candidate_observation.get("character_rendering_domain_observation"))
    if not observed_profile:
        observed_profile = _profile(_profile(candidate_observation.get("geometry_evidence")).get("character_rendering_domain_profile"))
    observed = normalize_rendering_domain(
        observed_profile.get("observed_domain")
        or observed_profile.get("resolved_domain")
        or observed_profile.get("rendering_domain")
        or observed_profile.get("domain")
    ) or UNKNOWN
    evidence_flags = sorted(name for name in DOMAIN_FAILURE_FLAGS if observed_profile.get(name) is True)
    mismatch = expected != UNKNOWN and observed != UNKNOWN and expected != observed
    photographic_style_failure = expected == PHOTOREALISTIC and bool(evidence_flags)
    if mismatch or photographic_style_failure:
        status = "HARD_FAIL"
    elif expected == UNKNOWN or observed == UNKNOWN:
        status = "NOT_ASSESSABLE"
    else:
        status = "PASS"
    continue_pipeline = status == "PASS"
    return {
        "schema_version": SCHEMA_VERSION,
        "gate": "CHARACTER_RENDERING_DOMAIN_CONSISTENCY",
        "enforcement": "HARD" if strict_reference_replica else "MODE_SCOPED_HARD",
        "status": status,
        "expected_domain": expected,
        "observed_domain": observed,
        "explicit_failure_evidence": evidence_flags,
        "failure_cause": "pose_reference_rendering_domain_leakage" if status == "HARD_FAIL" else None,
        "continue_to_pose_validation": continue_pipeline,
        "continue_to_candidate_ranking": continue_pipeline,
        "continue_to_repair": continue_pipeline,
        "continue_to_finalization": continue_pipeline,
        "validation_priority": list(VALIDATION_PRIORITY),
        "not_assessable_is_pass": False,
        "action_specific": False,
    }


def render_isolation_json(isolation: dict[str, Any]) -> str:
    return json.dumps(isolation, ensure_ascii=False, indent=2) + "\n"
