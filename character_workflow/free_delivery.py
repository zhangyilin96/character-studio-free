"""Free 的来源、domain 和严格交付门；不包含高级评分或修复。"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from reference_role_isolation import evaluate_character_rendering_domain  # noqa: E402
from renderer_adapter import verify_renderer_receipt  # noqa: E402


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"期望 JSON 对象：{path.name}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def finalize_free_candidate(
    *,
    provider: Any,
    run_dir: Path,
    candidate: Path,
    observation_path: Path,
    receipt_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = run_dir.resolve()
    candidate = candidate.resolve()
    if run_dir not in candidate.parents or not candidate.is_file():
        raise RuntimeError("Free 候选图不在当前任务目录。")
    receipt = verify_renderer_receipt(
        receipt_path,
        candidate_path=candidate,
        current_prompt_path=run_dir / "final_prompt.md",
        allow_synthetic=provider.allows_synthetic_receipt,
    )
    observation = _read(observation_path)
    quick = observation.get("quick_check", {}) if isinstance(observation.get("quick_check"), dict) else {}
    isolation = _read(run_dir / "reference_role_isolation.json")
    domain_gate = evaluate_character_rendering_domain(isolation, observation, strict_reference_replica=True)
    eligible = quick.get("status") == "PASS" and domain_gate.get("status") == "PASS"
    finalized = {
        "route": "FREE_FAST",
        "receipt": receipt,
        "quick_check": quick,
        "domain_gate": domain_gate,
        "eligible": eligible,
        "repair_executed": False,
        "retry_executed": False,
    }
    _write(run_dir / "free-finalization.json", finalized)
    if not eligible:
        delivery = {
            "delivery_scope": "NO_ELIGIBLE_CANDIDATE",
            "route": "FREE_FAST",
            "candidate_returned": False,
        }
        _write(run_dir / "strict-delivery.json", delivery)
        return finalized, delivery
    final_path = run_dir / "final.png"
    shutil.copy2(candidate, final_path)
    delivery = {
        "delivery_scope": "STRICT_REFERENCE_REPLICA",
        "route": "FREE_FAST",
        "candidate_returned": True,
        "final_path": final_path.name,
    }
    _write(run_dir / "strict-delivery.json", delivery)
    return finalized, delivery
