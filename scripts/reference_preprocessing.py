#!/usr/bin/env python3
"""Deterministic v1.8.1 reference preprocessing before renderer handoff.

This module is deliberately conservative.  It can consume an existing alpha
channel or an explicit mask, and otherwise estimates border-connected
background with Pillow-only operations.  A low-confidence estimate still
writes inspectable artifacts but is never renderer-authorized.
"""

from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageFilter, ImageOps


PREPROCESSING_SCHEMA_VERSION = "1.8.1"
PASS = "PASS"
NOT_ASSESSABLE = "NOT_ASSESSABLE"

CHARACTER_AUTHORITY = {
    "identity": "HARD",
    "rendering_domain": "HARD",
    "face_style": "HARD",
    "garment_identity": "HARD",
    "body_proportion": "HARD",
    "background": "NONE",
    "scene": "NONE",
    "lighting": "SOFT_OR_NONE",
}

POSE_AUTHORITY = {
    "pose_topology": "HIGH",
    "crop": "HIGH",
    "framing": "HIGH",
    "occlusion": "HIGH",
    "perspective": "HIGH",
    "silhouette": "HIGH",
    "action_energy": "HIGH",
    "identity": "NONE",
    "rendering_domain": "NONE",
    "garment_identity": "NONE",
    "scene": "NONE",
    "text": "NONE",
}


