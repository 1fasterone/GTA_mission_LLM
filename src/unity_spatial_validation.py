"""Deterministic Slice 6.6 validation for realized spatial artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional

from .unity_reachability import flood_fill_reachable
from .unity_schema import UnitySchema
from .unity_spatial_fusion import (
    SPATIAL_FUSION_FORMAT,
    _schema_hash as fusion_schema_hash,
)
from .unity_spatial_realization import (
    SPATIAL_REALIZATION_FORMAT,
    _validate_spatial_fusion,
)
from .unity_spatial_roles import SPATIAL_ROLES_FORMAT
from .visual_reasoning_compiler import sha256_bytes


SPATIAL_VALIDATION_FORMAT = "visual-reasoning-compiler/spatial-validation/v1"
SPATIAL_VALIDATION_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/spatial-validation-manifest/v1"
)
SPATIAL_VALIDATION_STAGE = "6.6_spatial_validation"


class SpatialValidationError(ValueError):
    """Raised when the validation inputs are malformed or hash-incompatible."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _position_rows(world_layout: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    layout = world_layout.get("layout")
    if not isinstance(layout, Mapping):
        raise SpatialValidationError("world layout has no layout object")
    rows = layout.get("entities")
    if not isinstance(rows, list):
        raise SpatialValidationError("world layout has no entity rows")
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SpatialValidationError(f"world layout entities[{index}] must be an object")
        name = str(row.get("entity") or "")
        if not name or name in result:
            raise SpatialValidationError(
                f"world layout has a missing or duplicate entity row at index {index}"
            )
        result[name] = row
    return result


