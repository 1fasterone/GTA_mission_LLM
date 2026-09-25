"""Deterministic Slice 6.3 hybrid spatial-evidence fusion.

This module joins the frozen image-space evidence from Slice 6.1 to the
schema-side role table from Slice 6.2.  It deliberately stops at a unit-free
composition/constraint IR: no Unity metres, transforms, colliders, routes, or
new entities are created here.

Concept/schema roles remain semantic authority.  Image evidence contributes
normalized composition, visual depth, visible surface, and visual relations
only after a closed image label has been bound to one declared schema entity.
Missing image evidence receives a named role default and a warning; an
ambiguous declared-entity binding aborts instead of silently swapping names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .unity_schema import UnitySchema
from .unity_spatial_roles import (
    ROLE_ABSTAIN,
    ROLE_AIRBORNE_HAZARD,
    ROLE_CHECKPOINT,
    ROLE_HAZARD,
    ROLE_NAVIGATION_MARKER,
    ROLE_PLAYER_PATH,
    ROLE_UI_ONLY,
    SPATIAL_ROLES_FORMAT,
    SPATIAL_ROLES_MANIFEST_FORMAT,
)
from .unity_spatial_identity import semantic_schema_hash
from .unity_visual_bindings import bind_visual_assets
from .visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
    sha256_file,
)
from .visual_spatial_extraction import (
    SPATIAL_MANIFEST_FORMAT,
    validate_image_spatial_response,
)


SPATIAL_FUSION_FORMAT = "visual-reasoning-compiler/hybrid-spatial-fusion/v1"
SPATIAL_FUSION_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/hybrid-spatial-fusion-manifest/v1"
)
SPATIAL_FUSION_STAGE = "6.3_hybrid_spatial_fusion"


class SpatialFusionError(VisualReasoningCompilerError):
    """Raised when frozen spatial inputs cannot be fused safely."""


_DEFAULT_COMPOSITIONS: dict[str, dict[str, Any]] = {
    ROLE_PLAYER_PATH: {
        "name": "player_path_foreground",
        "center": {"x": 0.5, "y": 0.72},
        "extent": {"w": 0.36, "h": 0.18},
        "image_layer": "foreground",
        "applied_surface": "unknown",
    },
    ROLE_CHECKPOINT: {
        "name": "checkpoint_midground",
        "center": {"x": 0.5, "y": 0.45},
        "extent": {"w": 0.2, "h": 0.2},
        "image_layer": "midground",
        "applied_surface": "unknown",
    },
    ROLE_HAZARD: {
        "name": "hazard_midground",
        "center": {"x": 0.5, "y": 0.55},
        "extent": {"w": 0.14, "h": 0.14},
        "image_layer": "midground",
        "applied_surface": "unknown",
    },
    ROLE_AIRBORNE_HAZARD: {
        "name": "airborne_hazard_midground",
        "center": {"x": 0.5, "y": 0.3},
        "extent": {"w": 0.14, "h": 0.14},
        "image_layer": "midground",
        "applied_surface": "air",
    },
    ROLE_NAVIGATION_MARKER: {
        "name": "navigation_marker_midground",
        "center": {"x": 0.5, "y": 0.52},
        "extent": {"w": 0.1, "h": 0.14},
        "image_layer": "midground",
        "applied_surface": "unknown",
    },
    "scenery": {
        "name": "scenery_background",
        "center": {"x": 0.5, "y": 0.3},
        "extent": {"w": 0.3, "h": 0.25},
        "image_layer": "background",
        "applied_surface": "unknown",
    },
    ROLE_UI_ONLY: {
        "name": "ui_centered",
        "center": {"x": 0.5, "y": 0.5},
        "extent": {"w": 0.3, "h": 0.1},
        "image_layer": "unknown",
        "applied_surface": "unknown",
    },
    ROLE_ABSTAIN: {
        "name": "unresolved_centered",
        "center": {"x": 0.5, "y": 0.5},
        "extent": {"w": 0.1, "h": 0.1},
        "image_layer": "unknown",
        "applied_surface": "unknown",
    },
}

_WORLD_ROLES = frozenset({
    ROLE_PLAYER_PATH,
    ROLE_CHECKPOINT,
    ROLE_HAZARD,
    ROLE_AIRBORNE_HAZARD,
    ROLE_NAVIGATION_MARKER,
})
_VALID_MEDIA = frozenset({"ground", "water", "air", "indoor"})


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise SpatialFusionError(f"{label} not found: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpatialFusionError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise SpatialFusionError(f"{label} must contain a JSON object: {path}")
    return value, raw


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpatialFusionError(f"{label} must be an object")
    return value


def _manifest_output_hash(
    manifest: dict[str, Any],
    output_path: Path,
    *,
    output_key: str,
    label: str,
) -> str:
    outputs = _mapping(manifest.get("outputs"), label=f"{label} outputs")
    record = _mapping(outputs.get(output_key), label=f"{label} output {output_key}")
    expected = record.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise SpatialFusionError(f"{label} output has no SHA-256 record")
    actual = sha256_file(output_path)
    if actual != expected:
        raise SpatialFusionError(f"{label} hash does not match: {output_path}")
    return actual


def _schema_semantic_hash(schema: UnitySchema) -> str:
    return semantic_schema_hash(schema)


def _schema_hash(schema: UnitySchema) -> str:
    """Compatibility name used by the deterministic spatial consumers."""
    return semantic_schema_hash(schema)


def _closed_labels(closed: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if (
        closed.get("format") != "visual-reasoning-compiler/closed-image-ir/v1"
        or closed.get("status") != "closed"
    ):
        raise SpatialFusionError("Slice 6.3 requires a closed Slice-3 image IR")
    inventory = _mapping(closed.get("inventory"), label="closed inventory")
    elements = inventory.get("visible_elements")
    if not isinstance(elements, list):
        raise SpatialFusionError("closed inventory has no visible_elements list")
    labels: list[str] = []
    by_label: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(elements):
        element = _mapping(raw, label=f"closed inventory visible_elements[{index}]")
        label = element.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SpatialFusionError(
                f"closed inventory visible_elements[{index}] has no label"
            )
        if label in by_label:
            raise SpatialFusionError(f"closed inventory repeats label {label!r}")
        labels.append(label)
        by_label[label] = element
    return labels, by_label


def _load_inputs(
    *,
    closed_ir: Path,
    evidence: Path,
    evidence_manifest: Path,
    schema: Path,
    roles: Path,
    roles_manifest: Path,
) -> dict[str, Any]:
    closed, closed_raw = _read_json(closed_ir, label="closed image IR")
    evidence_data, evidence_raw = _read_json(
        evidence, label="Slice-6.1 image spatial evidence"
    )
    evidence_report, _ = _read_json(
        evidence_manifest, label="Slice-6.1 evidence manifest"
    )
    schema_data, schema_raw = _read_json(schema, label="Unity schema")
    roles_data, roles_raw = _read_json(roles, label="Slice-6.2 spatial roles")
    roles_report, _ = _read_json(
        roles_manifest, label="Slice-6.2 roles manifest"
    )

    labels, elements = _closed_labels(closed)
    if (
        evidence_report.get("format") != SPATIAL_MANIFEST_FORMAT
        or evidence_report.get("stage") != "6.1_image_spatial_extraction"
        or evidence_report.get("status") != "complete"
    ):
        raise SpatialFusionError("Slice-6.1 manifest is not a complete evidence artifact")
    _manifest_output_hash(
        evidence_report,
        evidence,
        output_key="image_spatial_evidence",
        label="Slice-6.1 evidence manifest",
    )
    evidence_inputs = _mapping(evidence_report.get("inputs"), label="Slice-6.1 inputs")
    closed_record = _mapping(
        evidence_inputs.get("closed_image_ir"),
        label="Slice-6.1 closed_image_ir input",
    )
    if closed_record.get("sha256") != sha256_bytes(closed_raw):
        raise SpatialFusionError("Slice-6.1 closed image IR hash does not match")
    request_fingerprint = closed.get("request_fingerprint")
    if request_fingerprint and closed_record.get("request_fingerprint") not in {
        None, request_fingerprint
    }:
        raise SpatialFusionError("Slice-6.1 request fingerprint does not match")
    try:
        canonical_evidence = validate_image_spatial_response(evidence_data, labels)
    except (ValueError, TypeError) as exc:
        raise SpatialFusionError(f"Slice-6.1 evidence is not valid: {exc}") from exc

    if (
        roles_data.get("format") != SPATIAL_ROLES_FORMAT
        or roles_data.get("stage") != "6.2_schema_spatial_roles"
        or roles_data.get("status") != "closed"
    ):
        raise SpatialFusionError("Slice-6.2 roles are not a closed role artifact")
    if (
        roles_report.get("format") != SPATIAL_ROLES_MANIFEST_FORMAT
        or roles_report.get("stage") != "6.2_schema_spatial_roles"
        or roles_report.get("status") != "complete"
    ):
        raise SpatialFusionError("Slice-6.2 roles manifest is not complete")
    _manifest_output_hash(
        roles_report,
        roles,
        output_key="spatial_roles",
        label="Slice-6.2 roles manifest",
    )
    role_inputs = _mapping(roles_report.get("inputs"), label="Slice-6.2 inputs")
    schema_record = _mapping(role_inputs.get("schema"), label="Slice-6.2 schema input")
    if schema_record.get("sha256") != sha256_bytes(schema_raw):
        raise SpatialFusionError("Slice-6.2 schema hash does not match")

    try:
        schema_model = UnitySchema.model_validate(schema_data)
    except (ValueError, TypeError) as exc:
        raise SpatialFusionError(f"Unity schema is invalid: {schema}") from exc
    semantic_hash = _schema_semantic_hash(schema_model)
    expected_semantic_hash = role_inputs.get("schema_semantic_sha256")
    if expected_semantic_hash != semantic_hash:
        raise SpatialFusionError("Slice-6.2 semantic schema hash does not match")
    if roles_data.get("inputs", {}).get("schema_sha256") != semantic_hash:
        raise SpatialFusionError("Slice-6.2 role table schema hash does not match")

    role_rows = roles_data.get("role_table")
    if not isinstance(role_rows, list):
        raise SpatialFusionError("Slice-6.2 role table is not a list")
    entity_names = [str(entity.name) for entity in schema_model.entities or []]
    if len(entity_names) != len(set(entity_names)):
        raise SpatialFusionError(
            "schema entity names are duplicated; hybrid visual binding abstains"
        )
    role_by_entity: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(role_rows):
        row = _mapping(raw, label=f"Slice-6.2 role_table[{index}]")
        entity = row.get("entity")
        role = row.get("role")
        if not isinstance(entity, str) or not entity:
            raise SpatialFusionError(f"Slice-6.2 role_table[{index}] has no entity")
        if entity in role_by_entity:
            raise SpatialFusionError(f"Slice-6.2 repeats role for {entity!r}")
        if entity not in entity_names:
            raise SpatialFusionError(
                f"Slice-6.2 role names undeclared schema entity {entity!r}"
            )
        role_by_entity[entity] = row
        if not isinstance(role, str) or not role:
            raise SpatialFusionError(f"Slice-6.2 role for {entity!r} is empty")
    if set(role_by_entity) != set(entity_names):
        missing = sorted(set(entity_names) - set(role_by_entity))
        raise SpatialFusionError(
            "Slice-6.2 role table does not cover schema entities: "
            + ", ".join(missing)
        )

    return {
        "closed": closed,
        "closed_raw": closed_raw,
        "labels": labels,
        "elements": elements,
        "evidence": canonical_evidence,
        "evidence_raw": evidence_raw,
        "evidence_report": evidence_report,
        "schema": schema_model,
        "schema_raw": schema_raw,
        "roles": roles_data,
        "roles_raw": roles_raw,
        "roles_report": roles_report,
        "role_by_entity": role_by_entity,
    }


def _bind_labels(
    *,
    schema: UnitySchema,
    closed: dict[str, Any],
    elements: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    raw_matches = closed.get("entity_matches")
    if not isinstance(raw_matches, list):
        raise SpatialFusionError("closed image IR has no entity_matches list")
    records: list[dict[str, Any]] = []
    reference_by_label: dict[str, str] = {}
    for index, raw in enumerate(raw_matches):
        match = _mapping(raw, label=f"closed entity_matches[{index}]")
        label = match.get("inventory_label")
        reference = match.get("reference")
        if not isinstance(label, str) or not label:
            raise SpatialFusionError(
                f"closed entity_matches[{index}] has no inventory_label"
            )
        if label not in elements:
            raise SpatialFusionError(
                f"closed entity match refers to unknown label {label!r}"
            )
        if label in reference_by_label:
            raise SpatialFusionError(f"closed entity match repeats label {label!r}")
        if not isinstance(reference, str) or not reference.strip():
            raise SpatialFusionError(
                f"closed entity match for {label!r} has no reference"
            )
        reference_by_label[label] = reference
        records.append({
            "inventory_label": label,
            "element": elements[label],
            "query": reference,
            "selection": {"_spatial_label": label},
        })

    if not records:
        return {}, reference_by_label
    try:
        raw_bindings = bind_visual_assets(schema, records)
    except VisualReasoningCompilerError as exc:
        raise SpatialFusionError(
            "closed visual/schema binding abstained: " + str(exc)
        ) from exc

    label_by_entity: dict[str, str] = {}
    for entity, selection in raw_bindings.items():
        label = selection.get("_spatial_label")
        if not isinstance(label, str) or not label:
            raise SpatialFusionError(
                f"visual binding for {entity!r} omitted its closed image label"
            )
        label_by_entity[str(entity)] = label
    return label_by_entity, reference_by_label


def _surface_for_role(
    *, role: str, evidence_surface: str, medium: Any,
) -> tuple[str, str | None]:
    if role == ROLE_UI_ONLY:
        return "unknown", (
            "UI-only schema role prevents image surface from becoming a world "
            "surface constraint"
            if evidence_surface != "unknown" else None
        )
    if role == ROLE_AIRBORNE_HAZARD:
        expected = "air"
    elif role in _WORLD_ROLES and str(medium) in _VALID_MEDIA:
        expected = str(medium)
    else:
        expected = None
    if expected and evidence_surface not in {"unknown", expected}:
        return expected, (
            f"schema role {role!r} wins over image surface {evidence_surface!r}; "
            f"applied surface is {expected!r}"
        )
    return evidence_surface, None


def _anchor_composition(
    anchor: dict[str, Any],
    *,
    role: str,
    medium: Any,
) -> tuple[dict[str, Any], str | None]:
    bbox = _mapping(anchor.get("bbox"), label="image anchor bbox")
    x = float(bbox["x"])
    y = float(bbox["y"])
    width = float(bbox["w"])
    height = float(bbox["h"])
    applied_surface, conflict = _surface_for_role(
        role=role,
        evidence_surface=str(anchor.get("surface") or "unknown"),
        medium=medium,
    )
    return {
        "center": {
            "x": round(x + width / 2.0, 6),
            "y": round(y + height / 2.0, 6),
        },
        "extent": {"w": round(width, 6), "h": round(height, 6)},
        "relative_area": round(width * height, 6),
        "image_layer": str(anchor["layer"]),
        "image_surface": str(anchor["surface"]),
        "applied_surface": applied_surface,
        "facing": str(anchor["facing"]),
        "occlusion": str(anchor["occlusion"]),
        "confidence": round(float(anchor["confidence"]), 6),
        "source": "image_spatial_evidence",
    }, conflict


def _default_composition(role: str, *, medium: Any) -> dict[str, Any]:
    profile = dict(_DEFAULT_COMPOSITIONS.get(role, _DEFAULT_COMPOSITIONS[ROLE_ABSTAIN]))
    if profile["applied_surface"] == "unknown" and role in _WORLD_ROLES:
        if str(medium) in _VALID_MEDIA:
            profile["applied_surface"] = str(medium)
    profile.update({
        "relative_area": round(
            float(profile["extent"]["w"]) * float(profile["extent"]["h"]), 6
        ),
        "image_surface": "unknown",
        "facing": "unknown",
        "occlusion": "unknown",
        "confidence": 0.0,
        "source": "named_role_default",
        "default_profile": profile["name"],
    })
    return profile


def _fuse(data: dict[str, Any]) -> dict[str, Any]:
    closed = data["closed"]
    evidence = data["evidence"]
    role_by_entity = data["role_by_entity"]
    schema: UnitySchema = data["schema"]
    label_by_entity, reference_by_label = _bind_labels(
        schema=schema,
        closed=closed,
        elements=data["elements"],
    )
    label_to_entity = {label: entity for entity, label in label_by_entity.items()}
    anchors = {
        str(anchor["label"]): anchor for anchor in evidence["anchors"]
    }


    medium = (data["roles"].get("inputs") or {}).get("archetype", {}).get("medium")
    warnings: list[str] = list(data["roles"].get("warnings") or [])
    abstentions: list[dict[str, Any]] = []
    entity_rows: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []

    for entity in sorted(role_by_entity):
        role_row = role_by_entity[entity]
        role = str(role_row["role"])
        label = label_by_entity.get(entity)
        if label and label in anchors:
            composition, conflict = _anchor_composition(
                anchors[label], role=role, medium=medium
            )
            if conflict:
                warnings.append(f"{entity}: {conflict}")
            binding_status = "bound"
            source = "image_spatial_evidence"
            if role == ROLE_ABSTAIN:
                warnings.append(
                    f"{entity}: schema spatial role abstains; image composition is "
                    "retained as visual evidence only"
                )
        else:
            composition = _default_composition(role, medium=medium)
            binding_status = "defaulted"
            source = "named_role_default"
            reason = (
                f"declared entity {entity!r} has no uniquely bound image anchor"
            )
            warnings.append(f"{entity}: {reason}; applied {composition['default_profile']}")
            abstentions.append({
                "entity": entity,
                "role": role,
                "reason": reason,
                "default_profile": composition["default_profile"],
            })
        row = {
            "entity": entity,
            "inventory_label": label,
            "reference": reference_by_label.get(label) if label else None,
            "role": role,
            "binding_status": binding_status,
            "composition": composition,
            "schema_role_evidence": role_row.get("evidence", []),
            "source": source,
        }
        entity_rows.append(row)
        constraints.append({
            "kind": "normalized_composition",
            "entity": entity,
            "status": binding_status,
            "composition": composition,
            "source": source,
        })

    relation_rows: list[dict[str, Any]] = []
    for relation in evidence["relations"]:
        source_label = str(relation["source"])
        target_label = str(relation["target"])
        source_entity = label_to_entity.get(source_label)
        target_entity = label_to_entity.get(target_label)
        if source_entity and target_entity:
            status = "bound"
            constraints.append({
                "kind": "relative_visual_relation",
                "source": source_entity,
                "target": target_entity,
                "relation": relation["relation"],
                "confidence": relation["confidence"],
                "source_labels": [source_label, target_label],
            })
        else:
            status = "unbound_visual_evidence"
            warnings.append(
                f"visual relation {source_label!r} -> {target_label!r} is not "
                "applied because both labels are not declared entity matches"
            )
        relation_rows.append({
            "source_label": source_label,
            "target_label": target_label,
            "source_entity": source_entity,
            "target_entity": target_entity,
            "relation": relation["relation"],
            "confidence": relation["confidence"],
            "status": status,
        })

    adjacency = list(data["roles"].get("adjacency_requirements") or [])
    for requirement in adjacency:
        constraints.append({
            "kind": "semantic_adjacency",
            "requirement": requirement.get("kind"),
            "entity": requirement.get("entity"),
            "player_path_entities": requirement.get("player_path_entities", []),
            "status": requirement.get("status"),
            "source": "schema_spatial_roles",
        })
        if requirement.get("status") == "abstain":
            abstentions.append({
                "entity": requirement.get("entity"),
                "role": "adjacency",
                "reason": requirement.get("abstention") or "schema adjacency abstained",
            })

    unbound_labels = sorted(set(data["labels"]) - set(label_to_entity))
    if unbound_labels:
        warnings.append(
            "visual labels not declared as schema entity matches remain evidence-only: "
            + ", ".join(unbound_labels)
        )
    regions = [
        {
            "id": f"surface:{region['surface']}",
            "surface": region["surface"],
            "bbox": region["bbox"],
            "confidence": region["confidence"],
            "source": "image_spatial_evidence",
        }
        for region in evidence["surface_regions"]
    ]
    clusters = []
    for cluster in evidence["clusters"]:
        members = [
            {
                "label": label,
                "entity": label_to_entity.get(label),
            }
            for label in cluster["members"]
        ]
        clusters.append({
            "members": members,
            "cohesion": cluster["cohesion"],
            "confidence": cluster["confidence"],
            "status": (
                "bound" if all(member["entity"] for member in members)
                else "unbound_visual_evidence"
            ),
            "source": "image_spatial_evidence",
        })

    return {
        "format": SPATIAL_FUSION_FORMAT,
        "stage": SPATIAL_FUSION_STAGE,
        "status": "closed",
        "authority": {
            "llm_called_here": False,
            "schema_roles_authority": True,
            "image_composition_authority": True,
            "ambiguous_entity_binding_abstains": True,
            "unbound_visual_labels_create_entities": False,
            "world_coordinates_emitted_here": False,
            "unity_transforms_emitted_here": False,
        },
        "inputs": {
            "closed_image_ir_sha256": sha256_bytes(data["closed_raw"]),
            "image_spatial_evidence_sha256": sha256_bytes(data["evidence_raw"]),
            "schema_sha256": sha256_bytes(data["schema_raw"]),
            "schema_semantic_sha256": data["roles"]["inputs"]["schema_sha256"],
            "schema_spatial_roles_sha256": sha256_bytes(data["roles_raw"]),
            "archetype": (data["roles"].get("inputs") or {}).get("archetype"),
        },
        "frame": evidence["frame"],
        "surface_regions": regions,
        "entity_regions": entity_rows,
        "relations": relation_rows,
        "clusters": clusters,
        "semantic_constraints": {
            "objectives": data["roles"].get("objectives", []),
            "adjacency_requirements": adjacency,
        },
        "constraints": constraints,
        "unbound_visual_labels": unbound_labels,
        "warnings": sorted(set(str(warning) for warning in warnings)),
        "abstentions": sorted(
            abstentions,
            key=lambda item: (str(item.get("entity")), str(item.get("role")), str(item.get("reason"))),
        ),
        "counts": {
            "declared_schema_entities": len(entity_rows),
            "bound_entities": sum(row["binding_status"] == "bound" for row in entity_rows),
            "defaulted_entities": sum(row["binding_status"] == "defaulted" for row in entity_rows),
            "bound_visual_relations": sum(row["status"] == "bound" for row in relation_rows),
            "unbound_visual_labels": len(unbound_labels),
        },
    }


def fuse_spatial_evidence_data(
    schema: UnitySchema,
    *,
    closed_ir: Mapping[str, Any],
    image_evidence: Mapping[str, Any],
    spatial_roles: Mapping[str, Any],
) -> dict[str, Any]:
    """Fuse already-loaded inputs for the in-process Unity sidecar pipeline.

    The file-based :func:`fuse_spatial_evidence` function remains the canonical
    Slice-6.3 API and provenance boundary. This adapter reuses the same
    validation and fusion core when the generator has already loaded the
    closed artifacts, so the 6.4–6.6 pipeline emits the canonical format.
    """
    closed = dict(closed_ir)
    evidence_data = dict(image_evidence)
    roles = dict(spatial_roles)
    labels, elements = _closed_labels(closed)
    try:
        evidence = validate_image_spatial_response(evidence_data, labels)
    except (ValueError, TypeError) as exc:
        raise SpatialFusionError(f"Slice-6.1 evidence is not valid: {exc}") from exc

    if (
        roles.get("format") != SPATIAL_ROLES_FORMAT
        or roles.get("stage") != "6.2_schema_spatial_roles"
        or roles.get("status") != "closed"
    ):
        raise SpatialFusionError("Slice-6.2 roles are not a closed role artifact")
    role_rows = roles.get("role_table")
    if not isinstance(role_rows, list):
        raise SpatialFusionError("Slice-6.2 role table is not a list")
    entity_names = [str(entity.name) for entity in schema.entities]
    role_by_entity: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(role_rows):
        row = _mapping(raw, label=f"Slice-6.2 role_table[{index}]")
        entity = row.get("entity")
        role = row.get("role")
        if not isinstance(entity, str) or not entity:
            raise SpatialFusionError(f"Slice-6.2 role_table[{index}] has no entity")
        if entity in role_by_entity or entity not in entity_names:
            raise SpatialFusionError(
                f"Slice-6.2 role names undeclared or repeated: {entity!r}"
            )
        if not isinstance(role, str) or not role:
            raise SpatialFusionError(f"Slice-6.2 role for {entity!r} is empty")
        role_by_entity[entity] = row
    if set(role_by_entity) != set(entity_names):
        missing = sorted(set(entity_names) - set(role_by_entity))
        raise SpatialFusionError(
            "Slice-6.2 role table does not cover schema entities: "
            + ", ".join(missing)
        )
    expected_hash = _schema_hash(schema)
    if (roles.get("inputs") or {}).get("schema_sha256") != expected_hash:
        raise SpatialFusionError("Slice-6.2 role table schema hash does not match")

    def canonical(value: Mapping[str, Any]) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    data = {
        "closed": closed,
        "closed_raw": canonical(closed),
        "labels": labels,
        "elements": elements,
        "evidence": evidence,
        "evidence_raw": canonical(evidence_data),
        "schema": schema,
        "schema_raw": canonical(schema.model_dump(mode="json")),
        "roles": roles,
        "roles_raw": canonical(roles),
        "role_by_entity": role_by_entity,
    }
    return _fuse(data)


def fuse_spatial_evidence(
    *,
    closed_ir: Path,
    evidence: Path,
    evidence_manifest: Path,
    schema: Path,
    roles: Path,
    roles_manifest: Path,
    output: Path,
    manifest: Path | None = None,
) -> Path:
    """Validate 6.1/6.2 lineage and write the closed 6.3 fusion IR."""
    manifest_path = manifest or output.with_name(output.stem + ".manifest.json")
    existing = [path for path in (output, manifest_path) if path.exists()]
    if existing:
        raise SpatialFusionError(
            "refusing to overwrite Slice-6.3 artifact(s): "
            + ", ".join(str(path) for path in existing)
        )
    data = _load_inputs(
        closed_ir=closed_ir,
        evidence=evidence,
        evidence_manifest=evidence_manifest,
        schema=schema,
        roles=roles,
        roles_manifest=roles_manifest,
    )
    fused = _fuse(data)
    output_bytes = (
        json.dumps(fused, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    manifest_data = {
        "format": SPATIAL_FUSION_MANIFEST_FORMAT,
        "stage": SPATIAL_FUSION_STAGE,
        "status": "complete",
        "authority": fused["authority"],
        "inputs": {
            "closed_image_ir": {
                "path": str(closed_ir),
                "sha256": sha256_bytes(data["closed_raw"]),
            },
            "image_spatial_evidence": {
                "path": str(evidence),
                "sha256": sha256_bytes(data["evidence_raw"]),
            },
            "image_spatial_manifest": {
                "path": str(evidence_manifest),
                "sha256": sha256_file(evidence_manifest),
            },
            "schema": {
                "path": str(schema),
                "sha256": sha256_bytes(data["schema_raw"]),
            },
            "schema_spatial_roles": {
                "path": str(roles),
                "sha256": sha256_bytes(data["roles_raw"]),
            },
            "schema_spatial_manifest": {
                "path": str(roles_manifest),
                "sha256": sha256_file(roles_manifest),
            },
        },
        "outputs": {
            "hybrid_spatial_fusion": {
                "path": str(output),
                "sha256": sha256_bytes(output_bytes),
            }
        },
        "counts": fused["counts"],
        "warnings": fused["warnings"],
        "abstention_count": len(fused["abstentions"]),
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
    "fuse_spatial_evidence_data",
]
