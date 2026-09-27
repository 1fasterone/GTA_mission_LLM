"""Deterministic Stable Audio 3 batch export.

The Unity compiler writes the audio system and this small request file, but it
does not load a GPU audio model.  A later standalone command consumes the
request and fills the same ``Resources`` folder with clips and a manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .resolver import classify


def _plain_cue(raw: Any) -> dict[str, Any]:
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raw = vars(raw)
    return {
        "id": str(raw.get("id", "cue")),
        "category": str(raw.get("category", "sfx")),
        "kind": str(raw.get("kind", "auto")),
        "descriptor": str(raw.get("descriptor", "")),
        "archetype": str(raw.get("archetype", "")),
        "gen_prompt": str(raw.get("gen_prompt", "")),
        "text": str(raw.get("text", "")),
        "voice": str(raw.get("voice", "default")),
        "spatial": bool(raw.get("spatial", False)),
        "loop": bool(raw.get("loop", False)),
        "variations": int(raw.get("variations", 1) or 1),
        "duration_s": float(raw.get("duration_s", 0.5) or 0.5),
    }


def stable3_model_for(cue: dict[str, Any]) -> str:
    """Return the standalone model that will consume one cue."""
    route = classify(cue["category"], cue["kind"], cue["descriptor"])
    if route == "tts":
        return "tts"
    if cue["category"] in ("music", "ambient"):
        return "music"
    return "sfx"


def pending_manifest(cues: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    """Create a Unity-readable manifest before the standalone render exists."""
    return {
        "seed": int(seed),
        "backend": "stable3",
        "status": "pending_stable3_batch",
        "cue_count": len(cues),
        "cues": [
            {
                "id": cue["id"],
                "category": cue["category"],
                "spatial": cue["spatial"],
                "loop": cue["loop"],
                "volume": 1.0,
                "clips": [],
                "variations": 0,
            }
            for cue in cues
        ],
        "warnings": [
            "Stable Audio 3 rendering is deferred; run unity-audio-batch."
        ],
    }


def write_stable3_batch(
    cues: list[Any],
    batch_path: str | Path,
    *,
    seed: int = 0,
    project_name: str = "",
) -> Path:
    """Write prompts and pending Unity metadata without importing Stable Audio."""
    batch_path = Path(batch_path)
    batch_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = [_plain_cue(cue) for cue in cues]
    records = []
    for cue in normalized:
        record = dict(cue)
        record["prompt"] = cue["gen_prompt"] or cue["descriptor"]
        record["stable3_model"] = stable3_model_for(cue)
        records.append(record)

    payload = {
        "version": 1,
        "status": "pending",
        "backend": "stable3",
        "project_name": project_name,
        "seed": int(seed),
        "resources_dir": "Resources",
        "cues": records,
        "usage": (
            "Run `python main.py unity-audio-batch <path-to-this-file>` "
            "after the Unity project is generated."
        ),
    }
    batch_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    resources_dir = batch_path.parent / "Resources"
    resources_dir.mkdir(parents=True, exist_ok=True)
    (resources_dir / "audio_manifest.json").write_text(
        json.dumps(pending_manifest(normalized, seed), indent=2),
        encoding="utf-8",
    )
    return batch_path
