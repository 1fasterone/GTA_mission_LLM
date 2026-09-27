"""Deterministic Slice 6.5 world realization boundary.

The existing ``src.unity_layout`` archetypes are the geometry authority.  This
module gives that authority a frozen, inspectable boundary: it validates the
optional relation graph and measured footprint inputs, calls the existing
archetype realizer, verifies that authored Scene Compiler transforms survived,
and writes a hash-linked world-layout sidecar.

This is intentionally the first 6.5 slice, not the missing 6.3 fusion pass.
The optional relation graph carries declared gameplay relationships; visual
image constraints are not invented here.  When no measured footprint sidecar
is supplied, the existing primitive-size approximation remains byte-compatible
with direct ``compute_layout(schema)`` calls.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional

from .scene_graph import (
    SCENE_GRAPH_FORMAT,
    validate_scene_graph,
)
from .unity_layout import compute_layout, terrain_bounds
from .unity_layout_archetype import spatial_relationship_of
from .unity_schema import UnitySchema
from .unity_spatial_fusion import SPATIAL_FUSION_FORMAT
from .unity_spatial_identity import semantic_schema_hash
from .visual_reasoning_compiler import sha256_bytes


LEGACY_SPATIAL_FUSION_FORMAT = "visual-reasoning-compiler/spatial-fusion/v1"


SPATIAL_REALIZATION_FORMAT = "visual-reasoning-compiler/world-layout/v1"
SPATIAL_REALIZATION_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/world-layout-manifest/v1"
)
SPATIAL_REALIZATION_STAGE = "6.5_world_realization"


class SpatialRealizationError(ValueError):
    """Raised when 6.5 inputs cannot be resolved without guessing."""


def _schema_hash(schema: UnitySchema) -> str:
    return semantic_schema_hash(schema)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _footprint_payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = raw.get("footprints")
    if nested is None:
        return raw
    if not isinstance(nested, Mapping):
        raise SpatialRealizationError(
            "[spatial.strict] footprint sidecar 'footprints' must be an object"
        )
    return nested


def validate_footprints(
    schema: UnitySchema,
    raw: Optional[Mapping[str, Any]],
) -> dict[str, float]:
    """Validate measured ground footprints without silently remapping names."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpatialRealizationError(
            "[spatial.strict] measured footprint input must be a JSON object"
        )
    declared_hash = raw.get("schema_sha256")
    if declared_hash is not None and str(declared_hash) != _schema_hash(schema):
        raise SpatialRealizationError(
            "[spatial.strict] measured footprints belong to a different schema; "
            "refresh the frozen asset-binding measurement"
        )
    entity_names = {str(entity.name) for entity in schema.entities}
    values = _footprint_payload(raw)
    result: dict[str, float] = {}
    for name, value in values.items():
        entity_name = str(name)
        if entity_name not in entity_names:
            raise SpatialRealizationError(
                f"[spatial.strict] measured footprint names undeclared entity "
                f"{entity_name!r}"
            )
        if isinstance(value, bool):
            raise SpatialRealizationError(
                f"[spatial.strict] measured footprint for {entity_name!r} "
                "must be a finite positive number"
            )
        try:
            footprint = float(value)
        except (TypeError, ValueError) as exc:
            raise SpatialRealizationError(
                f"[spatial.strict] measured footprint for {entity_name!r} "
                "must be a finite positive number"
            ) from exc
        if not math.isfinite(footprint) or footprint <= 0.0:
            raise SpatialRealizationError(
                f"[spatial.strict] measured footprint for {entity_name!r} "
                "must be a finite positive number"
            )
        result[entity_name] = round(footprint, 3)
    return result