class ReferencePreprocessingError(ValueError):
    """Raised when preprocessing inputs or package boundaries are invalid."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReferencePreprocessingError(f"Expected an object in {path}")
    return value


def _source_record(path: Path, image: Image.Image) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "width": image.width,
        "height": image.height,
        "mode": image.mode,
    }


def _resolve_optional_path(value: Any, base: Path) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ReferencePreprocessingError("Mask paths must be strings.")
    path = Path(value)
    if not path.is_absolute():
        path = (base / path).resolve()
    if not path.is_file():
        raise ReferencePreprocessingError(f"Missing preprocessing mask: {path}")
    return path


def _normalize_mask_paths(options: dict[str, Any], count: int, base: Path) -> tuple[list[Path | None], Path | None]:
    raw_character_masks = options.get("character_masks", [])
    if isinstance(raw_character_masks, str):
        raw_character_masks = [raw_character_masks]
    if not isinstance(raw_character_masks, list):
        raise ReferencePreprocessingError("preprocessing.character_masks must be a list or string.")
    if len(raw_character_masks) > count:
        raise ReferencePreprocessingError("More character masks were supplied than character references.")
    character_masks = [
        _resolve_optional_path(raw_character_masks[index], base) if index < len(raw_character_masks) else None
        for index in range(count)
    ]
    pose_mask = _resolve_optional_path(options.get("pose_subject_mask"), base)
    return character_masks, pose_mask


def _mask_metrics(mask: Image.Image) -> dict[str, float]:
    binary = mask.point(lambda value: 255 if value >= 128 else 0)
    histogram = binary.histogram()
    total = max(1, binary.width * binary.height)
    subject = histogram[255]
    perimeter = max(1, 2 * binary.width + 2 * binary.height - 4)
    pixels = binary.load()
    touches = 0
    for x in range(binary.width):
        touches += 1 if pixels[x, 0] else 0
        if binary.height > 1:
            touches += 1 if pixels[x, binary.height - 1] else 0
    for y in range(1, max(1, binary.height - 1)):
        touches += 1 if pixels[0, y] else 0
        if binary.width > 1:
            touches += 1 if pixels[binary.width - 1, y] else 0
    return {
        "subject_fraction": round(subject / total, 6),
        "background_fraction": round(1.0 - subject / total, 6),
        "subject_border_touch_fraction": round(touches / perimeter, 6),
    }


def _mask_status(metrics: dict[str, float], *, existing_alpha: bool = False) -> tuple[str, float, list[str]]:
    subject = metrics["subject_fraction"]
    background = metrics["background_fraction"]
    border_touch = metrics["subject_border_touch_fraction"]
    reasons: list[str] = []
    score = 1.0
    if subject < 0.02:
        reasons.append("subject_area_too_small")
        score -= 0.65
    if subject > 0.94:
        reasons.append("background_removal_not_observable")
        score -= 0.65
    if background < 0.06:
        reasons.append("insufficient_removed_background")
        score -= 0.35
    if border_touch > 0.72 and not existing_alpha:
        reasons.append("foreground_dominates_canvas_boundary")
        score -= 0.3
    score = round(max(0.0, min(1.0, score)), 3)
    status = PASS if score >= 0.6 and not {"subject_area_too_small", "background_removal_not_observable"}.intersection(reasons) else NOT_ASSESSABLE
    return status, score, reasons


def _component_mask(foreground: list[bool], width: int, height: int) -> tuple[list[bool], dict[str, Any]]:
    seen = bytearray(width * height)
    components: list[tuple[list[int], tuple[int, int, int, int]]] = []
    for start, is_foreground in enumerate(foreground):
        if not is_foreground or seen[start]:
            continue
        seen[start] = 1
        queue: deque[int] = deque([start])
        members: list[int] = []
        min_x = max_x = start % width
        min_y = max_y = start // width
        while queue:
            current = queue.popleft()
            members.append(current)
            x = current % width
            y = current // width
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for neighbor in (current - 1, current + 1, current - width, current + width):
                if neighbor < 0 or neighbor >= width * height or seen[neighbor] or not foreground[neighbor]:
                    continue
                nx, ny = neighbor % width, neighbor // width
                if abs(nx - x) + abs(ny - y) != 1:
                    continue
                seen[neighbor] = 1
                queue.append(neighbor)
        components.append((members, (min_x, min_y, max_x, max_y)))
    if not components:
        return foreground, {
            "component_count": 0,
            "kept_component_count": 0,
            "largest_component_fraction": 0.0,
            "primary_component_selection": "unavailable",
            "primary_selection_margin": 0.0,
        }
    components.sort(key=lambda item: len(item[0]), reverse=True)
    largest_members, _largest_box = components[0]
    canvas_center_x = (width - 1) / 2.0
    canvas_center_y = (height - 1) / 2.0

    def primary_score(item: tuple[list[int], tuple[int, int, int, int]]) -> float:
        members, box = item
        box_center_x = (box[0] + box[2]) / 2.0
        box_center_y = (box[1] + box[3]) / 2.0
        distance = (
            ((box_center_x - canvas_center_x) / max(1.0, width)) ** 2
            + ((box_center_y - canvas_center_y) / max(1.0, height)) ** 2
        ) ** 0.5
        contains_center = box[0] <= canvas_center_x <= box[2] and box[1] <= canvas_center_y <= box[3]
        area_fraction = len(members) / max(1, width * height)
        return (3.0 if contains_center else 0.0) + area_fraction * 2.0 - distance

    ranked = sorted(((primary_score(item), item) for item in components), key=lambda item: item[0], reverse=True)
    primary_score_value, (primary_members, primary_box) = ranked[0]
    selection_margin = primary_score_value - ranked[1][0] if len(ranked) > 1 else primary_score_value
    margin_x = max(2, int(width * 0.08))
    margin_y = max(2, int(height * 0.08))
    expanded = (
        max(0, primary_box[0] - margin_x),
        max(0, primary_box[1] - margin_y),
        min(width - 1, primary_box[2] + margin_x),
        min(height - 1, primary_box[3] + margin_y),
    )
    minimum_secondary = max(4, int(len(primary_members) * 0.0025))
    kept: list[tuple[list[int], tuple[int, int, int, int]]] = [(primary_members, primary_box)]
    for members, box in components:
        if members is primary_members:
            continue
        intersects = not (box[2] < expanded[0] or box[0] > expanded[2] or box[3] < expanded[1] or box[1] > expanded[3])
        if len(members) >= minimum_secondary and intersects:
            kept.append((members, box))
    result = [False] * (width * height)
    kept_total = 0
    for members, _box in kept:
        kept_total += len(members)
        for index in members:
            result[index] = True
    all_foreground = sum(len(item[0]) for item in components)
    return result, {
        "component_count": len(components),
        "kept_component_count": len(kept),
        "largest_component_fraction": round(len(largest_members) / max(1, all_foreground), 6),
        "primary_component_fraction": round(len(primary_members) / max(1, all_foreground), 6),
        "primary_component_selection": "center_weighted_connected_component",
        "primary_component_contains_canvas_center": (
            primary_box[0] <= canvas_center_x <= primary_box[2]
            and primary_box[1] <= canvas_center_y <= primary_box[3]
        ),
        "primary_selection_margin": round(selection_margin, 6),
        "discarded_foreground_fraction": round((all_foreground - kept_total) / max(1, width * height), 6),
    }


def _border_connected_subject_mask(image: Image.Image, max_dimension: int = 640) -> tuple[Image.Image, dict[str, Any]]:
    rgb = image.convert("RGB")
    working = rgb.copy()
    working.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    width, height = working.size
    pixels = list(working.getdata())
    # Use a narrow seed band rather than only the outermost pixels.  This lets
    # a thin manga/comic panel frame be treated as attachment information while
    # still requiring background-color connectivity from the canvas boundary.
    seed_band = max(2, min(12, int(min(width, height) * 0.035)))
    border_indices = [
        y * width + x
        for y in range(height)
        for x in range(width)
        if x < seed_band or x >= width - seed_band or y < seed_band or y >= height - seed_band
    ]
    border_pixels = [pixels[index] for index in border_indices]
    strip = Image.new("RGB", (max(1, len(border_pixels)), 1))
    strip.putdata(border_pixels or [(255, 255, 255)])
    quantized = strip.quantize(colors=min(8, max(1, len(set(border_pixels)))), method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette() or []
    color_counts = sorted(quantized.getcolors() or [], reverse=True)
    minimum_count = max(2, int(len(border_pixels) * 0.025))
    prototypes: list[tuple[int, int, int]] = []
    covered = 0
    for count, palette_index in color_counts:
        if count < minimum_count and prototypes:
            continue
        offset = palette_index * 3
        prototypes.append(tuple(palette[offset:offset + 3]))
        covered += count
        if len(prototypes) >= 6 or covered >= len(border_pixels) * 0.94:
            break
    if not prototypes:
        prototypes = [border_pixels[0] if border_pixels else (255, 255, 255)]
    distance_limit = 46 * 46

    def matches_background(pixel: tuple[int, int, int]) -> bool:
        return any(sum((int(pixel[channel]) - int(proto[channel])) ** 2 for channel in range(3)) <= distance_limit for proto in prototypes)

    background = bytearray(width * height)
    queue: deque[int] = deque()
    for index in border_indices:
        if not background[index] and matches_background(pixels[index]):
            background[index] = 1
            queue.append(index)
    while queue:
        current = queue.popleft()
        x, y = current % width, current // width
        for neighbor in (current - 1, current + 1, current - width, current + width):
            if neighbor < 0 or neighbor >= width * height or background[neighbor]:
                continue
            nx, ny = neighbor % width, neighbor // width
            if abs(nx - x) + abs(ny - y) != 1:
                continue
            if matches_background(pixels[neighbor]):
                background[neighbor] = 1
                queue.append(neighbor)
    foreground = [not value for value in background]
    foreground, component_metrics = _component_mask(foreground, width, height)
    mask_small = Image.new("L", (width, height))
    mask_small.putdata([255 if value else 0 for value in foreground])
    mask = mask_small.resize(rgb.size, Image.Resampling.NEAREST)
    return mask, {
        "method": "border_connected_color_quantization",
        "working_width": width,
        "working_height": height,
        "seed_band_pixels": seed_band,
        "prototype_count": len(prototypes),
        "distance_threshold_rgb": 46,
        **component_metrics,
    }


def _derive_subject_mask(source: Image.Image, explicit_mask: Path | None) -> tuple[Image.Image, dict[str, Any]]:
    if explicit_mask is not None:
        supplied = Image.open(explicit_mask).convert("L")
        if supplied.size != source.size:
            supplied = supplied.resize(source.size, Image.Resampling.NEAREST)
        mask = supplied.point(lambda value: 255 if value >= 128 else 0)
        metrics = _mask_metrics(mask)
        status, score, reasons = _mask_status(metrics)
        return mask, {
            "method": "explicit_subject_mask",
            "explicit_mask_path": str(explicit_mask.resolve()),
            "explicit_mask_sha256": _sha256(explicit_mask),
            "status": status,
            "confidence": score,
            "reasons": reasons,
            **metrics,
        }
    rgba = source.convert("RGBA")
    alpha = rgba.getchannel("A")
    alpha_metrics = _mask_metrics(alpha)
    if alpha.getextrema()[0] < 250 and alpha_metrics["background_fraction"] >= 0.01:
        mask = alpha.point(lambda value: 255 if value >= 16 else 0)
        metrics = _mask_metrics(mask)
        status, score, reasons = _mask_status(metrics, existing_alpha=True)
        return mask, {
            "method": "source_alpha_channel",
            "status": status,
            "confidence": score,
            "reasons": reasons,
            **metrics,
        }
    mask, details = _border_connected_subject_mask(source)
    metrics = _mask_metrics(mask)
    status, score, reasons = _mask_status(metrics)
    if details.get("primary_selection_margin", 0.0) < 0.2:
        status = NOT_ASSESSABLE
        score = min(score, 0.5)
        reasons.append("ambiguous_primary_component_selection")
    if details.get("primary_component_fraction", 0.0) < 0.65 or details.get("kept_component_count", 0) > 8:
        status = NOT_ASSESSABLE
        score = min(score, 0.5)
        reasons.append("fragmented_subject_estimate")
    return mask, {
        **details,
        "status": status,
        "confidence": score,
        "reasons": reasons,
        **metrics,
    }


def character_cutout(
    source_path: Path,
    output_dir: Path,
    *,
    explicit_mask: Path | None = None,
    artifact_prefix: str = "character_cutout",
) -> dict[str, Any]:
    source_path = source_path.resolve()
    source = ImageOps.exif_transpose(Image.open(source_path)).convert("RGBA")
    mask, segmentation = _derive_subject_mask(source, explicit_mask)
    source_alpha = source.getchannel("A")
    final_alpha = ImageChops.multiply(source_alpha, mask)
    cutout = source.copy()
    cutout.putalpha(final_alpha)
    output_dir.mkdir(parents=True, exist_ok=True)
    cutout_path = output_dir / f"{artifact_prefix}.png"
    mask_path = output_dir / f"{artifact_prefix}_mask.png"
    meta_path = output_dir / f"{artifact_prefix}_meta.json"
    cutout.save(cutout_path, format="PNG", optimize=True)
    mask.save(mask_path, format="PNG", optimize=True)
    status = segmentation["status"]
    meta = {
        "schema_version": PREPROCESSING_SCHEMA_VERSION,
        "module": "CHARACTER_CUTOUT",
        "status": status,
        "renderer_eligible": status == PASS,
        "source": _source_record(source_path, source),
        "outputs": {
            "character_cutout": cutout_path.name,
            "character_cutout_mask": mask_path.name,
            "character_cutout_sha256": _sha256(cutout_path),
            "character_cutout_mask_sha256": _sha256(mask_path),
            "background": "transparent",
            "canvas_preserved": True,
        },
        "segmentation": segmentation,
        "authority": deepcopy(CHARACTER_AUTHORITY),
        "removed_authority": {
            "background": "NONE",
            "scene_semantics": "NONE",
            "unrelated_people_text_ui_watermark_noise": "NONE",
        },
        "policy": {
            "purpose": "isolate_character_authority_before_renderer_handoff",
            "raw_character_scene_is_renderer_authority": False,
            "generative_inpainting_used": False,
            "safety_bypass_purpose": False,
        },
        "limitations": [
            "Automatic border-connected segmentation is conservative and may require an explicit mask for complex backgrounds.",
            "A PASS describes preprocessing observability, not final renderer compliance.",
        ],
    }
    _write_json(meta_path, meta)
    return {"meta": meta, "cutout_path": cutout_path, "mask_path": mask_path, "meta_path": meta_path}


def pose_reference_sanitization(
    source_path: Path,
    output_dir: Path,
    *,
    explicit_mask: Path | None = None,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    source = ImageOps.exif_transpose(Image.open(source_path)).convert("RGBA")
    mask, segmentation = _derive_subject_mask(source, explicit_mask)
    rgb = source.convert("RGB")
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(rgb))
    neutral_tone = grayscale.point(lambda value: 164 + int(value * 0.18))
    neutral_subject = Image.merge("RGB", (neutral_tone, neutral_tone, neutral_tone))
    sanitized = Image.new("RGB", source.size, (247, 247, 247))
    sanitized.paste(neutral_subject, (0, 0), mask)
    internal_edges = ImageOps.autocontrast(grayscale.filter(ImageFilter.FIND_EDGES))
    internal_edges = internal_edges.point(lambda value: 255 if value >= 64 else 0)
    internal_edges = ImageChops.multiply(internal_edges, mask)
    sanitized.paste((92, 92, 92), (0, 0, source.width, source.height), internal_edges)
    outer = mask.filter(ImageFilter.MaxFilter(5))
    inner = mask.filter(ImageFilter.MinFilter(5))
    silhouette_outline = ImageChops.subtract(outer, inner)
    sanitized.paste((48, 48, 48), (0, 0, source.width, source.height), silhouette_outline)

    output_dir.mkdir(parents=True, exist_ok=True)
    sanitized_path = output_dir / "pose_sanitized.png"
    mask_path = output_dir / "pose_subject_mask.png"
    meta_path = output_dir / "pose_sanitized_meta.json"
    crop_path = output_dir / "pose_crop_overlay.json"
    sanitized.save(sanitized_path, format="PNG", optimize=True)
    mask.save(mask_path, format="PNG", optimize=True)
    status = segmentation["status"]
    crop_overlay = {
        "schema_version": PREPROCESSING_SCHEMA_VERSION,
        "source_canvas": {"width": source.width, "height": source.height},
        "sanitized_canvas": {"width": sanitized.width, "height": sanitized.height},
        "crop_rectangle": {"x": 0, "y": 0, "width": source.width, "height": source.height},
        "crop_changed": False,
        "rotation_changed": False,
        "perspective_corrected": False,
        "canvas_expanded": False,
        "off_canvas_anatomy_completed": False,
        "subject_mask_registration": "same_pixel_canvas",
        "invariant_status": PASS if source.size == sanitized.size else "FAIL",
    }
    _write_json(crop_path, crop_overlay)
    meta = {
        "schema_version": PREPROCESSING_SCHEMA_VERSION,
        "module": "POSE_REFERENCE_SANITIZATION",
        "status": status,
        "renderer_eligible": status == PASS,
        "source": _source_record(source_path, source),
        "outputs": {
            "pose_sanitized": sanitized_path.name,
            "pose_subject_mask": mask_path.name,
            "pose_crop_overlay": crop_path.name,
            "pose_sanitized_sha256": _sha256(sanitized_path),
            "pose_subject_mask_sha256": _sha256(mask_path),
            "canvas_preserved": source.size == sanitized.size,
        },
        "segmentation": segmentation,
        "authority": deepcopy(POSE_AUTHORITY),
        "sanitization": {
            "background_outside_subject": "NEUTRALIZED",
            "scene_semantics_outside_subject": "NEUTRALIZED",
            "text_titles_labels_panel_borders_outside_subject": "REMOVED_WITH_BACKGROUND",
            "text_overlapping_subject": NOT_ASSESSABLE,
            "pose_identity_and_garment_style": "WEAKENED_BY_NEUTRAL_GRAYSCALE_AND_EDGE_REPRESENTATION",
            "visual_style_authority": "NONE",
            "generative_inpainting_used": False,
        },
        "policy": {
            "purpose": "preserve_pose_crop_silhouette_occlusion_and_perspective_without_scene_or_style_authority",
            "pose_only_reference_complete": False,
            "generic_visual_conditioning_only": True,
            "hard_pose_control_claimed": False,
            "safety_bypass_purpose": False,
        },
        "limitations": [
            "This is not OpenPose, depth, ControlNet, or a complete POSE_ONLY_REFERENCE.",
            "Text or style that overlaps the protected subject may remain rather than risking geometry damage.",
            "A PASS describes preprocessing observability, not pixel-level renderer benefit.",
        ],
    }
    _write_json(meta_path, meta)
    return {
        "meta": meta,
        "sanitized_path": sanitized_path,
        "mask_path": mask_path,
        "meta_path": meta_path,
        "crop_path": crop_path,
    }


def _portable_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _rewrite_render_package(
    run_dir: Path,
    character_results: list[dict[str, Any]],
    pose_result: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    package = run_dir / "render_package"
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        raise ReferencePreprocessingError("render_package/manifest.json is required before preprocessing handoff.")
    manifest = _read_object(manifest_path)
    old_references = manifest.get("references")
    if not isinstance(old_references, list) or not old_references:
        raise ReferencePreprocessingError("Render package has no raw reference manifest to sanitize.")
    references_dir = package / "references"
    originals_dir = package / "source_references"
    originals_dir.mkdir(parents=True, exist_ok=True)
    source_records: list[dict[str, Any]] = []
    for index, record in enumerate(old_references, start=1):
        if not isinstance(record, dict):
            raise ReferencePreprocessingError("Invalid raw reference record.")
        source_relative = Path(str(record.get("file", "")))
        source_file = (package / source_relative).resolve()
        if not source_file.is_file() or package.resolve() not in source_file.parents:
            raise ReferencePreprocessingError(f"Raw package reference is missing or unsafe: {source_relative}")
        role = str(record.get("role", "unspecified")).casefold()
        suffix = source_file.suffix.lower() or ".bin"
        label = f"character_original_{index:02d}{suffix}" if role == "character" else f"pose_reference_original{suffix}"
        destination = originals_dir / label
        if destination.exists():
            destination.unlink()
        shutil.move(str(source_file), str(destination))
        source_records.append({
            "role": role,
            "file": _portable_path(destination, package),
            "sha256": _sha256(destination),
            "transmitted_to_renderer": False,
        })
    references_dir.mkdir(parents=True, exist_ok=True)
    transmitted: list[dict[str, Any]] = []
    for index, result in enumerate(character_results, start=1):
        destination = references_dir / f"character_{index:02d}_cutout.png"
        shutil.copy2(result["cutout_path"], destination)
        transmitted.append({
            "role": "character",
            "file": _portable_path(destination, package),
            "representation": "CHARACTER_CUTOUT",
            "preprocessing_meta": result["meta_path"].name,
            "preprocessing_sha256": _sha256(result["meta_path"]),
            "authority": deepcopy(CHARACTER_AUTHORITY),
            "source_reference_transmitted": False,
        })
    pose_destination = references_dir / "pose_sanitized.png"
    shutil.copy2(pose_result["sanitized_path"], pose_destination)
    transmitted.append({
        "role": "pose",
        "file": _portable_path(pose_destination, package),
        "representation": "POSE_SANITIZED",
        "preprocessing_meta": pose_result["meta_path"].name,
        "preprocessing_sha256": _sha256(pose_result["meta_path"]),
        "authority": deepcopy(POSE_AUTHORITY),
        "source_reference_transmitted": False,
    })
    manifest["schema_version"] = PREPROCESSING_SCHEMA_VERSION
    manifest["skill_version"] = PREPROCESSING_SCHEMA_VERSION
    manifest["references"] = transmitted
    manifest["source_references"] = source_records
    manifest["reference_preprocessing"] = {
        "schema_version": PREPROCESSING_SCHEMA_VERSION,
        "status": plan["status"],
        "artifact": "reference_preprocessing.json",
        "raw_full_image_references_transmitted": False,
        "renderer_reference_order": plan["renderer_reference_order"],
        "pixel_level_benefit": NOT_ASSESSABLE,
    }
    _write_json(manifest_path, manifest)

    portable_input_path = package / "input.normalized.json"
    if portable_input_path.is_file():
        portable = _read_object(portable_input_path)
        portable["character_images"] = [item["file"] for item in transmitted if item["role"] == "character"]
        portable["pose_reference"] = next(item["file"] for item in transmitted if item["role"] == "pose")
        portable["reference"] = {"type": "pose", "path": portable["pose_reference"]}
        portable["preprocessing"] = {
            "status": plan["status"],
            "raw_full_image_references_transmitted": False,
        }
        _write_json(portable_input_path, portable)
    readme_path = package / "README.md"
    existing = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else "# Render Package\n"
    marker = "## v1.8.1 sanitized renderer inputs"
    if marker not in existing:
        existing += "\n" + "\n".join([
            marker,
            "",
            "Only files listed by `manifest.json.references` may be transmitted to the renderer.",
            "Image A is the character cutout authority. Do not inherit its removed background or scene.",
            "Image B is the sanitized pose reference. Use it only for pose, crop, silhouette, occlusion, framing, and perspective.",
            "Do not inherit Image B style, identity, garments, text, panel labels, or scene semantics.",
            "Raw source references are retained under `source_references/` for audit only and must not be transmitted.",
            "",
        ])
        readme_path.write_text(existing, encoding="utf-8")


def preprocessing_prompt_block(character_count: int) -> str:
    if character_count == 1:
        character_label = "Image A (submitted reference 1) is the character cutout"
        pose_label = "Image B (submitted reference 2) is the sanitized pose reference"
    else:
        character_label = f"Images A1-A{character_count} (submitted references 1-{character_count}) are character cutouts"
        pose_label = f"Image B (submitted reference {character_count + 1}) is the sanitized pose reference"
    return "\n".join([
        "Reference Preprocessing / Sanitized Input Roles (v1.8.1):",
        f"- {character_label} and the only authority for identity, face, body proportions, garment identity, accessories, and final rendering domain.",
        f"- {pose_label} and is only for pose topology, crop, framing, silhouette, occlusion, perspective, foreshortening, and action energy.",
        "- Do not inherit Image A background, furniture, room, lighting environment, or scene semantics.",
        "- Do not inherit Image B visual style, character identity, garment identity, text, labels, panel borders, background, or scene semantics.",
        "- Raw full-image references are audit-only and are not renderer inputs.",
        "",
    ])


def apply_preprocessing_prompt(prompt: str, character_count: int) -> str:
    marker = "Reference Preprocessing / Sanitized Input Roles (v1.8.1):"
    if marker in prompt:
        return prompt
    anchor = "## Character Identity Lock"
    block = preprocessing_prompt_block(character_count)
    if anchor in prompt:
        return prompt.replace(anchor, block + "\n" + anchor, 1)
    return block + "\n" + prompt


def lint_preprocessing_prompt(prompt: str) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    required = {
        "CHARACTER_CUTOUT_ROLE_MISSING": "only authority for identity, face, body proportions, garment identity, accessories, and final rendering domain",
        "POSE_SANITIZED_ROLE_MISSING": "only for pose topology, crop, framing, silhouette, occlusion, perspective, foreshortening, and action energy",
        "CHARACTER_BACKGROUND_EXCLUSION_MISSING": "Do not inherit Image A background",
        "POSE_STYLE_EXCLUSION_MISSING": "Do not inherit Image B visual style",
        "RAW_REFERENCE_EXCLUSION_MISSING": "Raw full-image references are audit-only and are not renderer inputs",
    }
    for code, text in required.items():
        if text not in prompt:
            violations.append({"code": code, "stage": "reference_preprocessing_prompt", "message": text})
    return violations


def preprocess_run_references(
    run_dir: Path,
    *,
    input_base: Path,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    normalized = _read_object(run_dir / "input.normalized.json")
    characters = [Path(str(value)).resolve() for value in normalized.get("character_images", [])]
    pose = Path(str(normalized.get("pose_reference", ""))).resolve()
    if not characters or not all(path.is_file() for path in characters) or not pose.is_file():
        raise ReferencePreprocessingError("Normalized character and pose references are required for preprocessing.")
    opts = options if isinstance(options, dict) else {}
    character_masks, pose_mask = _normalize_mask_paths(opts, len(characters), input_base)
    character_results: list[dict[str, Any]] = []
    artifact_names: list[str] = []
    for index, (source, explicit_mask) in enumerate(zip(characters, character_masks), start=1):
        if index == 1:
            result = character_cutout(source, run_dir, explicit_mask=explicit_mask)
        else:
            result = character_cutout(source, run_dir, explicit_mask=explicit_mask, artifact_prefix=f"character_cutout_{index:02d}")
        character_results.append(result)
        artifact_names.extend([result["cutout_path"].name, result["mask_path"].name, result["meta_path"].name])
    pose_result = pose_reference_sanitization(pose, run_dir, explicit_mask=pose_mask)
    artifact_names.extend([
        pose_result["sanitized_path"].name,
        pose_result["mask_path"].name,
        pose_result["meta_path"].name,
        pose_result["crop_path"].name,
    ])
    statuses = [result["meta"]["status"] for result in character_results] + [pose_result["meta"]["status"]]
    overall_status = PASS if all(status == PASS for status in statuses) else NOT_ASSESSABLE
    renderer_order = [
        {
            "index": index,
            "role": "CHARACTER",
            "representation": "CHARACTER_CUTOUT",
            "authority": deepcopy(CHARACTER_AUTHORITY),
        }
        for index in range(1, len(character_results) + 1)
    ]
    renderer_order.append({
        "index": len(character_results) + 1,
        "role": "POSE",
        "representation": "POSE_SANITIZED",
        "authority": deepcopy(POSE_AUTHORITY),
    })
    plan = {
        "schema_version": PREPROCESSING_SCHEMA_VERSION,
        "stage": "REFERENCE_PREPROCESSING_BEFORE_RENDERER_ADAPTER",
        "status": overall_status,
        "renderer_handoff_authorized": overall_status == PASS,
        "character_cutouts": [
            {
                "index": index,
                "status": result["meta"]["status"],
                "cutout": result["cutout_path"].name,
                "mask": result["mask_path"].name,
                "meta": result["meta_path"].name,
            }
            for index, result in enumerate(character_results, start=1)
        ],
        "pose_sanitization": {
            "status": pose_result["meta"]["status"],
            "image": pose_result["sanitized_path"].name,
            "subject_mask": pose_result["mask_path"].name,
            "meta": pose_result["meta_path"].name,
            "crop_overlay": pose_result["crop_path"].name,
        },
        "renderer_reference_order": renderer_order,
        "raw_full_image_references_transmitted": False,
        "pose_only_reference": {
            "status": "FOUNDATION_ONLY",
            "implemented": False,
            "future_contracts": [
                "pose_lines.png",
                "silhouette_proxy.png",
                "crop_contract.json",
                "occlusion_contract.json",
                "perspective_contract.json",
            ],
        },
        "pixel_level_renderer_benefit": NOT_ASSESSABLE,
        "safety_boundary": "Preprocessing reduces irrelevant scene semantics and role mixing; it does not bypass renderer safety systems or guarantee acceptance of high-risk final content.",
        "violations": [
            {
                "code": "REFERENCE_PREPROCESSING_NOT_ASSESSABLE",
                "message": "At least one subject mask is not sufficiently observable for renderer handoff.",
            }
        ] if overall_status != PASS else [],
    }
    plan_path = run_dir / "reference_preprocessing.json"
    _write_json(plan_path, plan)
    artifact_names.append(plan_path.name)
    plan["artifact_names"] = artifact_names
    _write_json(plan_path, plan)
    _rewrite_render_package(run_dir, character_results, pose_result, plan)
    return plan


def preprocess_standalone(
    character_paths: Iterable[Path],
    pose_path: Path,
    output_dir: Path,
    *,
    character_masks: Iterable[Path | None] | None = None,
    pose_mask: Path | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    characters = [path.resolve() for path in character_paths]
    masks = list(character_masks or [])
    while len(masks) < len(characters):
        masks.append(None)
    character_results = [
        character_cutout(
            source,
            output_dir,
            explicit_mask=masks[index - 1],
            artifact_prefix="character_cutout" if index == 1 else f"character_cutout_{index:02d}",
        )
        for index, source in enumerate(characters, start=1)
    ]
    pose_result = pose_reference_sanitization(pose_path.resolve(), output_dir, explicit_mask=pose_mask)
    status = PASS if all(item["meta"]["status"] == PASS for item in character_results) and pose_result["meta"]["status"] == PASS else NOT_ASSESSABLE
    result = {
        "schema_version": PREPROCESSING_SCHEMA_VERSION,
        "status": status,
        "character_cutouts": [item["meta"] for item in character_results],
        "pose_sanitization": pose_result["meta"],
        "pixel_level_renderer_benefit": NOT_ASSESSABLE,
    }
    _write_json(output_dir / "reference_preprocessing.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create v1.8.1 character cutout and sanitized pose artifacts.")
    parser.add_argument("--character", action="append", type=Path, required=True)
    parser.add_argument("--pose", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--character-mask", action="append", type=Path, default=[])
    parser.add_argument("--pose-mask", type=Path)
    parser.add_argument("--version", action="version", version=PREPROCESSING_SCHEMA_VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = preprocess_standalone(
            args.character,
            args.pose,
            args.output,
            character_masks=args.character_mask,
            pose_mask=args.pose_mask,
        )
    except (OSError, ReferencePreprocessingError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == PASS else 3


if __name__ == "__main__":
    raise SystemExit(main())
