#!/usr/bin/env python3
"""Capability-aware renderer requests, GPT Image 2 execution, and immutable receipts.

This module is a v1.7 adapter. It deliberately does not modify the retained v1.5
generation, scoring, or finalization implementation.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import struct
import sys
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid


ADAPTER_VERSION = "1.0"
BUILTIN_BACKEND = "codex_builtin_imagegen"
OPENAI_BACKEND = "openai_images_api_gpt_image_2"
STRICT_REFERENCE_REPLICA = "STRICT_REFERENCE_REPLICA"
FULLBODY_EXPANSION = "FULLBODY_EXPANSION"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_API_BASE = "https://api.openai.com"
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3_840
MAX_ASPECT = 3.0


class RendererAdapterError(ValueError):
    """Raised for invalid packages, requests, responses, or receipts."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RendererAdapterError(f"Invalid JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RendererAdapterError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length >= 7:
                height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
                return width, height
            break
        offset += length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    payload = data[20:]
    if kind == b"VP8X" and len(payload) >= 10:
        width = 1 + int.from_bytes(payload[4:7], "little")
        height = 1 + int.from_bytes(payload[7:10], "little")
        return width, height
    if kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
        height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
        return width, height
    if kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_dimensions(path: Path) -> tuple[int, int, str]:
    data = path.read_bytes()
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return width, height, "png"
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width, height = struct.unpack("<HH", data[6:10])
        return width, height, "gif"
    jpeg = _jpeg_dimensions(data)
    if jpeg:
        return jpeg[0], jpeg[1], "jpeg"
    webp = _webp_dimensions(data)
    if webp:
        return webp[0], webp[1], "webp"
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            return int(image.width), int(image.height), str(image.format or "unknown").casefold()
    except Exception as exc:
        raise RendererAdapterError(f"Unsupported or unreadable image {path}: {exc}") from exc


def _is_valid_gpt_image_2_size(width: int, height: int) -> bool:
    if width <= 0 or height <= 0 or width > MAX_EDGE or height > MAX_EDGE:
        return False
    if width % 16 or height % 16:
        return False
    pixels = width * height
    if pixels < MIN_PIXELS or pixels > MAX_PIXELS:
        return False
    return max(width, height) / min(width, height) <= MAX_ASPECT


def derive_gpt_image_2_size(width: int, height: int) -> dict[str, Any]:
    """Preserve an input canvas exactly when valid, otherwise find its closest valid canvas."""

    if width <= 0 or height <= 0:
        raise RendererAdapterError("Canvas dimensions must be positive integers.")
    source_ratio = width / height
    if source_ratio > MAX_ASPECT or source_ratio < 1 / MAX_ASPECT:
        raise RendererAdapterError("Pose-reference aspect ratio exceeds the GPT Image 2 3:1 limit.")
    if _is_valid_gpt_image_2_size(width, height):
        return {
            "width": width,
            "height": height,
            "size": f"{width}x{height}",
            "source_width": width,
            "source_height": height,
            "source_ratio": source_ratio,
            "requested_ratio": source_ratio,
            "ratio_error": 0.0,
            "selection_rule": "exact_pose_or_contract_canvas",
        }

    target_area = min(max(width * height, MIN_PIXELS), MAX_PIXELS)
    candidates: list[tuple[tuple[float, float, float, int], int, int]] = []
    for candidate_width in range(16, MAX_EDGE + 1, 16):
        ideal_height = candidate_width / source_ratio
        candidate_height = max(16, int(round(ideal_height / 16)) * 16)
        if not _is_valid_gpt_image_2_size(candidate_width, candidate_height):
            continue
        ratio_error = abs((candidate_width / candidate_height) - source_ratio) / source_ratio
        area_error = abs(math.log((candidate_width * candidate_height) / target_area))
        scale_error = abs(candidate_width - width) + abs(candidate_height - height)
        # Keep ratio error strongly bounded without choosing a needlessly large and costly
        # canvas merely because a much larger integer multiple reproduces the ratio exactly.
        candidates.append(((ratio_error * 25.0 + area_error, ratio_error, area_error, scale_error), candidate_width, candidate_height))
    if not candidates:
        raise RendererAdapterError("No valid GPT Image 2 size could preserve the source aspect ratio.")
    _, requested_width, requested_height = min(candidates, key=lambda item: item[0])
    requested_ratio = requested_width / requested_height
    return {
        "width": requested_width,
        "height": requested_height,
        "size": f"{requested_width}x{requested_height}",
        "source_width": width,
        "source_height": height,
        "source_ratio": source_ratio,
        "requested_ratio": requested_ratio,
        "ratio_error": abs(requested_ratio - source_ratio) / source_ratio,
        "selection_rule": "nearest_valid_multiple_of_16_without_reframing",
    }