def _validate_role_sidecar(
    schema: UnitySchema,
    roles: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if roles.get("format") != SPATIAL_ROLES_FORMAT or roles.get("status") != "closed":
        raise SpatialValidationError("spatial validation requires a closed Slice-6.2 role sidecar")
    if ((roles.get("inputs") or {}).get("schema_sha256")) != fusion_schema_hash(schema):
        raise SpatialValidationError("schema spatial roles hash does not match the schema")
    table = roles.get("role_table")
    if not isinstance(table, list):
        raise SpatialValidationError("schema spatial roles have no role_table")
    result = {
        str(row.get("entity")): dict(row)
        for row in table
        if isinstance(row, Mapping) and row.get("entity")
    }
    declared = {str(entity.name) for entity in schema.entities}
    if set(result) != declared:
        raise SpatialValidationError("schema spatial roles do not cover every declared entity")
    return result


def _points(row: Mapping[str, Any]) -> list[tuple[float, float, float]]:
    raw = row.get("instance_positions") or []
    if not isinstance(raw, list):
        return []
    points: list[tuple[float, float, float]] = []
    for point in raw:
        if isinstance(point, list) and len(point) >= 3:
            points.append((float(point[0]), float(point[1]), float(point[2])))
    return points


def _nearest_polyline_distance(x: float, z: float, points: list[list[float]]) -> float:
    if not points:
        return math.inf
    best = math.inf
    for index in range(len(points) - 1):
        ax, az = float(points[index][0]), float(points[index][2])
        bx, bz = float(points[index + 1][0]), float(points[index + 1][2])
        dx, dz = bx - ax, bz - az
        length2 = dx * dx + dz * dz
        if length2 <= 1e-12:
            distance = math.hypot(x - ax, z - az)
        else:
            t = max(0.0, min(1.0, ((x - ax) * dx + (z - az) * dz) / length2))
            distance = math.hypot(x - (ax + t * dx), z - (az + t * dz))
        best = min(best, distance)
    if len(points) == 1:
        best = min(best, math.hypot(x - points[0][0], z - points[0][2]))
    return best


def _copy_realized_schema(
    schema: UnitySchema,
    rows: Mapping[str, Mapping[str, Any]],
) -> UnitySchema:
    copy = schema.model_copy(deep=True)
    for entity in copy.entities:
        row = rows[entity.name]
        entity.origin_offset = list(row.get("origin_offset") or [0.0, 0.0])
        entity.group_radius = float(row.get("group_radius") or 0.0)
        entity.elevation = float(row.get("elevation") or 0.0)
        entity.instance_positions = [list(point) for point in row.get("instance_positions") or []]
        entity.instance_yaws = [float(value) for value in row.get("instance_yaws") or []]
        entity.path_points = [list(point) for point in row.get("path_points") or []]
    return copy


def validate_spatial_layout(
    schema: UnitySchema,
    *,
    world_layout: Mapping[str, Any],
    spatial_roles: Mapping[str, Any],
    spatial_fusion: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate a realized layout without mutating the supplied schema."""
    if world_layout.get("format") != SPATIAL_REALIZATION_FORMAT:
        raise SpatialValidationError("world layout is not a world-layout/v1 artifact")
    if world_layout.get("status") != "complete":
        raise SpatialValidationError("spatial validation requires a complete world layout")
    schema_hash = fusion_schema_hash(schema)
    if ((world_layout.get("inputs") or {}).get("schema_sha256")) != schema_hash:
        raise SpatialValidationError("world layout hash does not match the schema")
    roles = _validate_role_sidecar(schema, spatial_roles)
    rows = _position_rows(world_layout)
    declared = {str(entity.name) for entity in schema.entities}
    if set(rows) != declared:
        raise SpatialValidationError("world layout does not cover every declared entity")

    fusion = None
    fusion_hash = None
    if spatial_fusion is not None:
        if spatial_fusion.get("format") != SPATIAL_FUSION_FORMAT:
            raise SpatialValidationError("spatial fusion has the wrong format")
        fusion, fusion_hash = _validate_spatial_fusion(schema, spatial_fusion)
        if ((world_layout.get("inputs") or {}).get("spatial_fusion_sha256")) != fusion_hash:
            raise SpatialValidationError("world layout spatial-fusion hash does not match")

    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    def check(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})
        if status == "fail":
            errors.append(f"[spatial.strict] {name}: {detail}")
        elif status == "warn":
            warnings.append(f"[spatial] {name}: {detail}")

    check("entity_role_coverage", "pass", f"{len(roles)} declared entities have one closed role")

    if fusion is None:
        check("fused_image_bindings", "warn", "no Slice-6.3 fusion artifact was supplied")
    else:
        bound = [row for row in fusion.get("bindings", [])
                 if isinstance(row, Mapping) and row.get("status") == "bound"]
        check("fused_image_bindings", "pass", f"{len(bound)} fused visual references bind uniquely")

    group_overlap = 0
    row_list = list(rows.items())
    for index, (_left_name, left) in enumerate(row_list):
        left_radius = float(left.get("group_radius") or 0.0)
        left_origin = list(left.get("origin_offset") or [0.0, 0.0])
        if left_radius <= 0.0:
            continue
        for _right_name, right in row_list[index + 1:]:
            right_radius = float(right.get("group_radius") or 0.0)
            if right_radius <= 0.0:
                continue
            right_origin = list(right.get("origin_offset") or [0.0, 0.0])
            if math.hypot(
                float(left_origin[0]) - float(right_origin[0]),
                float(left_origin[1]) - float(right_origin[1]),
            ) + 0.25 < left_radius + right_radius:
                group_overlap += 1
    check(
        "group_footprints",
        "fail" if group_overlap else "pass",
        f"{group_overlap} realized group footprint overlap(s)",
    )

    instance_overlap = 0
    entity_by_name = {str(entity.name): entity for entity in schema.entities}
    for name, row in rows.items():
        points = _points(row)
        entity = entity_by_name[name]
        measured = row.get("measured_footprint_m")
        minimum = float(measured) if measured else max(0.5, entity.scale * 1.15)
        for index, left_point in enumerate(points):
            for right_point in points[index + 1:]:
                if math.dist(left_point, right_point) + 0.01 < minimum:
                    instance_overlap += 1
    check(
        "instance_footprints",
        "fail" if instance_overlap else "pass",
        f"{instance_overlap} within-group instance overlap(s)",
    )

    bounds = ((world_layout.get("layout") or {}).get("world_bounds"))
    if not isinstance(bounds, list) or len(bounds) != 4:
        check("world_bounds", "warn", "world layout did not emit a four-value world_bounds envelope")
        bounds_tuple = None
    else:
        bounds_tuple = (
            float(bounds[0]), float(bounds[1]),
            float(bounds[2]), float(bounds[3]),
        )
        out_of_bounds = 0
        for name, row in rows.items():
            radius = float(row.get("group_radius") or 0.0)
            xmin, zmin, xmax, zmax = bounds_tuple
            for x, _y, z in _points(row):
                if not (xmin - 0.01 <= x <= xmax + 0.01
                        and zmin - 0.01 <= z <= zmax + 0.01):
                    out_of_bounds += 1
            origin = list(row.get("origin_offset") or [0.0, 0.0])
            if not (xmin - radius - 0.01 <= float(origin[0]) <= xmax + radius + 0.01
                    and zmin - radius - 0.01 <= float(origin[1]) <= zmax + radius + 0.01):
                out_of_bounds += 1
        check(
            "world_bounds",
            "fail" if out_of_bounds else "pass",
            f"{out_of_bounds} position(s) outside the realized world envelope",
        )

    route_entities = [
        entity for entity in schema.entities
        if entity.placement == "path" and entity.path_points
    ]
    required_adjacency = []
    role_sidecar_rows = spatial_roles.get("adjacency_requirements") or []
    if isinstance(role_sidecar_rows, list):
        required_adjacency = [
            row for row in role_sidecar_rows
            if isinstance(row, Mapping) and row.get("required")
        ]
    adjacency_failures = 0
    adjacency_unproved = 0
    for requirement in required_adjacency:
        target_name = str(requirement.get("entity") or "")
        target_points = _points(rows[target_name]) if target_name in rows else []
        if not route_entities or not target_points:
            adjacency_unproved += 1
            continue
        width = max((route.path_width for route in route_entities), default=0.0)
        tolerance = width / 2.0 + float(rows[target_name].get("group_radius") or 0.0) + 0.5
        distance = min(
            _nearest_polyline_distance(point[0], point[2], route.path_points)
            for point in target_points
            for route in route_entities
        )
        if distance > tolerance:
            adjacency_failures += 1
    check(
        "route_adjacency",
        "fail" if adjacency_failures else ("warn" if adjacency_unproved else "pass"),
        f"{adjacency_failures} required route adjacency failure(s); "
        f"{adjacency_unproved} could not be proven",
    )

    airborne_failures = 0
    for name, role_row in roles.items():
        if role_row.get("role") != "airborne_hazard":
            continue
        points = _points(rows[name])
        if points and max(point[1] for point in points) <= 0.0:
            airborne_failures += 1
    check(
        "surface_layer_restrictions",
        "fail" if airborne_failures else "pass",
        f"{airborne_failures} airborne role(s) realized without positive elevation",
    )

    primitive = str(((world_layout.get("layout") or {}).get("spatial_primitive")) or "")
    if primitive in {"ArenaPlane", "route_corridor", "ground_plane"} \
            and bounds_tuple is not None:
        realized_schema = _copy_realized_schema(schema, rows)
        target_names = {
            name for name, row in roles.items()
            if row.get("role") in {"checkpoint", "hazard", "navigation_marker"}
        }
        targets = {
            name: [(point[0], point[2]) for point in _points(rows[name])]
            for name in sorted(target_names)
            if _points(rows[name])
        }
        reachability = flood_fill_reachable(
            realized_schema.entities,
            bounds_tuple,
            (0.0, 0.0),
            targets,
        )
        check(
            "applicable_reachability",
            "fail" if reachability.unreachable else "pass",
            f"{len(reachability.unreachable)} target group(s) unreachable from player spawn",
        )
    else:
        check(
            "applicable_reachability",
            "warn",
            f"primitive {primitive or 'unknown'!r} is outside the 2-D proof domain",
        )

    check(
        "stale_realization_guard",
        "pass",
        "world layout carries the current schema semantic hash",
    )
    status = "fail" if errors else ("warn" if warnings else "pass")
    return {
        "format": SPATIAL_VALIDATION_FORMAT,
        "stage": SPATIAL_VALIDATION_STAGE,
        "status": status,
        "authority": {
            "llm_called_here": False,
            "world_layout_validated_here": True,
            "schema_semantics_authority": True,
            "world_coordinates_emitted_here": False,
        },
        "inputs": {
            "schema_sha256": schema_hash,
            "world_layout_sha256": sha256_bytes(_canonical_bytes(world_layout)),
            "spatial_fusion_sha256": fusion_hash,
        },
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }


def write_spatial_validation(
    schema_path: Path,
    world_layout_path: Path,
    spatial_roles_path: Path,
    output: Path,
    *,
    spatial_fusion_path: Optional[Path] = None,
    manifest: Optional[Path] = None,
) -> Path:
    """Validate frozen world layout inputs and write a report plus manifest."""
    if not schema_path.is_file():
        raise SpatialValidationError(f"schema not found: {schema_path}")
    schema_raw = schema_path.read_bytes()
    try:
        schema = UnitySchema.model_validate(json.loads(schema_raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SpatialValidationError(f"schema is not valid UnitySchema JSON: {schema_path}") from exc

    def load(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
        if not path.is_file():
            raise SpatialValidationError(f"{label} not found: {path}")
        raw = path.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpatialValidationError(f"{label} is not valid JSON: {path}") from exc
        if not isinstance(value, dict):
            raise SpatialValidationError(f"{label} must be a JSON object: {path}")
        return value, raw

    world_layout, world_raw = load(world_layout_path, "world layout")
    roles, roles_raw = load(spatial_roles_path, "schema spatial roles")
    fusion: Optional[dict[str, Any]] = None
    fusion_raw: Optional[bytes] = None
    if spatial_fusion_path is not None:
        fusion, fusion_raw = load(spatial_fusion_path, "spatial fusion")
    report = validate_spatial_layout(
        schema,
        world_layout=world_layout,
        spatial_roles=roles,
        spatial_fusion=fusion,
    )
    report_bytes = (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path = manifest or output.with_name(output.stem + ".manifest.json")
    manifest_data: dict[str, Any] = {
        "format": SPATIAL_VALIDATION_MANIFEST_FORMAT,
        "stage": SPATIAL_VALIDATION_STAGE,
        "status": report["status"],
        "authority": report["authority"],
        "inputs": {
            "schema": {"path": str(schema_path), "sha256": sha256_bytes(schema_raw)},
            "world_layout": {"path": str(world_layout_path), "sha256": sha256_bytes(world_raw)},
            "spatial_roles": {"path": str(spatial_roles_path), "sha256": sha256_bytes(roles_raw)},
        },
        "outputs": {
            "spatial_validation": {"path": str(output), "sha256": sha256_bytes(report_bytes)}
        },
        "check_count": len(report["checks"]),
        "errors": report["errors"],
        "warnings": report["warnings"],
    }
    if spatial_fusion_path is not None and fusion_raw is not None:
        manifest_data["inputs"]["spatial_fusion"] = {
            "path": str(spatial_fusion_path), "sha256": sha256_bytes(fusion_raw)
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(report_bytes)
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


__all__ = [
    "SPATIAL_VALIDATION_FORMAT",
    "SPATIAL_VALIDATION_MANIFEST_FORMAT",
    "SPATIAL_VALIDATION_STAGE",
    "SpatialValidationError",
    "validate_spatial_layout",
    "write_spatial_validation",
]