def _validate_relation_graph(
    schema: UnitySchema,
    relation_graph: Optional[Mapping[str, Any]],
) -> Optional[str]:
    if relation_graph is None:
        return None
    if not isinstance(relation_graph, Mapping):
        raise SpatialRealizationError(
            "[spatial.strict] relation graph must be a JSON object"
        )
    if relation_graph.get("format") != SCENE_GRAPH_FORMAT:
        raise SpatialRealizationError(
            "[spatial.strict] relation graph is not a scene-relation-graph/v1 "
            "artifact"
        )
    expected = _schema_hash(schema)
    actual = ((relation_graph.get("inputs") or {}).get("schema_sha256"))
    if actual != expected:
        raise SpatialRealizationError(
            "[spatial.strict] relation graph belongs to a different schema; "
            "rebuild the Slice-6.4 graph"
        )
    try:
        validate_scene_graph(relation_graph)
    except ValueError as exc:
        raise SpatialRealizationError(
            f"[spatial.strict] relation graph failed validation: {exc}"
        ) from exc
    graph_hash = relation_graph.get("graph_hash")
    if not graph_hash:
        raise SpatialRealizationError(
            "[spatial.strict] relation graph has no graph_hash"
        )
    return str(graph_hash)


def _authored_entity_names(schema: UnitySchema) -> set[str]:
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    authored = meta.get("authored_models")
    if not isinstance(authored, Mapping):
        return set()
    declared = {str(entity.name) for entity in schema.entities}
    return {str(name) for name in authored if str(name) in declared}


def _positions(value: Any) -> list[list[float]]:
    return [list(point) for point in (value or [])]


def _validate_spatial_fusion(
    schema: UnitySchema,
    spatial_fusion: Optional[Mapping[str, Any]],
) -> tuple[Optional[Mapping[str, Any]], Optional[str]]:
    if spatial_fusion is None:
        return None, None
    if not isinstance(spatial_fusion, Mapping):
        raise SpatialRealizationError(
            "[spatial.strict] spatial fusion must be a JSON object"
        )
    if spatial_fusion.get("format") not in {
        SPATIAL_FUSION_FORMAT,
        LEGACY_SPATIAL_FUSION_FORMAT,
    }:
        raise SpatialRealizationError(
            "[spatial.strict] spatial fusion has an unsupported artifact format"
        )
    if spatial_fusion.get("status") not in {"complete", "closed"}:
        raise SpatialRealizationError(
            "[spatial.strict] spatial fusion contains unresolved required "
            "visual/schema bindings; resolve Slice 6.3 abstentions first"
        )
    expected = _schema_hash(schema)
    fusion_inputs = spatial_fusion.get("inputs") or {}
    actual = fusion_inputs.get("schema_sha256")
    if actual != expected:
        actual = fusion_inputs.get("schema_semantic_sha256")
    if actual != expected:
        raise SpatialRealizationError(
            "[spatial.strict] spatial fusion belongs to a different schema; "
            "rebuild Slice 6.3"
        )
    bindings = spatial_fusion.get("bindings")
    entity_regions = spatial_fusion.get("entity_regions")
    rows = bindings if isinstance(bindings, list) else entity_regions
    if not isinstance(rows, list):
        raise SpatialRealizationError(
            "[spatial.strict] spatial fusion has no bindings or entity regions list"
        )
    declared = {str(entity.name) for entity in schema.entities}
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion rows[{index}] must be an object"
            )
        entity = row.get("entity")
        bound = row.get("status") == "bound" or row.get("binding_status") == "bound"
        if not bound:
            continue
        if not isinstance(entity, str) or entity not in declared:
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion binds undeclared entity {entity!r}"
            )
        if entity in seen:
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion binds entity {entity!r} more than once"
            )
        visual = row.get("visual_constraints")
        if visual is None and isinstance(row.get("composition"), Mapping):
            composition = row["composition"]
            center = composition.get("center")
            if isinstance(center, Mapping):
                visual = {"center_xy": [center.get("x"), center.get("y")]}
        if not isinstance(visual, Mapping):
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion has no visual constraints for {entity!r}"
            )
        center = visual.get("center_xy")
        if (not isinstance(center, list) or len(center) != 2
                or any(isinstance(value, bool) for value in center)):
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion center_xy for {entity!r} is invalid"
            )
        try:
            coords = [float(value) for value in center]
        except (TypeError, ValueError) as exc:
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion center_xy for {entity!r} is invalid"
            ) from exc
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in coords):
            raise SpatialRealizationError(
                f"[spatial.strict] spatial fusion center_xy for {entity!r} is outside [0,1]"
            )
        seen.add(entity)
    return spatial_fusion, sha256_bytes(_canonical_bytes(spatial_fusion))