def backend_capabilities(backend: str) -> dict[str, Any]:
    if backend == BUILTIN_BACKEND:
        return {
            "schema_version": ADAPTER_VERSION,
            "backend": backend,
            "execution_available_from_script": False,
            "prompt": True,
            "ordered_generic_references": True,
            "typed_reference_roles": False,
            "output_size": False,
            "mask": False,
            "regional_control": False,
            "skeleton_depth_segmentation_control": False,
            "pose_only_reference": {"feature_flag": False, "implemented": False},
        }
    if backend == OPENAI_BACKEND:
        return {
            "schema_version": ADAPTER_VERSION,
            "backend": backend,
            "execution_available_from_script": True,
            "endpoint": "/v1/images/edits",
            "prompt": True,
            "ordered_generic_references": True,
            "typed_reference_roles": False,
            "output_size": True,
            "mask": True,
            "regional_control": "mask_only_soft_guidance",
            "skeleton_depth_segmentation_control": False,
            "input_fidelity": "high_automatic_for_gpt_image_2",
            "pose_only_reference": {"feature_flag": False, "implemented": False},
        }
    raise RendererAdapterError(f"Unsupported renderer backend: {backend}")


def _safe_child(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RendererAdapterError(f"Path escapes run directory: {candidate}") from exc
    return resolved


def _load_optional_object(path: Path) -> dict[str, Any]:
    return read_object(path) if path.is_file() else {}


def _completion_mode(run_dir: Path, override: str | None) -> tuple[str, str]:
    if override:
        return override, "explicit_adapter_override"
    intent = _load_optional_object(run_dir / "completion_intent.yaml")
    if intent.get("mode"):
        return str(intent["mode"]), "completion_intent.yaml"
    contract = _load_optional_object(run_dir / "composition_contract.json")
    if contract.get("completion_intent"):
        return str(contract["completion_intent"]), "composition_contract.json"
    runtime = _load_optional_object(run_dir / "runtime_modes.json")
    mode = ((runtime.get("completion", {}) or {}).get("mode"))
    if mode:
        return str(mode), "runtime_modes.json"
    raise RendererAdapterError(
        "Completion intent is absent. Use a v1.7 run or pass --completion-intent explicitly for a legacy regression."
    )


def _contract_canvas(contract: dict[str, Any]) -> tuple[int, int, str] | None:
    candidates: list[tuple[str, Any]] = [
        ("composition_contract.canvas_geometry", contract.get("canvas_geometry")),
        ("composition_contract.reference_canvas", contract.get("reference_canvas")),
        ("composition_contract.canvas", contract.get("canvas")),
    ]
    framing = contract.get("framing", {}) if isinstance(contract.get("framing"), dict) else {}
    candidates.append(("composition_contract.framing.reference_canvas", framing.get("reference_canvas")))
    for source, value in candidates:
        if not isinstance(value, dict):
            continue
        width = value.get("width")
        height = value.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height, source
    return None


def _reference_manifest(run_dir: Path) -> tuple[Path, list[dict[str, Any]]]:
    package = run_dir / "render_package"
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        raise RendererAdapterError("render_package/manifest.json is required for ordered reference provenance.")
    manifest = read_object(manifest_path)
    raw_references = manifest.get("references", [])
    if not isinstance(raw_references, list) or not raw_references:
        raise RendererAdapterError("render_package/manifest.json has no ordered references.")
    references: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_references, start=1):
        if not isinstance(raw, dict):
            raise RendererAdapterError("Every render reference must be an object.")
        relative = Path(str(raw.get("file", "")))
        image_path = _safe_child(package, package / relative)
        if not image_path.is_file():
            raise RendererAdapterError(f"Missing renderer reference: {relative}")
        width, height, image_format = image_dimensions(image_path)
        references.append({
            "index": index,
            "role": str(raw.get("role", "unspecified")).upper(),
            "semantic_role_is_renderer_parameter": False,
            "path": str(image_path),
            "package_relative_path": relative.as_posix(),
            "sha256": sha256_file(image_path),
            "width": width,
            "height": height,
            "format": image_format,
            "conditioning_mode": "generic_visual_reference",
            "preprocessing_lineage": None,
        })
    return package, references


