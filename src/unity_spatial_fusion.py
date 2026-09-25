"""Deterministic Slice 6.3 fusion of image evidence and schema roles.

Slice 6.1 owns closed image-space observations and Slice 6.2 owns declared
schema roles and gameplay adjacency.  This module joins those artifacts
without adding entities, changing the schema, or emitting world coordinates.
Visual composition constraints remain normalized to the image frame; schema
roles and adjacency remain separate authoritative constraints for later
realization.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional

from .unity_schema import UnitySchema
from .unity_spatial_roles import SPATIAL_ROLES, SPATIAL_ROLES_FORMAT
from .unity_spatial_identity import semantic_schema_hash
from .visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
    validate_visual_inventory,
)
from .visual_spatial_extraction import (
    SPATIAL_FORMAT,
    validate_image_spatial_response,
)


SPATIAL_FUSION_FORMAT = "visual-reasoning-compiler/spatial-fusion/v1"
SPATIAL_FUSION_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/spatial-fusion-manifest/v1"
)
SPATIAL_FUSION_STAGE = "6.3_hybrid_evidence_fusion"

_GENERIC_TERMS = frozenset({
    "a", "an", "and", "asset", "character", "element", "entity", "game",
    "group", "object", "player", "prop", "scene", "thing", "vehicle",
    "world",
})


class SpatialFusionError(ValueError):
    """Raised when the 6.3 inputs cannot be joined without guessing."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _schema_hash(schema: UnitySchema) -> str:
    return semantic_schema_hash(schema)


