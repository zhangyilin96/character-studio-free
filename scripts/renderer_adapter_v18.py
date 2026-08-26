#!/usr/bin/env python3
"""v1.8.1 sanitized-reference gate over the frozen v1.7 Renderer Adapter."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import renderer_adapter as retained
from reference_preprocessing import PREPROCESSING_SCHEMA_VERSION

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from character_workflow.versioning import VERSIONS  # noqa: E402


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise retained.RendererAdapterError(f"Expected an object in {path}")
    return value


def _sanitized_manifest(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    preprocessing_path = run_dir / "reference_preprocessing.json"
    conflicts_path = run_dir / "composition_conflicts.json"
    manifest_path = run_dir / "render_package" / "manifest.json"
    for path in (preprocessing_path, conflicts_path, manifest_path):
        if not path.is_file():
            raise retained.RendererAdapterError(f"Missing v1.8.1 renderer prerequisite: {path.name}")
    preprocessing = _read_object(preprocessing_path)
    conflicts = _read_object(conflicts_path)
    manifest = _read_object(manifest_path)
    if preprocessing.get("schema_version") != PREPROCESSING_SCHEMA_VERSION:
        raise retained.RendererAdapterError("Reference preprocessing schema is not v1.8.1.")
    if preprocessing.get("status") != "PASS" or preprocessing.get("renderer_handoff_authorized") is not True:
        raise retained.RendererAdapterError("REFERENCE_PREPROCESSING_NOT_ASSESSABLE: renderer handoff is blocked.")
    if conflicts.get("render_authorized") is not True:
        raise retained.RendererAdapterError("composition_conflicts.json does not authorize rendering.")
    if preprocessing.get("raw_full_image_references_transmitted") is not False:
        raise retained.RendererAdapterError("Raw full-image reference transmission is forbidden in v1.8.1.")
    references = manifest.get("references", [])
    if not isinstance(references, list) or not references:
        raise retained.RendererAdapterError("Sanitized renderer reference manifest is empty.")
    representations = [item.get("representation") for item in references if isinstance(item, dict)]
    if len(representations) != len(references) or representations[-1:] != ["POSE_SANITIZED"]:
        raise retained.RendererAdapterError("Renderer manifest must end with one POSE_SANITIZED reference.")
    allowed_representations = {"CHARACTER_CUTOUT", "OUTFIT_SANITIZED", "POSE_SANITIZED"}
    if any(value not in allowed_representations for value in representations):
        raise retained.RendererAdapterError("Renderer manifest contains a non-sanitized representation.")
    if representations.count("POSE_SANITIZED") != 1 or "CHARACTER_CUTOUT" not in representations:
        raise retained.RendererAdapterError("Renderer manifest requires character cutout(s) plus one sanitized pose.")
    if representations.count("OUTFIT_SANITIZED") > 1:
        raise retained.RendererAdapterError("Renderer manifest permits at most one sanitized outfit reference.")
    if "OUTFIT_SANITIZED" in representations:
        outfit_index = representations.index("OUTFIT_SANITIZED")
        pose_index = representations.index("POSE_SANITIZED")
        last_character_index = max(index for index, value in enumerate(representations) if value == "CHARACTER_CUTOUT")
        if not last_character_index < outfit_index < pose_index:
            raise retained.RendererAdapterError(
                "Outfit transfer order must be CHARACTER_CUTOUT, OUTFIT_SANITIZED, then POSE_SANITIZED."
            )
    for item in references:
        relative = Path(str(item.get("file", "")))
        if "source_references" in relative.parts or "original" in relative.name.casefold():
            raise retained.RendererAdapterError("Raw/original reference appears in renderer transmission order.")
    return preprocessing, manifest


def compile_renderer_request_v18(
    run_dir: Path,
    backend: str,
    candidate_name: str,
    **kwargs: Any,
) -> tuple[Path, dict[str, Any]]:
    run_dir = run_dir.resolve()
    preprocessing, manifest = _sanitized_manifest(run_dir)
    attempt_dir, request = retained.compile_renderer_request(run_dir, backend, candidate_name, **kwargs)
    manifest_references = manifest["references"]
    if len(request.get("references", [])) != len(manifest_references):
        raise retained.RendererAdapterError("Compiled request changed sanitized reference cardinality.")
    for request_reference, manifest_reference in zip(request["references"], manifest_references):
        request_reference["conditioning_mode"] = "generic_visual_conditioning"
        request_reference["transmitted_representation"] = manifest_reference["representation"]
        request_reference["preprocessing_lineage"] = {
            "schema_version": PREPROCESSING_SCHEMA_VERSION,
            "meta": manifest_reference.get("preprocessing_meta"),
            "meta_sha256": manifest_reference.get("preprocessing_sha256"),
            "source_reference_transmitted": False,
        }
    for transmitted, manifest_reference in zip(request.get("transmitted_files", []), manifest_references):
        transmitted["representation"] = manifest_reference["representation"]
        transmitted["source_reference_transmitted"] = False
    request["pipeline_schema_version"] = PREPROCESSING_SCHEMA_VERSION
    request["product_versions"] = VERSIONS.as_dict()
    request["reference_preprocessing"] = {
        "status": preprocessing["status"],
        "raw_full_image_references_transmitted": False,
        "artifact_sha256": retained.sha256_file(run_dir / "reference_preprocessing.json"),
        "pixel_level_renderer_benefit": "NOT_ASSESSABLE",
    }
    request["ordered_reference_manifest_sha256"] = retained.canonical_hash(request["references"])
    retained.write_json(attempt_dir / "renderer_request.json", request)
    return attempt_dir, request


def verify_v18_attempt(attempt_dir: Path) -> dict[str, Any]:
    request_path = attempt_dir.resolve() / "renderer_request.json"
    request = _read_object(request_path)
    preprocessing = request.get("reference_preprocessing", {})
    if not isinstance(preprocessing, dict) or preprocessing.get("status") != "PASS":
        raise retained.RendererAdapterError("Compiled attempt lacks a PASS v1.8.1 preprocessing gate.")
    if preprocessing.get("raw_full_image_references_transmitted") is not False:
        raise retained.RendererAdapterError("Compiled attempt permits raw full-image transmission.")
    if any(
        item.get("transmitted_representation")
        not in {"CHARACTER_CUTOUT", "OUTFIT_SANITIZED", "POSE_SANITIZED"}
        for item in request.get("references", [])
    ):
        raise retained.RendererAdapterError("Compiled attempt contains a non-sanitized reference.")
    return request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile or execute a v1.8.1 sanitized renderer request.")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--backend", choices=[retained.BUILTIN_BACKEND, retained.OPENAI_BACKEND])
    parser.add_argument("--execute-attempt", type=Path)
    parser.add_argument("--candidate", default="candidate_api_01.png")
    parser.add_argument("--attempt-id")
    parser.add_argument("--completion-intent", choices=[retained.STRICT_REFERENCE_REPLICA, retained.FULLBODY_EXPANSION])
    parser.add_argument("--size")
    parser.add_argument("--model", default=retained.DEFAULT_MODEL)
    parser.add_argument("--quality", choices=["low", "medium", "high", "auto"], default="medium")
    parser.add_argument("--output-format", choices=["png", "jpeg", "webp"], default="png")
    parser.add_argument("--background", choices=["transparent", "opaque", "auto"])
    parser.add_argument("--api-base", default=os.environ.get("OPENAI_BASE_URL", retained.DEFAULT_API_BASE))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--version", action="version", version=PREPROCESSING_SCHEMA_VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.execute_attempt is not None:
            if args.run_dir is not None or args.backend is not None or args.prepare_only:
                raise retained.RendererAdapterError("--execute-attempt cannot be combined with compilation options.")
            verify_v18_attempt(args.execute_attempt)
            receipt = retained.execute_openai_request(
                args.execute_attempt.resolve(),
                api_key=os.environ.get(args.api_key_env, ""),
                timeout_seconds=args.timeout_seconds,
                force=args.force,
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        if args.run_dir is None or args.backend is None:
            raise retained.RendererAdapterError("--run-dir and --backend are required when compiling a request.")
        attempt_dir, request = compile_renderer_request_v18(
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
        )
        print(f"Prepared sanitized renderer request: {attempt_dir}")
        print(f"Reference manifest SHA-256: {request['ordered_reference_manifest_sha256']}")
        if args.prepare_only or args.backend == retained.BUILTIN_BACKEND:
            return 0
        verify_v18_attempt(attempt_dir)
        receipt = retained.execute_openai_request(
            attempt_dir,
            api_key=os.environ.get(args.api_key_env, ""),
            timeout_seconds=args.timeout_seconds,
            force=args.force,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    except (OSError, retained.RendererAdapterError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