def _world_bounds(schema: UnitySchema) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for entity in schema.entities:
        for point in entity.path_points:
            if len(point) >= 3:
                points.append((float(point[0]), float(point[2])))
        for point in entity.instance_positions:
            if len(point) >= 3:
                points.append((float(point[0]), float(point[2])))
        if entity.group_radius > 0.0:
            points.extend((
                (entity.origin_offset[0] - entity.group_radius,
                 entity.origin_offset[1] - entity.group_radius),
                (entity.origin_offset[0] + entity.group_radius,
                 entity.origin_offset[1] + entity.group_radius),
            ))
    if not points:
        return (-10.0, -10.0, 10.0, 10.0)
    xs = [point[0] for point in points]
    zs = [point[1] for point in points]
    return min(xs), min(zs), max(xs), max(zs)


def _composition_center(row: Mapping[str, Any]) -> Optional[tuple[float, float]]:
    visual = row.get("visual_constraints")
    if visual is None and isinstance(row.get("composition"), Mapping):
        center = row["composition"].get("center")
        if isinstance(center, Mapping):
            visual = {"center_xy": [center.get("x"), center.get("y")]}
    if not isinstance(visual, Mapping):
        return None
    center = visual.get("center_xy")
    if not isinstance(center, list) or len(center) != 2:
        return None
    return float(center[0]), float(center[1])