def _terms(value: Any) -> set[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
    folded = unicodedata.normalize("NFKC", text).casefold()
    return {
        token for token in re.findall(r"[a-z0-9]+", folded)
        if len(token) > 1 and token not in _GENERIC_TERMS
    }


def _entity_terms(entity: Any) -> set[str]:
    return (
        _terms(getattr(entity, "name", ""))
        | _terms(getattr(entity, "model_hint", ""))
        | _terms(getattr(entity, "sketchfab_query", ""))
        | _terms(getattr(entity, "description", ""))
    )


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise SpatialFusionError(f"{label} not found: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpatialFusionError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SpatialFusionError(f"{label} must contain a JSON object: {path}")
    return payload, raw


def _require_format(payload: Mapping[str, Any], expected: str, *, label: str) -> None:
    if payload.get("format") != expected:
        raise SpatialFusionError(
            f"{label} format must be {expected!r}"
        )


def _validate_closed_ir(closed_ir: Mapping[str, Any]) -> dict[str, Any]:
    _require_format(
        closed_ir,
        "visual-reasoning-compiler/closed-image-ir/v1",
        label="closed image IR",
    )
    if closed_ir.get("status") != "closed":
        raise SpatialFusionError("6.3 requires a closed Slice-3 image IR")
    inventory = closed_ir.get("inventory")
    if not isinstance(inventory, Mapping):
        raise SpatialFusionError("closed image IR has no visual inventory")
    try:
        validated_inventory = validate_visual_inventory(dict(inventory))
    except (KeyError, TypeError, ValueError) as exc:
        raise SpatialFusionError(
            f"closed image IR inventory is invalid: {exc}"
        ) from exc

    matches = closed_ir.get("entity_matches")
    if not isinstance(matches, list):
        raise SpatialFusionError("closed image IR has no entity_matches list")
    labels = {
        str(element["label"])
        for element in validated_inventory["visible_elements"]
    }
    by_label: dict[str, str] = {}
    references: set[str] = set()
    for index, item in enumerate(matches):
        if not isinstance(item, Mapping):
            raise SpatialFusionError(
                f"closed image IR entity_matches[{index}] must be an object"
            )
        reference = str(item.get("reference") or "").strip()
        label = str(item.get("inventory_label") or "").strip()
        if not reference or not label:
            raise SpatialFusionError(
                f"closed image IR entity_matches[{index}] needs reference and inventory_label"
            )
        if label not in labels:
            raise SpatialFusionError(
                f"closed image IR entity match names undeclared inventory label {label!r}"
            )
        if label in by_label or reference in references:
            raise SpatialFusionError(
                "closed image IR entity_matches contains duplicate references"
            )
        by_label[label] = reference
        references.add(reference)

    required = ((closed_ir.get("coverage") or {}).get("required_inventory_labels")
                or [])
    missing_required = sorted(set(map(str, required)) - set(by_label))
    if missing_required:
        raise SpatialFusionError(
            "closed image IR omits required entity matches: "
            + ", ".join(missing_required)
        )
    return {
        "inventory": validated_inventory,
        "entity_matches": by_label,
        "asset_requests": closed_ir.get("asset_requests") or [],
        "request_fingerprint": closed_ir.get("request_fingerprint"),
        "coverage": dict(closed_ir.get("coverage") or {}),
    }


def _validate_roles(
    schema: UnitySchema,
    roles: Mapping[str, Any],
) -> dict[str, Any]:
    _require_format(roles, SPATIAL_ROLES_FORMAT, label="schema spatial roles")
    if roles.get("status") != "closed":
        raise SpatialFusionError("6.3 requires a closed Slice-6.2 role sidecar")
    expected_hash = _schema_hash(schema)
    actual_hash = ((roles.get("inputs") or {}).get("schema_sha256"))
    if actual_hash != expected_hash:
        raise SpatialFusionError(
            "schema spatial roles belong to a different schema; regenerate Slice 6.2"
        )
    table = roles.get("role_table")
    if not isinstance(table, list):
        raise SpatialFusionError("schema spatial roles have no role_table")
    declared = [str(entity.name) for entity in schema.entities]
    rows: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(table):
        if not isinstance(row, Mapping):
            raise SpatialFusionError(f"role_table[{index}] must be an object")
        name = str(row.get("entity") or "")
        role = str(row.get("role") or "")
        if name not in declared:
            raise SpatialFusionError(
                f"schema spatial roles name undeclared entity {name!r}"
            )
        if name in rows:
            raise SpatialFusionError(
                f"schema spatial roles repeat entity {name!r}"
            )
        if role not in SPATIAL_ROLES:
            raise SpatialFusionError(
                f"schema spatial roles use unsupported role {role!r}"
            )
        rows[name] = dict(row)
    if set(rows) != set(declared):
        missing = sorted(set(declared) - set(rows))
        raise SpatialFusionError(
            "schema spatial roles do not cover declared entities: "
            + ", ".join(missing)
        )
    adjacency = roles.get("adjacency_requirements") or []
    if not isinstance(adjacency, list):
        raise SpatialFusionError("schema spatial roles adjacency_requirements must be a list")
    adjacency_by_entity: dict[str, list[dict[str, Any]]] = {name: [] for name in declared}
    for index, requirement in enumerate(adjacency):
        if not isinstance(requirement, Mapping):
            raise SpatialFusionError(
                f"adjacency_requirements[{index}] must be an object"
            )
        entity = str(requirement.get("entity") or "")
        if entity not in adjacency_by_entity:
            raise SpatialFusionError(
                f"adjacency requirement names undeclared entity {entity!r}"
            )
        adjacency_by_entity[entity].append(dict(requirement))
    return {"rows": rows, "adjacency": adjacency_by_entity, "schema_hash": expected_hash}


def _anchor_constraints(anchor: Mapping[str, Any]) -> dict[str, Any]:
    bbox = anchor["bbox"]
    x = float(bbox["x"])
    y = float(bbox["y"])
    width = float(bbox["w"])
    height = float(bbox["h"])
    return {
        "bbox": {key: round(float(bbox[key]), 6) for key in ("x", "y", "w", "h")},
        "center_xy": [round(x + width / 2.0, 6), round(y + height / 2.0, 6)],
        "size_wh": [round(width, 6), round(height, 6)],
        "area_ratio": round(width * height, 6),
        "aspect_ratio": round(width / height, 6) if height else None,
        "layer": str(anchor["layer"]),
        "surface": str(anchor["surface"]),
        "facing": str(anchor["facing"]),
        "occlusion": str(anchor["occlusion"]),
        "confidence": round(float(anchor["confidence"]), 6),
    }


def _surface_constraints(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for region in evidence.get("surface_regions") or []:
        bbox = region["bbox"]
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox["w"])
        height = float(bbox["h"])
        rows.append({
            "surface": str(region["surface"]),
            "bbox": {key: round(float(bbox[key]), 6) for key in ("x", "y", "w", "h")},
            "center_xy": [round(x + width / 2.0, 6), round(y + height / 2.0, 6)],
            "size_wh": [round(width, 6), round(height, 6)],
            "confidence": round(float(region["confidence"]), 6),
        })
    return rows


def _entity_candidates(
    schema: UnitySchema,
    *,
    reference: str,
    label: str,
    element: Mapping[str, Any],
    query: str,
) -> list[tuple[int, str]]:
    target = (
        _terms(reference)
        | _terms(label)
        | _terms(element.get("model_hint"))
        | _terms(element.get("description"))
        | _terms(query)
    )
    candidates: list[tuple[int, str]] = []
    for entity in schema.entities:
        name = str(entity.name)
        name_terms = _terms(name)
        hint_terms = _terms(getattr(entity, "model_hint", "")) | _terms(
            getattr(entity, "sketchfab_query", "")
        )
        overlap = target & _entity_terms(entity)
        if not overlap:
            continue
        exact = int(
            bool(name_terms)
            and (name_terms == _terms(reference) or name_terms == _terms(label))
        )
        name_overlap = len(name_terms & target)
        hint_overlap = len(hint_terms & target)
        score = exact * 100 + name_overlap * 4 + hint_overlap * 3 + len(overlap)
        candidates.append((score, name))
    return sorted(candidates, key=lambda item: (-item[0], item[1]))


def _query_by_label(closed: Mapping[str, Any]) -> dict[str, str]:
    output: dict[str, str] = {}
    for item in closed.get("asset_requests") or []:
        if isinstance(item, Mapping) and item.get("inventory_label"):
            output[str(item["inventory_label"])] = str(item.get("query") or "")
    return output


def fuse_spatial_evidence(
    schema: UnitySchema,
    *,
    closed_ir: Mapping[str, Any],
    image_evidence: Mapping[str, Any],
    spatial_roles: Mapping[str, Any],
) -> dict[str, Any]:
    """Join frozen image evidence to frozen schema roles without coordinates."""
    closed = _validate_closed_ir(closed_ir)
    _require_format(image_evidence, SPATIAL_FORMAT, label="image spatial evidence")
    labels = [str(item["label"]) for item in closed["inventory"]["visible_elements"]]
    try:
        evidence = validate_image_spatial_response(dict(image_evidence), labels)
    except (VisualReasoningCompilerError, TypeError, ValueError) as exc:
        raise SpatialFusionError(
            f"image spatial evidence failed validation: {exc}"
        ) from exc
    roles = _validate_roles(schema, spatial_roles)
    elements = {
        str(item["label"]): item
        for item in closed["inventory"]["visible_elements"]
    }
    anchors = {str(item["label"]): item for item in evidence["anchors"]}
    query_by_label = _query_by_label(closed)
    proposals: dict[str, list[tuple[int, str]]] = {}
    abstentions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for label, reference in sorted(closed["entity_matches"].items()):
        candidates = _entity_candidates(
            schema,
            reference=reference,
            label=label,
            element=elements[label],
            query=query_by_label.get(label, ""),
        )
        if not candidates:
            abstentions.append({
                "inventory_label": label,
                "concept_reference": reference,
                "status": "abstain",
                "reason": "no unique declared schema entity matched the closed visual binding",
            })
            continue
        best_score = candidates[0][0]
        tied = [name for score, name in candidates if score == best_score]
        if len(tied) != 1:
            abstentions.append({
                "inventory_label": label,
                "concept_reference": reference,
                "status": "abstain",
                "reason": "ambiguous declared schema entity match",
                "candidates": tied[:8],
            })
            continue
        proposals[label] = candidates

    selected: dict[str, str] = {}
    for label, candidates in proposals.items():
        selected[label] = candidates[0][1]
    reverse: dict[str, list[str]] = {}
    for label, entity in selected.items():
        reverse.setdefault(entity, []).append(label)
    for entity, labels_for_entity in sorted(reverse.items()):
        if len(labels_for_entity) <= 1:
            continue
        for label in labels_for_entity:
            selected.pop(label, None)
            abstentions.append({
                "inventory_label": label,
                "concept_reference": closed["entity_matches"][label],
                "status": "abstain",
                "reason": "multiple visual references resolved to one schema entity",
                "candidates": sorted(labels_for_entity),
            })

    required_labels = set(map(str, closed["coverage"].get("required_inventory_labels") or []))
    for abstention in abstentions:
        label = str(abstention["inventory_label"])
        prefix = "required" if label in required_labels else "optional"
        warnings.append(
            f"[spatial.fusion] {prefix} visual reference {label!r} abstained: "
            f"{abstention['reason']}"
        )

    bindings: list[dict[str, Any]] = []
    for label, anchor in sorted(anchors.items()):
        reference = closed["entity_matches"].get(label)
        if reference is None:
            bindings.append({
                "inventory_label": label,
                "status": "unbound_optional",
                "entity": None,
                "visual_constraints": _anchor_constraints(anchor),
                "schema_constraints": None,
                "authority": "no declared Concept/schema binding; visual evidence retained as warning",
            })
            warnings.append(
                f"[spatial.fusion] visual anchor {label!r} has no closed entity "
                "binding; it cannot create a schema entity"
            )
            continue
        entity_name = selected.get(label)
        if entity_name is None:
            abstention = next(
                row for row in abstentions
                if row["inventory_label"] == label
            )
            bindings.append({
                "inventory_label": label,
                "concept_reference": reference,
                "status": "abstain",
                "entity": None,
                "visual_constraints": _anchor_constraints(anchor),
                "schema_constraints": None,
                "authority": "ambiguous visual/schema binding is not realized",
                "abstention": abstention,
            })
            continue
        role_row = roles["rows"][entity_name]
        bindings.append({
            "inventory_label": label,
            "concept_reference": reference,
            "status": "bound",
            "entity": entity_name,
            "visual_constraints": _anchor_constraints(anchor),
            "schema_constraints": {
                "role": role_row["role"],
                "adjacency_requirements": roles["adjacency"].get(entity_name, []),
            },
            "authority": (
                "schema role and gameplay adjacency remain authoritative; "
                "visual values constrain normalized composition only"
            ),
        })

    relation_constraints: list[dict[str, Any]] = []
    for relation in evidence["relations"]:
        source = selected.get(str(relation["source"]))
        target = selected.get(str(relation["target"]))
        if source is None or target is None:
            relation_constraints.append({
                "source_inventory_label": relation["source"],
                "target_inventory_label": relation["target"],
                "relation": relation["relation"],
                "confidence": relation["confidence"],
                "status": "abstain",
                "reason": "relation endpoint has no unique declared schema binding",
            })
            continue
        relation_constraints.append({
            "source_entity": source,
            "target_entity": target,
            "relation": relation["relation"],
            "confidence": relation["confidence"],
            "status": "bound",
            "authority": "visual composition constraint; not gameplay semantics",
        })

    frame = dict(evidence["frame"])
    defaults_applied: list[str] = []
    if frame["horizon_y"] is None:
        frame["horizon_y"] = 0.5
        defaults_applied.append("frame.horizon_y=0.5 (centerline default)")
    if frame["vanishing_x"] is None:
        frame["vanishing_x"] = 0.5
        defaults_applied.append("frame.vanishing_x=0.5 (centerline default)")
    warnings.extend(
        f"[spatial.fusion] named default applied: {item}"
        for item in defaults_applied
    )
    status = "abstain" if any(
        row["inventory_label"] in required_labels for row in abstentions
    ) else "complete"
    if any(row["status"] == "abstain" for row in relation_constraints):
        warnings.append(
            "[spatial.fusion] one or more visual relations remain unbound and "
            "are not promoted to gameplay edges"
        )
    return {
        "format": SPATIAL_FUSION_FORMAT,
        "stage": SPATIAL_FUSION_STAGE,
        "status": status,
        "authority": {
            "llm_called_here": False,
            "image_composition_authority": True,
            "schema_semantics_authority": True,
            "schema_roles_used_here": True,
            "closed_visual_bindings_used_here": True,
            "entities_invented_here": False,
            "world_coordinates_emitted_here": False,
        },
        "inputs": {
            "schema_sha256": roles["schema_hash"],
            "closed_request_fingerprint": closed["request_fingerprint"],
            "visual_labels": labels,
        },
        "frame": {
            "coordinate_space": "normalized_image_0_1",
            "horizon_y": round(float(frame["horizon_y"]), 6),
            "vanishing_x": round(float(frame["vanishing_x"]), 6),
        },
        "surface_regions": _surface_constraints(evidence),
        "bindings": bindings,
        "relations": relation_constraints,
        "abstentions": sorted(
            abstentions, key=lambda row: str(row["inventory_label"])
        ),
        "defaults_applied": defaults_applied,
        "warnings": sorted(set(warnings)),
    }


def write_spatial_fusion(
    schema_path: Path,
    closed_ir_path: Path,
    image_evidence_path: Path,
    spatial_roles_path: Path,
    output: Path,
    manifest: Optional[Path] = None,
) -> Path:
    """Read frozen Slice-3/6.1/6.2 inputs and write the 6.3 fusion artifact."""
    schema_raw = schema_path.read_bytes() if schema_path.is_file() else None
    if schema_raw is None:
        raise SpatialFusionError(f"schema not found: {schema_path}")
    try:
        schema = UnitySchema.model_validate(json.loads(schema_raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SpatialFusionError(f"schema is not valid UnitySchema JSON: {schema_path}") from exc
    closed, closed_raw = _read_json(closed_ir_path, label="closed image IR")
    evidence, evidence_raw = _read_json(
        image_evidence_path, label="image spatial evidence"
    )
    roles, roles_raw = _read_json(spatial_roles_path, label="schema spatial roles")
    fused = fuse_spatial_evidence(
        schema,
        closed_ir=closed,
        image_evidence=evidence,
        spatial_roles=roles,
    )
    output_bytes = (json.dumps(fused, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path = manifest or output.with_name(output.stem + ".manifest.json")
    manifest_data = {
        "format": SPATIAL_FUSION_MANIFEST_FORMAT,
        "stage": SPATIAL_FUSION_STAGE,
        "status": fused["status"],
        "authority": fused["authority"],
        "inputs": {
            "schema": {"path": str(schema_path), "sha256": sha256_bytes(schema_raw)},
            "closed_image_ir": {
                "path": str(closed_ir_path), "sha256": sha256_bytes(closed_raw)
            },
            "image_spatial_evidence": {
                "path": str(image_evidence_path), "sha256": sha256_bytes(evidence_raw)
            },
            "schema_spatial_roles": {
                "path": str(spatial_roles_path), "sha256": sha256_bytes(roles_raw)
            },
        },
        "outputs": {
            "spatial_fusion": {
                "path": str(output), "sha256": sha256_bytes(output_bytes)
            }
        },
        "binding_count": sum(
            1 for row in fused["bindings"] if row["status"] == "bound"
        ),
        "abstention_count": len(fused["abstentions"]),
        "warnings": fused["warnings"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(output_bytes)
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


__all__ = [
    "SPATIAL_FUSION_FORMAT",
    "SPATIAL_FUSION_MANIFEST_FORMAT",
    "SPATIAL_FUSION_STAGE",
    "SpatialFusionError",
    "fuse_spatial_evidence",
    "write_spatial_fusion",
]