def _mime_type(path: Path, image_format: str) -> str:
    known = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
    return known.get(image_format, mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def _find_pose_reference(references: Iterable[dict[str, Any]]) -> dict[str, Any]:
    poses = [item for item in references if item.get("role") == "POSE"]
    if len(poses) != 1:
        raise RendererAdapterError(f"Expected exactly one POSE reference, found {len(poses)}.")
    return poses[0]


def _attempt_id(value: str | None) -> str:
    if value:
        return value
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def compile_renderer_request(
    run_dir: Path,
    backend: str,
    candidate_name: str,
    *,
    attempt_id: str | None = None,
    completion_intent_override: str | None = None,
    explicit_size: str | None = None,
    model: str = DEFAULT_MODEL,
    quality: str = "medium",
    output_format: str = "png",
    background: str | None = None,
    api_base: str = DEFAULT_API_BASE,
    pose_only_reference_enabled: bool = False,
) -> tuple[Path, dict[str, Any]]:
    run_dir = run_dir.resolve()
    if pose_only_reference_enabled:
        raise RendererAdapterError("POSE_ONLY_REFERENCE is feature-flagged off and is not implemented.")
    capabilities = backend_capabilities(backend)
    prompt_path = run_dir / "final_prompt.md"
    if not prompt_path.is_file():
        raise RendererAdapterError("final_prompt.md is required.")
    prompt_bytes = prompt_path.read_bytes()
    try:
        prompt = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RendererAdapterError("final_prompt.md must be UTF-8.") from exc
    _, references = _reference_manifest(run_dir)
    pose_reference = _find_pose_reference(references)
    completion_mode, completion_source = _completion_mode(run_dir, completion_intent_override)
    strict = completion_mode == STRICT_REFERENCE_REPLICA
    contract = _load_optional_object(run_dir / "composition_contract.json")
    contract_canvas = _contract_canvas(contract)
    if contract_canvas:
        source_width, source_height, canvas_source = contract_canvas
    else:
        source_width = int(pose_reference["width"])
        source_height = int(pose_reference["height"])
        canvas_source = "pose_reference_original_canvas"

    requested_canvas: dict[str, Any] | None = None
    transmitted_size: str | None = None
    if explicit_size:
        try:
            width_text, height_text = explicit_size.casefold().split("x", 1)
            explicit_width, explicit_height = int(width_text), int(height_text)
        except (ValueError, AttributeError) as exc:
            raise RendererAdapterError("--size must be WIDTHxHEIGHT.") from exc
        if backend == OPENAI_BACKEND and not _is_valid_gpt_image_2_size(explicit_width, explicit_height):
            raise RendererAdapterError("Explicit size violates GPT Image 2 size constraints.")
        requested_canvas = {
            "width": explicit_width,
            "height": explicit_height,
            "size": f"{explicit_width}x{explicit_height}",
            "source_width": source_width,
            "source_height": source_height,
            "source_ratio": source_width / source_height,
            "requested_ratio": explicit_width / explicit_height,
            "ratio_error": abs((explicit_width / explicit_height) - (source_width / source_height)) / (source_width / source_height),
            "selection_rule": "explicit_size",
        }
        transmitted_size = requested_canvas["size"] if capabilities.get("output_size") else None
    elif strict:
        requested_canvas = derive_gpt_image_2_size(source_width, source_height)
        transmitted_size = requested_canvas["size"] if capabilities.get("output_size") else None

    if strict and not capabilities.get("output_size"):
        canvas_enforcement = "UNAVAILABLE_BEST_EFFORT"
    elif strict:
        canvas_enforcement = "REQUEST_PARAMETER"
    else:
        canvas_enforcement = "NOT_REQUIRED"

    resolved_candidate = _safe_child(run_dir, run_dir / candidate_name)
    attempt = _attempt_id(attempt_id)
    attempt_dir = _safe_child(run_dir, run_dir / "renderer_attempts" / attempt)
    if attempt_dir.exists():
        raise RendererAdapterError(f"Renderer attempt already exists: {attempt_dir}")
    attempt_dir.mkdir(parents=True)
    prompt_copy = attempt_dir / "renderer_prompt.txt"
    prompt_copy.write_bytes(prompt_bytes)
    write_json(attempt_dir / "renderer_capabilities.json", {**capabilities, "recorded_at_utc": utc_now()})

    endpoint = None
    operation = "external_tool_dispatch"
    request_parameters: dict[str, Any] = {"prompt": prompt}
    transmitted_form_fields: list[dict[str, str]] = []
    if backend == OPENAI_BACKEND:
        operation = "image_edit_reference_generation"
        endpoint = api_base.rstrip("/") + "/v1/images/edits"
        request_parameters = {
            "model": model,
            "prompt": prompt,
            "size": transmitted_size or "auto",
            "quality": quality,
            "output_format": output_format,
        }
        if background:
            request_parameters["background"] = background
        transmitted_form_fields = [
            {"name": key, "value": str(value)} for key, value in request_parameters.items()
        ]
    request = {
        "schema_version": ADAPTER_VERSION,
        "attempt_id": attempt,
        "compiled_at_utc": utc_now(),
        "backend": backend,
        "operation": operation,
        "method": "POST" if endpoint else None,
        "endpoint": endpoint,
        "model": model if backend == OPENAI_BACKEND else None,
        "completion_intent": completion_mode,
        "completion_intent_source": completion_source,
        "strict_reference_replica": strict,
        "composition_contract_sha256": sha256_file(run_dir / "composition_contract.json") if (run_dir / "composition_contract.json").is_file() else None,
        "submitted_prompt": prompt,
        "submitted_prompt_sha256": sha256_bytes(prompt_bytes),
        "prompt_artifact": "renderer_prompt.txt",
        "references": references,
        "ordered_reference_manifest_sha256": canonical_hash(references),
        "request_parameters": request_parameters,
        "transmitted_form_fields": transmitted_form_fields,
        "transmitted_files": [
            {
                "field": "image[]" if backend == OPENAI_BACKEND else "referenced_image_paths[]",
                "index": item["index"],
                "role": item["role"],
                "path": item["path"],
                "filename": Path(str(item["path"])).name,
                "content_type": _mime_type(Path(str(item["path"])), str(item["format"])),
                "sha256": item["sha256"],
                "bytes": Path(str(item["path"])).stat().st_size,
            }
            for item in references
        ],
        "requested_canvas": requested_canvas,
        "canvas_source": canvas_source,
        "canvas_enforcement": canvas_enforcement,
        "candidate_output": str(resolved_candidate),
        "pose_only_reference": {"feature_flag": False, "implemented": False, "sent": False},
        "unsupported_controls": [
            "typed_reference_roles",
            "reference_weights",
            "subject_placement_coordinates",
            "crop_rectangle",
            "camera_matrix",
            "depth_order_parameter",
            "skeleton_depth_segmentation_control",
        ],
        "wire": None,
        "local_final_prompt_hash_is_execution_proof": False,
    }
    if backend == OPENAI_BACKEND:
        prepared_boundary = f"ccp-{attempt}"
        prepared_body, prepared_boundary = build_multipart_body(request, boundary=prepared_boundary)
        request["wire"] = {
            "content_type": f"multipart/form-data; boundary={prepared_boundary}",
            "body_sha256": sha256_bytes(prepared_body),
            "body_bytes": len(prepared_body),
            "authorization_recorded": False,
            "prepared_not_sent": True,
        }
    write_json(attempt_dir / "renderer_request.json", request)
    return attempt_dir, request


def _multipart_part(name: str, value: bytes, boundary: str, filename: str | None = None, content_type: str | None = None) -> bytes:
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename.replace(chr(34), "_")}"'
    headers = [f"--{boundary}", disposition]
    if content_type:
        headers.append(f"Content-Type: {content_type}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + value + b"\r\n"


def build_multipart_body(request_record: dict[str, Any], boundary: str | None = None) -> tuple[bytes, str]:
    boundary = boundary or f"ccp-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for item in request_record.get("transmitted_form_fields", []):
        chunks.append(_multipart_part(str(item["name"]), str(item["value"]).encode("utf-8"), boundary))
    for item in request_record.get("transmitted_files", []):
        path = Path(str(item["path"]))
        if sha256_file(path) != item.get("sha256"):
            raise RendererAdapterError(f"Reference changed after request compilation: {path}")
        chunks.append(_multipart_part(
            str(item["field"]),
            path.read_bytes(),
            boundary,
            filename=str(item["filename"]),
            content_type=str(item["content_type"]),
        ))
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _safe_response_headers(headers: Any) -> dict[str, str]:
    allowed = {"content-type", "date", "openai-processing-ms", "x-request-id", "x-ratelimit-remaining-images"}
    output: dict[str, str] = {}
    if headers is None:
        return output
    for key, value in headers.items():
        if str(key).casefold() in allowed:
            output[str(key).casefold()] = str(value)
    return output


def _redact_response(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: "<omitted-base64-image>" if key in {"b64_json", "result"} and isinstance(value, str) else _redact_response(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_redact_response(item) for item in payload]
    return payload


def _extract_image_payload(payload: dict[str, Any]) -> tuple[bytes, str | None]:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RendererAdapterError("Renderer response has no image data item.")
    encoded = data[0].get("b64_json")
    if not isinstance(encoded, str) or not encoded:
        raise RendererAdapterError("Renderer response does not contain data[0].b64_json.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RendererAdapterError("Renderer returned invalid base64 image data.") from exc
    revised_prompt = data[0].get("revised_prompt") or payload.get("revised_prompt")
    return image_bytes, str(revised_prompt) if revised_prompt else None


def _write_receipt(
    attempt_dir: Path,
    request_record: dict[str, Any],
    response_record: dict[str, Any],
    *,
    status: str,
    candidate_path: Path | None,
    revised_prompt: str | None,
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    request_path = attempt_dir / "renderer_request.json"
    response_path = attempt_dir / "renderer_response.json"
    prompt_path = attempt_dir / "renderer_prompt.txt"
    requested_canvas = request_record.get("requested_canvas") or {}
    actual_canvas: dict[str, Any] | None = None
    output_sha: str | None = None
    strict_canvas_status = "NOT_ASSESSABLE"
    if candidate_path and candidate_path.is_file():
        width, height, image_format = image_dimensions(candidate_path)
        actual_canvas = {"width": width, "height": height, "size": f"{width}x{height}", "format": image_format}
        output_sha = sha256_file(candidate_path)
        if request_record.get("strict_reference_replica"):
            strict_canvas_status = "PASS" if (
                requested_canvas.get("width") == width and requested_canvas.get("height") == height
            ) else "FAIL"
    receipt = {
        "schema_version": ADAPTER_VERSION,
        "attempt_id": request_record.get("attempt_id"),
        "status": status,
        "backend": request_record.get("backend"),
        "model": request_record.get("model"),
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "renderer_request_artifact": "renderer_request.json",
        "renderer_request_sha256": sha256_file(request_path),
        "submitted_prompt_artifact": "renderer_prompt.txt",
        "submitted_prompt_sha256": sha256_file(prompt_path),
        "revised_prompt": revised_prompt,
        "revised_prompt_sha256": sha256_bytes(revised_prompt.encode("utf-8")) if revised_prompt else None,
        "ordered_reference_manifest_sha256": request_record.get("ordered_reference_manifest_sha256"),
        "renderer_response_artifact": "renderer_response.json",
        "renderer_response_sha256": sha256_file(response_path),
        "wire_body_sha256": ((request_record.get("wire") or {}).get("body_sha256")),
        "wire_body_bytes": ((request_record.get("wire") or {}).get("body_bytes")),
        "requested_canvas": requested_canvas or None,
        "actual_output_canvas": actual_canvas,
        "strict_canvas_lock": {
            "requested": bool(request_record.get("strict_reference_replica")),
            "request_parameter_sent": request_record.get("canvas_enforcement") == "REQUEST_PARAMETER",
            "status": strict_canvas_status,
        },
        "candidate_output": str(candidate_path) if candidate_path else None,
        "output_image_sha256": output_sha,
        "local_final_prompt_hash_is_execution_proof": False,
        "execution_proof": status == "COMPLETED" and output_sha is not None,
    }
    write_json(attempt_dir / "renderer_request_receipt.json", receipt)
    return receipt


def execute_openai_request(
    attempt_dir: Path,
    *,
    api_key: str,
    timeout_seconds: int = 180,
    force: bool = False,
) -> dict[str, Any]:
    if not api_key:
        raise RendererAdapterError("OPENAI_API_KEY is required for the GPT Image 2 API backend.")
    request_path = attempt_dir / "renderer_request.json"
    request_record = read_object(request_path)
    if request_record.get("backend") != OPENAI_BACKEND:
        raise RendererAdapterError("Only the GPT Image 2 API backend can be executed by this script.")
    candidate_path = Path(str(request_record.get("candidate_output")))
    if candidate_path.exists() and not force:
        raise RendererAdapterError(f"Immutable candidate already exists: {candidate_path}")
    prepared_wire = request_record.get("wire") or {}
    prepared_content_type = str(prepared_wire.get("content_type", ""))
    prepared_boundary = prepared_content_type.split("boundary=", 1)[1] if "boundary=" in prepared_content_type else None
    body, boundary = build_multipart_body(request_record, boundary=prepared_boundary)
    if prepared_wire.get("body_sha256") and prepared_wire.get("body_sha256") != sha256_bytes(body):
        raise RendererAdapterError("Prepared renderer wire body changed before dispatch.")
    request_record["wire"] = {
        "content_type": f"multipart/form-data; boundary={boundary}",
        "body_sha256": sha256_bytes(body),
        "body_bytes": len(body),
        "authorization_recorded": False,
        "prepared_not_sent": False,
    }
    write_json(request_path, request_record)
    started_at = utc_now()
    http_request = Request(
        str(request_record["endpoint"]),
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": str(request_record["wire"]["content_type"]),
            "Accept": "application/json",
            "User-Agent": f"character-consistency-pipeline-renderer-adapter/{ADAPTER_VERSION}",
        },
    )
    response_record: dict[str, Any]
    try:
        with urlopen(http_request, timeout=timeout_seconds) as response:
            raw_response = response.read()
            status_code = int(getattr(response, "status", 200))
            headers = _safe_response_headers(response.headers)
        payload = json.loads(raw_response.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RendererAdapterError("Renderer response must be a JSON object.")
        image_bytes, revised_prompt = _extract_image_payload(payload)
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_bytes(image_bytes)
        output_width, output_height, output_format = image_dimensions(candidate_path)
        response_record = {
            "status": "COMPLETED",
            "http_status": status_code,
            "headers": headers,
            "response_metadata": _redact_response(payload),
            "revised_prompt": revised_prompt,
            "output": {
                "path": str(candidate_path),
                "sha256": sha256_file(candidate_path),
                "width": output_width,
                "height": output_height,
                "format": output_format,
                "bytes": candidate_path.stat().st_size,
            },
        }
        write_json(attempt_dir / "renderer_response.json", response_record)
        receipt = _write_receipt(
            attempt_dir,
            request_record,
            response_record,
            status="COMPLETED",
            candidate_path=candidate_path,
            revised_prompt=revised_prompt,
            started_at=started_at,
            completed_at=utc_now(),
        )
        if receipt["strict_canvas_lock"]["status"] == "FAIL":
            raise RendererAdapterError("Renderer output dimensions do not match the strict request size.")
        return receipt
    except HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"error": {"type": "http_error", "message": "Non-JSON renderer error response"}}
        response_record = {
            "status": "FAILED",
            "http_status": int(exc.code),
            "headers": _safe_response_headers(exc.headers),
            "response_metadata": _redact_response(payload),
        }
        write_json(attempt_dir / "renderer_response.json", response_record)
        _write_receipt(
            attempt_dir,
            request_record,
            response_record,
            status="FAILED",
            candidate_path=None,
            revised_prompt=None,
            started_at=started_at,
            completed_at=utc_now(),
        )
        raise RendererAdapterError(f"GPT Image 2 request failed with HTTP {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        response_record = {
            "status": "FAILED",
            "http_status": None,
            "headers": {},
            "response_metadata": {"error": {"type": type(exc).__name__, "message": str(exc.reason if isinstance(exc, URLError) else exc)}},
        }
        write_json(attempt_dir / "renderer_response.json", response_record)
        _write_receipt(
            attempt_dir,
            request_record,
            response_record,
            status="FAILED",
            candidate_path=None,
            revised_prompt=None,
            started_at=started_at,
            completed_at=utc_now(),
        )
        raise RendererAdapterError(f"GPT Image 2 request transport failed: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, RendererAdapterError) as exc:
        # If a completed receipt already exists, preserve it (for example, a strict
        # dimension mismatch) and surface the failure without rewriting history.
        if (attempt_dir / "renderer_request_receipt.json").is_file():
            raise
        response_record = {
            "status": "FAILED",
            "http_status": locals().get("status_code"),
            "headers": locals().get("headers", {}),
            "response_metadata": {"error": {"type": type(exc).__name__, "message": str(exc)}},
        }
        write_json(attempt_dir / "renderer_response.json", response_record)
        _write_receipt(
            attempt_dir,
            request_record,
            response_record,
            status="FAILED",
            candidate_path=None,
            revised_prompt=None,
            started_at=started_at,
            completed_at=utc_now(),
        )
        raise RendererAdapterError(f"GPT Image 2 response processing failed: {exc}") from exc


def write_synthetic_completed_receipt(
    attempt_dir: Path,
    candidate_path: Path,
    *,
    revised_prompt: str | None = None,
    request_id: str = "synthetic-regression-only",
) -> dict[str, Any]:
    """Create a completed receipt only for deterministic adapter regression tests."""

    request_record = read_object(attempt_dir / "renderer_request.json")
    request_record["wire"] = {
        "content_type": "multipart/form-data; boundary=synthetic-regression-only",
        "body_sha256": canonical_hash({"synthetic": request_record.get("attempt_id")}),
        "body_bytes": 0,
        "authorization_recorded": False,
        "synthetic_regression_only": True,
    }
    write_json(attempt_dir / "renderer_request.json", request_record)
    width, height, image_format = image_dimensions(candidate_path)
    response_record = {
        "status": "COMPLETED",
        "http_status": 200,
        "headers": {"x-request-id": request_id},
        "response_metadata": {"synthetic_regression_only": True},
        "revised_prompt": revised_prompt,
        "output": {
            "path": str(candidate_path),
            "sha256": sha256_file(candidate_path),
            "width": width,
            "height": height,
            "format": image_format,
            "bytes": candidate_path.stat().st_size,
        },
    }
    write_json(attempt_dir / "renderer_response.json", response_record)
    return _write_receipt(
        attempt_dir,
        request_record,
        response_record,
        status="COMPLETED",
        candidate_path=candidate_path,
        revised_prompt=revised_prompt,
        started_at=utc_now(),
        completed_at=utc_now(),
    )


def verify_renderer_receipt(
    receipt_path: Path,
    *,
    candidate_path: Path | None = None,
    current_prompt_path: Path | None = None,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    receipt_path = receipt_path.resolve()
    attempt_dir = receipt_path.parent
    receipt = read_object(receipt_path)
    if receipt.get("status") != "COMPLETED" or receipt.get("execution_proof") is not True:
        raise RendererAdapterError("Renderer receipt does not prove completed execution.")
    request_path = attempt_dir / str(receipt.get("renderer_request_artifact", "renderer_request.json"))
    response_path = attempt_dir / str(receipt.get("renderer_response_artifact", "renderer_response.json"))
    prompt_path = attempt_dir / str(receipt.get("submitted_prompt_artifact", "renderer_prompt.txt"))
    if sha256_file(request_path) != receipt.get("renderer_request_sha256"):
        raise RendererAdapterError("Renderer request artifact hash mismatch.")
    if sha256_file(response_path) != receipt.get("renderer_response_sha256"):
        raise RendererAdapterError("Renderer response artifact hash mismatch.")
    if sha256_file(prompt_path) != receipt.get("submitted_prompt_sha256"):
        raise RendererAdapterError("Submitted renderer prompt hash mismatch.")
    request_record = read_object(request_path)
    response_record = read_object(response_path)
    if request_record.get("submitted_prompt_sha256") != receipt.get("submitted_prompt_sha256"):
        raise RendererAdapterError("Request and receipt disagree on submitted prompt hash.")
    if request_record.get("ordered_reference_manifest_sha256") != receipt.get("ordered_reference_manifest_sha256"):
        raise RendererAdapterError("Request and receipt disagree on reference ordering/hash manifest.")
    for reference in request_record.get("references", []):
        path = Path(str(reference.get("path")))
        if not path.is_file() or sha256_file(path) != reference.get("sha256"):
            raise RendererAdapterError(f"Renderer reference changed or is missing: {path}")
    if current_prompt_path is not None and sha256_file(current_prompt_path) != receipt.get("submitted_prompt_sha256"):
        raise RendererAdapterError("Current final_prompt.md differs from the prompt actually submitted to the renderer.")
    actual_candidate = candidate_path or Path(str(receipt.get("candidate_output")))
    if not actual_candidate.is_file() or sha256_file(actual_candidate) != receipt.get("output_image_sha256"):
        raise RendererAdapterError("Candidate image does not match the renderer receipt output hash.")
    if receipt.get("strict_canvas_lock", {}).get("requested"):
        if receipt.get("strict_canvas_lock", {}).get("request_parameter_sent") is not True:
            raise RendererAdapterError("Strict receipt lacks request-level canvas enforcement.")
        if receipt.get("strict_canvas_lock", {}).get("status") != "PASS":
            raise RendererAdapterError("Strict output canvas does not match the renderer request.")
    if not allow_synthetic and (response_record.get("response_metadata", {}) or {}).get("synthetic_regression_only"):
        raise RendererAdapterError("Synthetic regression receipts cannot prove a real renderer execution.")
    return {
        "status": "PASS",
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256_file(receipt_path),
        "renderer_request_sha256": receipt.get("renderer_request_sha256"),
        "submitted_prompt_sha256": receipt.get("submitted_prompt_sha256"),
        "ordered_reference_manifest_sha256": receipt.get("ordered_reference_manifest_sha256"),
        "output_image_sha256": receipt.get("output_image_sha256"),
        "requested_canvas": receipt.get("requested_canvas"),
        "actual_output_canvas": receipt.get("actual_output_canvas"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile and optionally execute a provenance-bound renderer request.")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--backend", choices=[BUILTIN_BACKEND, OPENAI_BACKEND])
    parser.add_argument("--execute-attempt", type=Path, help="Execute an already compiled GPT Image 2 attempt directory.")
    parser.add_argument("--candidate", default="candidate_api_01.png")
    parser.add_argument("--attempt-id")
    parser.add_argument("--completion-intent", choices=[STRICT_REFERENCE_REPLICA, FULLBODY_EXPANSION])
    parser.add_argument("--size")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--quality", choices=["low", "medium", "high", "auto"], default="medium")
    parser.add_argument("--output-format", choices=["png", "jpeg", "webp"], default="png")
    parser.add_argument("--background", choices=["transparent", "opaque", "auto"])
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", DEFAULT_API_BASE))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--pose-only-reference", action="store_true", help="Reserved feature flag; currently fails closed.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.execute_attempt is not None:
            if args.run_dir is not None or args.backend is not None or args.prepare_only:
                raise RendererAdapterError("--execute-attempt cannot be combined with request compilation options.")
            api_key = os.environ.get(args.api_key_env, "")
            receipt = execute_openai_request(
                args.execute_attempt.resolve(),
                api_key=api_key,
                timeout_seconds=args.timeout_seconds,
                force=args.force,
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        if args.run_dir is None or args.backend is None:
            raise RendererAdapterError("--run-dir and --backend are required when compiling a request.")
        attempt_dir, request_record = compile_renderer_request(
            args.run_dir,
            args.backend,
            args.candidate,
            attempt_id=args.attempt_id,
            completion_intent_override=args.completion_intent,
            explicit_size=args.size,
            model=args.model,
            quality=args.quality,
            output_format=args.output_format,
            background=args.background,
            api_base=args.api_base,
            pose_only_reference_enabled=args.pose_only_reference,
        )
        print(f"Prepared renderer request: {attempt_dir}")
        print(f"Prompt SHA-256: {request_record['submitted_prompt_sha256']}")
        print(f"Reference manifest SHA-256: {request_record['ordered_reference_manifest_sha256']}")
        if request_record.get("requested_canvas"):
            print(f"Requested canvas: {request_record['requested_canvas']['size']}")
        if args.prepare_only or args.backend == BUILTIN_BACKEND:
            if args.backend == BUILTIN_BACKEND and not args.prepare_only:
                print("Builtin backend cannot be dispatched by this script; request remains NOT_SENT.")
            return 0
        api_key = os.environ.get(args.api_key_env, "")
        receipt = execute_openai_request(
            attempt_dir,
            api_key=api_key,
            timeout_seconds=args.timeout_seconds,
            force=args.force,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RendererAdapterError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
