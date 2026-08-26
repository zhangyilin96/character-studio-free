"""Free 公开快速参考分析，不包含高级评分、阈值或付费 Prompt。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any


def _data_url(path: Path) -> str:
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.casefold(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def json_response(
    client: Any,
    model: str,
    prompt: str,
    images: list[Path],
    *,
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    content = [{"type": "input_text", "text": prompt}]
    content.extend({"type": "input_image", "image_url": _data_url(path), "detail": "high"} for path in images)
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": content}],
        reasoning={"effort": reasoning_effort},
        text={"format": {"type": "json_object"}},
        store=False,
    )
    value = json.loads(response.output_text)
    if not isinstance(value, dict):
        raise ValueError("Vision response was not a JSON object")
    return value


def analyze_references_quick(
    client: Any,
    model: str,
    character_path: Path,
    pose_path: Path,
    *,
    outfit_path: Path | None = None,
) -> dict[str, Any]:
    """提取单次 Free 生成和基础交付检查需要的可见事实。"""

    prompt = """Analyze two images for one fast character pose-transfer generation.
Image 1 is the only authority for character identity, face, hair, body proportion, outfit and final visual domain.
Image 2 is authority only for pose, crop, framing, camera, support and occlusion.
Return JSON with object keys character, geometry_evidence, preflight, pose_blueprint,
contact_graph and occlusion_graph.

Keep the response compact. Record only visible facts and keep uncertain facts unknown.
preflight must identify obvious complex risk using low/medium/high values for occlusion_risk,
left_right_ambiguity, support_contact_complexity, camera_extremity and hidden_region_risk.
flags must include strong_perspective, extreme_foreshortening, crossed_limbs,
hidden_lower_body and foreground_limb_enlargement as booleans.
pose_blueprint must use subject-left/right. contact_graph uses {contacts: []};
occlusion_graph uses {occlusion: []}. Do not calculate scores, repair plans or hidden anatomy.
Do not inherit identity, clothing, style, lighting, text, background or scene from Image 2."""
    images = [character_path, pose_path]
    if outfit_path is not None:
        prompt += """
A third image is garment-only authority for structure, material, color, decoration, layering and shoes.
Never copy its identity, face, hair, body, pose, background, scene, lighting or visual domain."""
        images.append(outfit_path)
    analysis = json_response(client, model, prompt, images, reasoning_effort="low")
    required = {"character", "geometry_evidence", "preflight", "pose_blueprint", "contact_graph", "occlusion_graph"}
    if not required.issubset(analysis) or not all(isinstance(analysis.get(key), dict) for key in required):
        raise ValueError("快速参考图分析结果不完整。")
    analysis["analysis_profile"] = "QUICK"
    return analysis