def _image_landmark_compositions(
    spatial_fusion: Optional[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Extract closed normalized image rectangles for deterministic layout.

    Slice 6.3 owns the normalized image evidence; this adapter only carries
    that already-validated composition into the layout primitive.  It never
    infers a missing rectangle from entity keywords.  Legacy fusion artifacts
    without ``bbox`` remain usable through their validated center/extent pair.
    """
    if spatial_fusion is None:
        return {}
    raw_rows = spatial_fusion.get("bindings")
    if not isinstance(raw_rows, list):
        raw_rows = spatial_fusion.get("entity_regions") or []
    result: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("status") != "bound" and row.get("binding_status") != "bound":
            continue
        entity = row.get("entity")
        composition = row.get("composition")
        if not isinstance(entity, str) or not isinstance(composition, Mapping):
            continue
        center = composition.get("center")
        extent = composition.get("extent")
        bbox = composition.get("bbox")
        if isinstance(bbox, Mapping):
            try:
                x, y = float(bbox["x"]), float(bbox["y"])
                width, height = float(bbox["w"]), float(bbox["h"])
            except (KeyError, TypeError, ValueError):
                continue
        elif isinstance(center, Mapping) and isinstance(extent, Mapping):
            try:
                cx, cy = float(center["x"]), float(center["y"])
                width, height = float(extent["w"]), float(extent["h"])
            except (KeyError, TypeError, ValueError):
                continue
            x, y = cx - width / 2.0, cy - height / 2.0
        else:
            continue
        if not all(math.isfinite(value) for value in (x, y, width, height)):
            continue
        if width <= 0.0 or height <= 0.0 or x < 0.0 or y < 0.0 \
                or x + width > 1.000001 or y + height > 1.000001:
            continue
        result[entity] = {
            "bbox": {
                "x": round(x, 6), "y": round(y, 6),
                "w": round(width, 6), "h": round(height, 6),
            },
            "center": {
                "x": round(x + width / 2.0, 6),
                "y": round(y + height / 2.0, 6),
            },
            "extent": {"w": round(width, 6), "h": round(height, 6)},
            "role": str(row.get("role") or ""),
        }
    return result


def _apply_composition_constraints(
    schema: UnitySchema,
    spatial_fusion: Mapping[str, Any],
    *,
    preexisting_positions: set[str],
    authored_names: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply safe image composition hints to non-authored scenery groups.

    Gameplay roles, path groups, preplaced scaffolds, and authored Scene
    Compiler transforms are never moved here.  A candidate that would overlap
    an existing group is recorded as an abstention instead of being forced.
    """
    raw_rows = spatial_fusion.get("bindings")
    if not isinstance(raw_rows, list):
        raw_rows = spatial_fusion.get("entity_regions") or []
    by_entity = {
        str(row.get("entity")): row
        for row in raw_rows
        if isinstance(row, Mapping)
        and (row.get("status") == "bound" or row.get("binding_status") == "bound")
        and isinstance(row.get("entity"), str)
    }
    xmin, zmin, xmax, zmax = _world_bounds(schema)
    if xmax <= xmin or zmax <= zmin:
        return [], ["[spatial] image composition not applied: degenerate world bounds"]
    rows: list[dict[str, Any]] = []
    messages: list[str] = []
    entities = {str(entity.name): entity for entity in schema.entities}
    for name in sorted(by_entity):
        entity = entities[name]
        role = ((by_entity[name].get("schema_constraints") or {}).get("role"))
        role = role or by_entity[name].get("role")
        center = _composition_center(by_entity[name])
        if center is None:
            continue
        if (schema.game_type == "2d_topdown"
                and isinstance(by_entity[name].get("composition"), Mapping)
                and (by_entity[name]["composition"].get("bbox")
                     or by_entity[name]["composition"].get("extent"))):
            rows.append({
                "entity": name,
                "status": "protected",
                "reason": "top-down normalized image rectangle was realized by board layout",
                "normalized_center_xy": [round(center[0], 6), round(center[1], 6)],
            })
            continue
        if role != "scenery":
            rows.append({
                "entity": name,
                "status": "protected",
                "reason": "schema role owns gameplay placement",
            })
            continue
        if name in authored_names:
            rows.append({
                "entity": name,
                "status": "protected",
                "reason": "authored Scene Compiler transform is Tier A",
            })
            continue
        if name in preexisting_positions:
            rows.append({
                "entity": name,
                "status": "protected",
                "reason": "preplaced deterministic scaffold is preserved",
            })
            continue
        if entity.placement == "path" or not entity.instance_positions:
            rows.append({
                "entity": name,
                "status": "protected",
                "reason": "path or unbaked placement is owned by the archetype",
            })
            continue

        target_x = xmin + center[0] * (xmax - xmin)
        target_z = zmax - center[1] * (zmax - zmin)
        dx = target_x - entity.origin_offset[0]
        dz = target_z - entity.origin_offset[1]
        collision = False
        for other in schema.entities:
            if other.name == entity.name or other.group_radius <= 0.0:
                continue
            distance = math.hypot(
                target_x - other.origin_offset[0],
                target_z - other.origin_offset[1],
            )
            if distance + 0.25 < entity.group_radius + other.group_radius:
                collision = True
                break
        if collision:
            rows.append({
                "entity": name,
                "status": "abstain",
                "reason": "normalized composition target would overlap a realized group",
                "target_center_xz": [round(target_x, 3), round(target_z, 3)],
            })
            messages.append(
                f"[spatial] composition target for {name!r} was not applied "
                "because it would overlap a realized group"
            )
            continue
        entity.instance_positions = [
            [round(point[0] + dx, 3), point[1], round(point[2] + dz, 3)]
            for point in entity.instance_positions
        ]
        entity.origin_offset = [round(target_x, 3), round(target_z, 3)]
        rows.append({
            "entity": name,
            "status": "applied",
            "target_center_xz": [round(target_x, 3), round(target_z, 3)],
            "normalized_center_xy": [round(center[0], 6), round(center[1], 6)],
        })
        messages.append(
            f"[spatial] applied normalized image composition to scenery {name!r}"
        )
    return rows, messages


def _refresh_entity_regions(schema: UnitySchema) -> None:
    """Keep descriptive world regions aligned after a safe group translation."""
    world = schema.world_spec
    if world is None:
        return
    by_name = {str(entity.name): entity for entity in schema.entities}
    for region in world.regions:
        entity_name = str(region.name)
        entities = list(region.entities or [])
        if entity_name in by_name:
            entity = by_name[entity_name]
        elif len(entities) == 1 and str(entities[0]) in by_name:
            entity = by_name[str(entities[0])]
        else:
            continue
        if entity.group_radius <= 0.0:
            continue
        x, z = entity.origin_offset
        radius = entity.group_radius
        region.bounds = [
            round(x - radius, 3), round(z - radius, 3),
            round(x + radius, 3), round(z + radius, 3),
        ]


def _layout_rows(
    schema: UnitySchema,
    footprints: Mapping[str, float],
    authored_names: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entity in schema.entities:
        rows.append({
            "entity": entity.name,
            "placement": entity.placement,
            "relationship": spatial_relationship_of(entity),
            "authored_transform": entity.name in authored_names,
            "measured_footprint_m": footprints.get(entity.name),
            "origin_offset": list(entity.origin_offset),
            "group_radius": entity.group_radius,
            "elevation": entity.elevation,
            "instance_positions": _positions(entity.instance_positions),
            "instance_yaws": list(entity.instance_yaws),
            "path_points": _positions(entity.path_points),
        })
    return rows


def realize_spatial_layout(
    schema: UnitySchema,
    *,
    footprints: Optional[Mapping[str, Any]] = None,
    relation_graph: Optional[Mapping[str, Any]] = None,
    spatial_fusion: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Run the existing deterministic Unity archetype realizer.

    ``schema`` is mutated in the same way as a direct ``compute_layout`` call:
    ``origin_offset``, ``group_radius``, ``elevation`` and baked instance
    fields remain the runtime contract.  Authored Scene Compiler transforms
    are captured before realization and are a strict Tier-A invariant.
    """
    input_schema_hash = _schema_hash(schema)
    measured = validate_footprints(schema, footprints)
    relation_hash = _validate_relation_graph(schema, relation_graph)
    fusion, fusion_hash = _validate_spatial_fusion(schema, spatial_fusion)
    authored_names = _authored_entity_names(schema)
    preexisting_positions = {
        str(entity.name) for entity in schema.entities if entity.instance_positions
    }
    authored_positions = {
        entity.name: (
            _positions(entity.instance_positions),
            list(entity.instance_yaws),
            list(entity.origin_offset),
        )
        for entity in schema.entities
        if entity.name in authored_names and entity.instance_positions
    }

    image_landmarks = _image_landmark_compositions(fusion)
    messages = compute_layout(
        schema,
        measured or None,
        image_landmarks=image_landmarks or None,
    )

    for entity in schema.entities:
        before = authored_positions.get(entity.name)
        if before is None:
            continue
        after = (
            _positions(entity.instance_positions),
            list(entity.instance_yaws),
            list(entity.origin_offset),
        )
        if after != before:
            raise SpatialRealizationError(
                f"[spatial.strict] authored transform for {entity.name!r} "
                "changed during world realization"
            )

    composition_rows: list[dict[str, Any]] = []
    composition_messages: list[str] = []
    if fusion is not None:
        composition_rows, composition_messages = _apply_composition_constraints(
            schema,
            fusion,
            preexisting_positions=preexisting_positions,
            authored_names=authored_names,
        )
        _refresh_entity_regions(schema)
    rows = _layout_rows(schema, measured, authored_names)
    graph = {
        "format": SPATIAL_REALIZATION_FORMAT,
        "stage": SPATIAL_REALIZATION_STAGE,
        "status": "complete",
        "authority": {
            "llm_called_here": False,
            "archetype_geometry_authority": True,
            "relation_graph_used": relation_hash is not None,
            "spatial_fusion_used": fusion is not None,
            "measured_footprints_used": bool(measured),
            "image_spatial_evidence_used_here": False,
            "world_coordinates_emitted_here": True,
            "authored_transforms_preserved": True,
        },
        "inputs": {
            "schema_sha256": input_schema_hash,
            "relation_graph_hash": relation_hash,
            "spatial_fusion_sha256": fusion_hash,
            "footprints_sha256": sha256_bytes(_canonical_bytes(measured)),
        },
        "layout": {
            "archetype": schema.meta.get("layout_archetype")
            if isinstance(schema.meta, dict) else None,
            "spatial_primitive": schema.meta.get("spatial_primitive")
            if isinstance(schema.meta, dict) else None,
            "messages": list(messages) + composition_messages,
            "composition_constraints": composition_rows,
            "world_bounds": list(terrain_bounds(schema.entities, schema.locomotion)),
            "entities": rows,
        },
        "warnings": [],
    }
    return graph


def write_spatial_realization(
    schema_path: Path,
    output: Path,
    *,
    footprints_path: Optional[Path] = None,
    relation_graph_path: Optional[Path] = None,
    spatial_fusion_path: Optional[Path] = None,
    manifest: Optional[Path] = None,
) -> Path:
    """Read frozen inputs, realize world coordinates, and write a manifest."""
    if not schema_path.is_file():
        raise SpatialRealizationError(f"schema not found: {schema_path}")
    schema_raw = schema_path.read_bytes()
    try:
        schema = UnitySchema.model_validate(json.loads(schema_raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SpatialRealizationError(
            f"schema is not valid UnitySchema JSON: {schema_path}"
        ) from exc

    footprints: Optional[Mapping[str, Any]] = None
    footprints_raw: Optional[bytes] = None
    if footprints_path is not None:
        if not footprints_path.is_file():
            raise SpatialRealizationError(f"footprint sidecar not found: {footprints_path}")
        footprints_raw = footprints_path.read_bytes()
        try:
            footprints = json.loads(footprints_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpatialRealizationError(
                f"footprint sidecar is not valid JSON: {footprints_path}"
            ) from exc

    relation_graph: Optional[Mapping[str, Any]] = None
    relation_raw: Optional[bytes] = None
    if relation_graph_path is not None:
        if not relation_graph_path.is_file():
            raise SpatialRealizationError(
                f"relation graph not found: {relation_graph_path}"
            )
        relation_raw = relation_graph_path.read_bytes()
        try:
            relation_graph = json.loads(relation_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpatialRealizationError(
                f"relation graph is not valid JSON: {relation_graph_path}"
            ) from exc

    spatial_fusion: Optional[Mapping[str, Any]] = None
    fusion_raw: Optional[bytes] = None
    if spatial_fusion_path is not None:
        if not spatial_fusion_path.is_file():
            raise SpatialRealizationError(
                f"spatial fusion not found: {spatial_fusion_path}"
            )
        fusion_raw = spatial_fusion_path.read_bytes()
        try:
            spatial_fusion = json.loads(fusion_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpatialRealizationError(
                f"spatial fusion is not valid JSON: {spatial_fusion_path}"
            ) from exc

    realized = realize_spatial_layout(
        schema,
        footprints=footprints,
        relation_graph=relation_graph,
        spatial_fusion=spatial_fusion,
    )
    output_bytes = (json.dumps(realized, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path = manifest or output.with_name(output.stem + ".manifest.json")
    manifest_data: dict[str, Any] = {
        "format": SPATIAL_REALIZATION_MANIFEST_FORMAT,
        "stage": SPATIAL_REALIZATION_STAGE,
        "status": "complete",
        "authority": realized["authority"],
        "inputs": {
            "schema": {"path": str(schema_path), "sha256": sha256_bytes(schema_raw)},
            "schema_semantic_sha256": realized["inputs"]["schema_sha256"],
        },
        "outputs": {
            "world_layout": {
                "path": str(output),
                "sha256": sha256_bytes(output_bytes),
            }
        },
        "entity_count": len(realized["layout"]["entities"]),
        "warnings": realized["warnings"],
    }
    if footprints_path is not None and footprints_raw is not None:
        manifest_data["inputs"]["footprints"] = {
            "path": str(footprints_path),
            "sha256": sha256_bytes(footprints_raw),
        }
    if relation_graph_path is not None and relation_raw is not None:
        manifest_data["inputs"]["relation_graph"] = {
            "path": str(relation_graph_path),
            "sha256": sha256_bytes(relation_raw),
        }
    if spatial_fusion_path is not None and fusion_raw is not None:
        manifest_data["inputs"]["spatial_fusion"] = {
            "path": str(spatial_fusion_path),
            "sha256": sha256_bytes(fusion_raw),
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
    "SPATIAL_REALIZATION_FORMAT",
    "SPATIAL_REALIZATION_MANIFEST_FORMAT",
    "SPATIAL_REALIZATION_STAGE",
    "SpatialRealizationError",
    "realize_spatial_layout",
    "validate_footprints",
    "write_spatial_realization",
]
