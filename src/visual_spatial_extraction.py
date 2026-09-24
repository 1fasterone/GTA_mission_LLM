"""Slice 6.1 image-space evidence extraction.

This is the only image-aware part of the first spatial-layout slice.  The
Pipeline LLM annotates the already-closed image board; it does not choose
Unity coordinates, assets, gameplay roles, or scene-graph edges.  The output
is a frozen visual-evidence sidecar consumed by later deterministic slices.

The distinction matters: a 2-D screenshot can support normalized composition
evidence, but it cannot honestly establish metres, collider placement, route
reachability, or a parent/child relationship in the Unity world.  Those are
owned by the concept/schema and deterministic layout passes after this file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from .unity_reference_image import encode_reference_image
from .visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
    sha256_file,
    validate_visual_inventory,
)


SPATIAL_FORMAT = "visual-reasoning-compiler/image-spatial-evidence/v1"
SPATIAL_MANIFEST_FORMAT = "visual-reasoning-compiler/image-spatial-extraction/v1"
SPATIAL_STAGE = "6.1_image_spatial_extraction"
SPATIAL_SEED = 731949
SPATIAL_TEMPERATURE = 0.2
SPATIAL_MAX_TOKENS = 5000

_LAYERS = frozenset({"foreground", "midground", "background", "unknown"})
_SURFACES = frozenset({
    "ground", "water", "sky", "air", "indoor", "unknown",
})
_FACING = frozenset({"left", "right", "forward", "backward", "unknown"})
_OCCLUSION = frozenset({"none", "partial", "heavy", "unknown"})
_COHESION = frozenset({"tight", "loose", "unknown"})
_RELATIONS = frozenset({
    "left_of", "right_of", "above", "below", "in_front_of", "behind",
    "near", "far", "aligned_with", "overlaps",
})
_OPPOSITES = {
    "left_of": "right_of",
    "right_of": "left_of",
    "above": "below",
    "below": "above",
    "in_front_of": "behind",
    "behind": "in_front_of",
    "near": "far",
    "far": "near",
}

_SYSTEM = """\
You are the image-space annotation stage of a deterministic game-content
compiler. You receive one board image and a closed list of visible labels.
Use pixels as the only evidence for position, scale, visual depth, surface,
orientation, relations, and clusters.

You are an annotator, not a game designer. Do not invent labels, gameplay,
route order, world coordinates, metres, assets, model IDs, Unity objects,
colliders, navigation, or semantic roles. The supplied labels are only a
whitelist for referring to visible elements. If depth or a surface is unclear,
use "unknown" and record a short uncertainty. Return JSON only.
"""


def build_spatial_prompt(labels: Sequence[str]) -> str:
    """Build the fixed Step-3 prompt for one closed visual label set."""
    if not labels:
        raise VisualReasoningCompilerError(
            "cannot build spatial prompt without closed image labels"
        )
    label_lines = "\n".join(f"- {label}" for label in labels)
    return f"""\
Inspect the attached board image and return exactly one JSON object with these
keys and no others:

{{
  "frame": {{
    "horizon_y": 0.0,
    "vanishing_x": 0.0
  }},
  "surface_regions": [
    {{"surface": "water", "bbox": {{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}}, "confidence": 0.0}}
  ],
  "anchors": [
    {{
      "label": "exact supplied label",
      "bbox": {{"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1}},
      "layer": "foreground",
      "surface": "water",
      "facing": "forward",
      "occlusion": "none",
      "confidence": 0.0
    }}
  ],
  "relations": [
    {{"source": "exact label", "target": "exact label", "relation": "near", "confidence": 0.0}}
  ],
  "clusters": [
    {{"members": ["exact label", "exact label"], "cohesion": "tight", "confidence": 0.0}}
  ],
  "uncertainties": ["short image-grounded uncertainty"]
}}

Rules:
- Emit exactly one anchor for every supplied label, including UI labels. Do
  not emit any other anchor. A UI label can use surface "unknown".
- Coordinates are normalized image coordinates with origin at the top-left.
  Every bbox must have x, y, w, h in [0, 1], positive w/h, and remain inside
  the image. A broad environment may use a broad box.
- horizon_y and vanishing_x are normalized nullable estimates. Use null when
  not visible or not meaningful.
- Use only these values: layer = foreground | midground | background | unknown;
  surface = ground | water | sky | air | indoor | unknown; facing = left |
  right | forward | backward | unknown; occlusion = none | partial | heavy |
  unknown; cohesion = tight | loose | unknown.
- Use only these relation names: left_of, right_of, above, below,
  in_front_of, behind, near, far, aligned_with, overlaps. Do not emit a
  contradictory pair for the same ordered source and target.
- A cluster contains two or more supplied labels and has no invented name.
  Do not use clusters to imply gameplay grouping.
- Use [] for an empty list. Confidence is a number from 0 through 1.

SUPPLIED VISIBLE LABELS:
{label_lines}
"""


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise VisualReasoningCompilerError(f"{label} not found: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualReasoningCompilerError(
            f"{label} is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must contain a JSON object")
    return value, raw


def _extract_json_object(raw: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        raise VisualReasoningCompilerError(
            "image spatial response contained no JSON object"
        )
    try:
        value, end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise VisualReasoningCompilerError(
            f"image spatial response is not valid JSON: {exc}"
        ) from exc
    if text[start + end:].strip():
        raise VisualReasoningCompilerError(
            "image spatial response contains text outside the JSON object"
        )
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(
            "image spatial response must be a JSON object"
        )
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing={missing}")
        if extra:
            detail.append(f"extra={extra}")
        raise VisualReasoningCompilerError(
            f"{label} has the wrong fields ({', '.join(detail)})"
        )


def _string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VisualReasoningCompilerError(f"{label} must be a non-empty string")
    return value.strip()


def _number(value: Any, *, label: str, lo: float = 0.0,
            hi: float = 1.0) -> float:
    if isinstance(value, bool):
        raise VisualReasoningCompilerError(f"{label} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise VisualReasoningCompilerError(f"{label} must be a number") from exc
    if result < lo or result > hi:
        raise VisualReasoningCompilerError(
            f"{label} must be between {lo:g} and {hi:g}"
        )
    return round(result, 6)


def _optional_number(value: Any, *, label: str) -> float | None:
    if value is None:
        return None
    return _number(value, label=label)


def _bbox(value: Any, *, label: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must be an object")
    _exact_keys(value, {"x", "y", "w", "h"}, label=label)
    x = _number(value["x"], label=f"{label}.x")
    y = _number(value["y"], label=f"{label}.y")
    w = _number(value["w"], label=f"{label}.w", lo=0.000001)
    h = _number(value["h"], label=f"{label}.h", lo=0.000001)
    if x + w > 1.000001 or y + h > 1.000001:
        raise VisualReasoningCompilerError(
            f"{label} must remain inside normalized image bounds"
        )
    return {"x": x, "y": y, "w": w, "h": h}


def _confidence(value: Any, *, label: str) -> float:
    return _number(value, label=f"{label}.confidence")


def _string_list(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64:
        raise VisualReasoningCompilerError(f"{label} must be a list of at most 64 strings")
    return [_string(item, label=f"{label}[{index}]")
            for index, item in enumerate(value)]


def _closed_labels(closed_ir: dict[str, Any]) -> list[str]:
    if closed_ir.get("format") != "visual-reasoning-compiler/closed-image-ir/v1":
        raise VisualReasoningCompilerError(
            "Slice 6.1 requires a closed-image-ir/v1 input"
        )
    if closed_ir.get("status") != "closed":
        raise VisualReasoningCompilerError("Slice 6.1 requires a closed image IR")
    try:
        inventory = validate_visual_inventory(closed_ir["inventory"])
    except (KeyError, TypeError) as exc:
        raise VisualReasoningCompilerError(
            "closed image IR has no valid visual inventory"
        ) from exc
    labels = [str(element["label"]) for element in inventory["visible_elements"]]
    if len(labels) != len(set(labels)):
        raise VisualReasoningCompilerError(
            "closed image IR visual inventory contains duplicate labels"
        )
    return labels


def validate_image_spatial_response(
    raw: str | dict[str, Any], labels: Sequence[str]
) -> dict[str, Any]:
    """Validate and canonically order one model-produced image annotation."""
    payload = _extract_json_object(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        raise VisualReasoningCompilerError("image spatial response must be an object")
    if payload.get("format") == SPATIAL_FORMAT:
        payload = {key: value for key, value in payload.items() if key != "format"}
    elif "format" in payload:
        raise VisualReasoningCompilerError(
            f"image spatial response format must be {SPATIAL_FORMAT!r}"
        )
    _exact_keys(
        payload,
        {"frame", "surface_regions", "anchors", "relations", "clusters", "uncertainties"},
        label="image spatial response",
    )
    canonical_labels = list(labels)
    label_set = set(canonical_labels)
    if len(canonical_labels) != len(label_set):
        raise VisualReasoningCompilerError("spatial label whitelist contains duplicates")

    frame = payload["frame"]
    if not isinstance(frame, dict):
        raise VisualReasoningCompilerError("frame must be an object")
    _exact_keys(frame, {"horizon_y", "vanishing_x"}, label="frame")
    frame_out = {
        "horizon_y": _optional_number(frame["horizon_y"], label="frame.horizon_y"),
        "vanishing_x": _optional_number(frame["vanishing_x"], label="frame.vanishing_x"),
    }

    regions_raw = payload["surface_regions"]
    if not isinstance(regions_raw, list) or len(regions_raw) > 16:
        raise VisualReasoningCompilerError(
            "surface_regions must be a list of at most 16 entries"
        )
    regions: list[dict[str, Any]] = []
    seen_surfaces: set[str] = set()
    for index, item in enumerate(regions_raw):
        if not isinstance(item, dict):
            raise VisualReasoningCompilerError(
                f"surface_regions[{index}] must be an object"
            )
        _exact_keys(item, {"surface", "bbox", "confidence"},
                    label=f"surface_regions[{index}]")
        surface = _string(item["surface"], label=f"surface_regions[{index}].surface")
        if surface not in _SURFACES:
            raise VisualReasoningCompilerError(
                f"surface_regions[{index}].surface {surface!r} is unsupported"
            )
        if surface in seen_surfaces:
            raise VisualReasoningCompilerError(
                f"surface_regions repeats surface {surface!r}"
            )
        seen_surfaces.add(surface)
        regions.append({
            "surface": surface,
            "bbox": _bbox(item["bbox"], label=f"surface_regions[{index}].bbox"),
            "confidence": _confidence(
                item["confidence"], label=f"surface_regions[{index}]"
            ),
        })

    anchors_raw = payload["anchors"]
    if not isinstance(anchors_raw, list) or len(anchors_raw) != len(label_set):
        raise VisualReasoningCompilerError(
            "anchors must contain exactly one entry for every closed image label"
        )
    anchors: list[dict[str, Any]] = []
    seen_anchor_labels: set[str] = set()
    for index, item in enumerate(anchors_raw):
        if not isinstance(item, dict):
            raise VisualReasoningCompilerError(f"anchors[{index}] must be an object")
        _exact_keys(
            item,
            {"label", "bbox", "layer", "surface", "facing", "occlusion", "confidence"},
            label=f"anchors[{index}]",
        )
        label = _string(item["label"], label=f"anchors[{index}].label")
        if label not in label_set:
            raise VisualReasoningCompilerError(
                f"anchor refers to unknown closed image label {label!r}"
            )
        if label in seen_anchor_labels:
            raise VisualReasoningCompilerError(
                f"spatial response repeats anchor label {label!r}"
            )
        seen_anchor_labels.add(label)
        layer = _string(item["layer"], label=f"anchors[{index}].layer")
        surface = _string(item["surface"], label=f"anchors[{index}].surface")
        facing = _string(item["facing"], label=f"anchors[{index}].facing")
        occlusion = _string(item["occlusion"], label=f"anchors[{index}].occlusion")
        if layer not in _LAYERS or surface not in _SURFACES:
            raise VisualSpatialExtractionError(
                f"anchor {label!r} has an unsupported layer or surface"
            )
        if facing not in _FACING or occlusion not in _OCCLUSION:
            raise VisualSpatialExtractionError(
                f"anchor {label!r} has an unsupported facing or occlusion"
            )
        anchors.append({
            "label": label,
            "bbox": _bbox(item["bbox"], label=f"anchors[{index}].bbox"),
            "layer": layer,
            "surface": surface,
            "facing": facing,
            "occlusion": occlusion,
            "confidence": _confidence(
                item["confidence"], label=f"anchors[{index}]"
            ),
        })
    if seen_anchor_labels != label_set:
        missing = sorted(label_set - seen_anchor_labels)
        raise VisualReasoningCompilerError(
            f"spatial response omitted closed image labels: {missing}"
        )

    relations_raw = payload["relations"]
    if not isinstance(relations_raw, list) or len(relations_raw) > 256:
        raise VisualReasoningCompilerError(
            "relations must be a list of at most 256 entries"
        )
    relations: list[dict[str, Any]] = []
    relation_keys: set[tuple[str, str, str]] = set()
    for index, item in enumerate(relations_raw):
        if not isinstance(item, dict):
            raise VisualReasoningCompilerError(f"relations[{index}] must be an object")
        _exact_keys(item, {"source", "target", "relation", "confidence"},
                    label=f"relations[{index}]")
        source = _string(item["source"], label=f"relations[{index}].source")
        target = _string(item["target"], label=f"relations[{index}].target")
        relation = _string(item["relation"], label=f"relations[{index}].relation")
        if source not in label_set or target not in label_set:
            raise VisualReasoningCompilerError(
                f"relations[{index}] refers to a label outside the closed image IR"
            )
        if source == target:
            raise VisualReasoningCompilerError(
                f"relations[{index}] cannot relate a label to itself"
            )
        if relation not in _RELATIONS:
            raise VisualReasoningCompilerError(
                f"relations[{index}].relation {relation!r} is unsupported"
            )
        key = (source, target, relation)
        if key in relation_keys:
            raise VisualReasoningCompilerError(
                f"spatial response repeats relation {key!r}"
            )
        opposite = _OPPOSITES.get(relation)
        if opposite and (source, target, opposite) in relation_keys:
            raise VisualReasoningCompilerError(
                f"spatial response contradicts itself for {source!r} and {target!r}"
            )
        relation_keys.add(key)
        relations.append({
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": _confidence(
                item["confidence"], label=f"relations[{index}]"
            ),
        })

    clusters_raw = payload["clusters"]
    if not isinstance(clusters_raw, list) or len(clusters_raw) > 64:
        raise VisualReasoningCompilerError(
            "clusters must be a list of at most 64 entries"
        )
    clusters: list[dict[str, Any]] = []
    cluster_keys: set[tuple[str, ...]] = set()
    for index, item in enumerate(clusters_raw):
        if not isinstance(item, dict):
            raise VisualReasoningCompilerError(f"clusters[{index}] must be an object")
        _exact_keys(item, {"members", "cohesion", "confidence"},
                    label=f"clusters[{index}]")
        members_raw = item["members"]
        if not isinstance(members_raw, list) or len(members_raw) < 2:
            raise VisualReasoningCompilerError(
                f"clusters[{index}].members must contain at least two labels"
            )
        members = [_string(member, label=f"clusters[{index}].members[{n}]")
                   for n, member in enumerate(members_raw)]
        if len(members) != len(set(members)) or any(member not in label_set for member in members):
            raise VisualReasoningCompilerError(
                f"clusters[{index}].members must be unique closed image labels"
            )
        members.sort()
        cluster_key = tuple(members)
        if cluster_key in cluster_keys:
            raise VisualReasoningCompilerError(
                f"spatial response repeats cluster {members!r}"
            )
        cluster_keys.add(cluster_key)
        cohesion = _string(item["cohesion"], label=f"clusters[{index}].cohesion")
        if cohesion not in _COHESION:
            raise VisualReasoningCompilerError(
                f"clusters[{index}].cohesion {cohesion!r} is unsupported"
            )
        clusters.append({
            "members": members,
            "cohesion": cohesion,
            "confidence": _confidence(
                item["confidence"], label=f"clusters[{index}]"
            ),
        })

    return {
        "format": SPATIAL_FORMAT,
        "frame": frame_out,
        "surface_regions": sorted(regions, key=lambda item: item["surface"]),
        "anchors": sorted(anchors, key=lambda item: item["label"]),
        "relations": sorted(
            relations,
            key=lambda item: (item["source"], item["target"], item["relation"]),
        ),
        "clusters": sorted(clusters, key=lambda item: tuple(item["members"])),
        "uncertainties": sorted(_string_list(payload["uncertainties"], label="uncertainties")),
    }


class VisualSpatialExtractionError(VisualReasoningCompilerError):
    """Raised when the Slice 6.1 image-evidence contract cannot be met."""


def _closed_image_input(
    closed_ir: Path, board_image: Path
) -> tuple[dict[str, Any], bytes, list[str], str]:
    closed, raw = _read_json(closed_ir, label="closed image IR")
    labels = _closed_labels(closed)
    board_record = closed.get("board")
    if not isinstance(board_record, dict) or not board_record.get("sha256"):
        raise VisualSpatialExtractionError("closed image IR has no board hash")
    board_hash = sha256_file(board_image)
    if board_hash != board_record["sha256"]:
        raise VisualSpatialExtractionError(
            "board image hash does not match the closed image IR"
        )
    return closed, raw, labels, board_hash


def extract_image_spatial_evidence(
    *,
    board_image: Path,
    closed_ir: Path,
    output: Path,
    manifest: Path,
    llm: Any,
) -> Path:
    """Run one constrained vision call and freeze its validated evidence."""
    paths = (output, manifest, output.with_suffix(".raw.txt"))
    existing = [path for path in paths if path.exists()]
    if existing:
        raise VisualSpatialExtractionError(
            "refusing to overwrite Slice-6.1 artifact(s): "
            + ", ".join(str(path) for path in existing)
        )
    if not board_image.is_file():
        raise VisualSpatialExtractionError(f"board image not found: {board_image}")
    closed, closed_raw, labels, board_hash = _closed_image_input(closed_ir, board_image)
    prompt = build_spatial_prompt(labels)
    raw_response = llm.chat(
        system=_SYSTEM,
        user=prompt,
        images=[encode_reference_image(board_image)],
        temperature=SPATIAL_TEMPERATURE,
        thinking=False,
        seed=SPATIAL_SEED,
        max_tokens=SPATIAL_MAX_TOKENS,
        json_mode=True,
    )
    evidence = validate_image_spatial_response(raw_response, labels)
    evidence_bytes = (
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    raw_bytes = str(raw_response).encode("utf-8")
    model = str(getattr(llm, "model", "unknown"))
    request = {
        "board_sha256": board_hash,
        "closed_image_ir_sha256": sha256_bytes(closed_raw),
        "labels": labels,
        "model": model,
        "system": _SYSTEM,
        "user": prompt,
        "temperature": SPATIAL_TEMPERATURE,
        "thinking": False,
        "seed": SPATIAL_SEED,
        "max_tokens": SPATIAL_MAX_TOKENS,
        "json_mode": True,
    }
    manifest_data = {
        "format": SPATIAL_MANIFEST_FORMAT,
        "stage": SPATIAL_STAGE,
        "status": "complete",
        "authority": {
            "llm_called_here": True,
            "image_spatial_authority": "board_pixels",
            "semantic_label_source": "validated_closed_image_ir",
            "concept_semantics_used_here": False,
            "frozen_asset_bindings_used_here": False,
            "schema_roles_used_here": False,
            "world_coordinates_emitted_here": False,
        },
        "inputs": {
            "board": {"path": str(board_image), "sha256": board_hash},
            "closed_image_ir": {
                "path": str(closed_ir),
                "sha256": sha256_bytes(closed_raw),
                "request_fingerprint": closed.get("request_fingerprint", ""),
            },
        },
        "request": request,
        "outputs": {
            "image_spatial_evidence": {
                "path": str(output),
                "sha256": sha256_bytes(evidence_bytes),
            },
            "raw_response": {
                "path": str(output.with_suffix(".raw.txt")),
                "sha256": sha256_bytes(raw_bytes),
            },
        },
        "warnings": [
            "Image-space evidence is not a Unity world layout and must not be "
            "used as gameplay or semantic authority.",
            "Later slices must fuse this frozen sidecar with schema roles and "
            "measured asset footprints deterministically.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(evidence_bytes)
    output.with_suffix(".raw.txt").write_bytes(raw_bytes)
    manifest.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = [
    "SPATIAL_FORMAT",
    "SPATIAL_MANIFEST_FORMAT",
    "SPATIAL_MAX_TOKENS",
    "SPATIAL_SEED",
    "SPATIAL_STAGE",
    "SPATIAL_TEMPERATURE",
    "VisualSpatialExtractionError",
    "build_spatial_prompt",
    "extract_image_spatial_evidence",
    "validate_image_spatial_response",
]
