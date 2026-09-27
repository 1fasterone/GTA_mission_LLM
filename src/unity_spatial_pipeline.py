"""Orchestration for the deterministic Unity spatial sidecars.

This is the narrow bridge between the validated schema and the existing Unity
generator.  It owns no schema semantics and never calls the Pipeline LLM:

* 6.2 derives schema roles;
* 6.4 derives the closed relation graph;
* 6.3 is optional and consumes already-closed image artifacts;
* 6.5 realizes the existing layout archetype;
* 6.6 validates the realized contract.

The first pass runs before generation so the generator sees the same fused
constraints.  The freeze pass runs after generation so the persisted artifacts
describe the generator's final measured-footprint layout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from .scene_graph import build_scene_graph
from .unity_schema import UnitySchema
from .unity_spatial_fusion import (
    SPATIAL_FUSION_MANIFEST_FORMAT,
    SPATIAL_FUSION_STAGE,
    fuse_spatial_evidence_data,
)
from .unity_spatial_identity import semantic_schema_hash
from .unity_spatial_realization import realize_spatial_layout
from .unity_spatial_roles import derive_spatial_roles
from .unity_spatial_validation import validate_spatial_layout
from .visual_reasoning_compiler import sha256_bytes


class SpatialPipelineError(ValueError):
    """Raised when the sidecar handoff cannot remain hash-consistent."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SpatialPipelineError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpatialPipelineError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SpatialPipelineError(f"{label} must contain a JSON object: {path}")
    return payload


def load_spatial_inputs(
    *,
    closed_image_ir: Optional[Path] = None,
    image_spatial_evidence: Optional[Path] = None,
) -> tuple[Optional[Mapping[str, Any]], Optional[Mapping[str, Any]]]:
    """Load the optional closed image inputs, refusing a partial handoff."""
    if (closed_image_ir is None) != (image_spatial_evidence is None):
        raise SpatialPipelineError(
            "Slice 6.3 requires both --closed-ir and --image-evidence; "
            "omit both to run the schema-only spatial path"
        )
    if closed_image_ir is None:
        return None, None
    assert image_spatial_evidence is not None
    return (
        _read_json(closed_image_ir, "closed image IR"),
        _read_json(image_spatial_evidence, "image spatial evidence"),
    )


def prepare_spatial_layout(
    schema: UnitySchema,
    *,
    closed_image_ir: Optional[Mapping[str, Any]] = None,
    image_spatial_evidence: Optional[Mapping[str, Any]] = None,
    reset_generated_layout: bool = False,
) -> dict[str, Any]:
    """Prepare and realize sidecars before generator model resolution."""
    if (closed_image_ir is None) != (image_spatial_evidence is None):
        raise SpatialPipelineError(
            "Slice 6.3 requires both closed image IR and image spatial evidence"
        )
    roles = derive_spatial_roles(schema)
    graph = build_scene_graph(schema, roles)
    fusion = None
    if closed_image_ir is not None and image_spatial_evidence is not None:
        fusion = fuse_spatial_evidence_data(
            schema,
            closed_ir=closed_image_ir,
            image_evidence=image_spatial_evidence,
            spatial_roles=roles,
        )
    if reset_generated_layout:
        authored = {
            str(name) for name in ((schema.meta or {}).get("authored_models") or {})
        } if isinstance(schema.meta, dict) else set()
        fusion_rows = (fusion or {}).get("bindings")
        if not isinstance(fusion_rows, list):
            fusion_rows = (fusion or {}).get("entity_regions") or []
        composition_targets = set()
        topdown_image_targets = set()
        for row in fusion_rows:
            if not isinstance(row, Mapping):
                continue
            status = row.get("status") or row.get("binding_status")
            role = (row.get("schema_constraints") or {}).get("role") or row.get("role")
            if status == "bound" and role == "scenery" and row.get("entity"):
                composition_targets.add(str(row["entity"]))
            if (schema.game_type == "2d_topdown" and status == "bound"
                    and row.get("entity") and row.get("composition") is not None):
                topdown_image_targets.add(str(row["entity"]))
        for entity in schema.entities:
            # Existing deterministic scaffolds (for example the racing start
            # grid) are already authoritative.  Top-down image rectangles are
            # a deterministic board-layout input, so every bound group is
            # reset there; authored Scene Compiler transforms remain Tier A.
            reset_targets = (topdown_image_targets if schema.game_type == "2d_topdown"
                             else composition_targets)
            if (entity.name not in reset_targets
                    or entity.name in authored
                    or entity.placement == "path"):
                continue
            entity.instance_positions = []
            entity.instance_yaws = []
            entity.origin_offset = [0.0, 0.0]
            entity.group_radius = 0.0
            entity.elevation = 0.0

    realization = realize_spatial_layout(
        schema,
        relation_graph=graph,
        spatial_fusion=fusion,
    )
    validation = validate_spatial_layout(
        schema,
        world_layout=realization,
        spatial_roles=roles,
        spatial_fusion=fusion,
    )
    return {
        "spatial_roles": roles,
        "scene_graph": graph,
        "spatial_fusion": fusion,
        "world_layout": realization,
        "spatial_validation": validation,
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> bytes:
    raw = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _write_sidecar(
    *,
    root: Path,
    project_name: str,
    suffix: str,
    payload: Mapping[str, Any],
    stage: str,
    manifest_format: str,
    status: str,
    inputs: Mapping[str, Any],
) -> Path:
    output = root / f"{project_name}.{suffix}.json"
    raw = _write_json(output, payload)
    manifest = output.with_name(output.stem + ".manifest.json")
    manifest_payload = {
        "format": manifest_format,
        "stage": stage,
        "status": status,
        "authority": payload.get("authority", {}),
        "inputs": dict(inputs),
        "outputs": {
            suffix: {"path": str(output), "sha256": sha256_bytes(raw)},
        },
    }
    _write_json(manifest, manifest_payload)
    return output


def freeze_spatial_sidecars(
    project_root: Path,
    schema: UnitySchema,
    *,
    closed_image_ir: Optional[Mapping[str, Any]] = None,
    image_spatial_evidence: Optional[Mapping[str, Any]] = None,
    measured_footprints: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Freeze final post-generator sidecars beside the generated project."""
    if (closed_image_ir is None) != (image_spatial_evidence is None):
        raise SpatialPipelineError(
            "Slice 6.3 requires both closed image IR and image spatial evidence"
        )
    roles = derive_spatial_roles(schema)
    graph = build_scene_graph(schema, roles)
    fusion = None
    if closed_image_ir is not None and image_spatial_evidence is not None:
        fusion = fuse_spatial_evidence_data(
            schema,
            closed_ir=closed_image_ir,
            image_evidence=image_spatial_evidence,
            spatial_roles=roles,
        )

    # The generator has already baked final positions.  A deep copy lets the
    # existing archetype code rebuild its descriptive envelope without changing
    # the schema that was rendered into Unity files.
    snapshot_schema = schema.model_copy(deep=True)
    realization = realize_spatial_layout(
        snapshot_schema,
        footprints=measured_footprints,
        relation_graph=graph,
        spatial_fusion=fusion,
    )
    validation = validate_spatial_layout(
        schema,
        world_layout=realization,
        spatial_roles=roles,
        spatial_fusion=fusion,
    )

    schema_path = project_root / f"{project_name(schema)}.schema.json"
    schema_raw = schema_path.read_bytes() if schema_path.is_file() else b""
    base_inputs = {
        "schema": {
            "path": str(schema_path),
            "sha256": sha256_bytes(schema_raw) if schema_raw else None,
            "semantic_sha256": semantic_schema_hash(schema),
        },
    }
    role_path = _write_sidecar(
        root=project_root,
        project_name=project_name(schema),
        suffix="spatial-roles",
        payload=roles,
        stage="6.2_schema_spatial_roles",
        manifest_format="visual-reasoning-compiler/schema-spatial-roles-manifest/v1",
        status="complete",
        inputs=base_inputs,
    )
    graph_path = _write_sidecar(
        root=project_root,
        project_name=project_name(schema),
        suffix="scene-graph",
        payload=graph,
        stage="6.4_scene_relation_graph",
        manifest_format="visual-reasoning-compiler/scene-relation-graph-manifest/v1",
        status="complete",
        inputs={**base_inputs, "spatial_roles": {"path": str(role_path)}},
    )
    fusion_path = None
    if fusion is not None:
        fusion_path = _write_sidecar(
            root=project_root,
            project_name=project_name(schema),
            suffix="spatial-fusion",
            payload=fusion,
            stage=SPATIAL_FUSION_STAGE,
            manifest_format=SPATIAL_FUSION_MANIFEST_FORMAT,
            status="complete",
            inputs={**base_inputs, "closed_visual_inputs": True},
        )
    world_path = _write_sidecar(
        root=project_root,
        project_name=project_name(schema),
        suffix="world-layout",
        payload=realization,
        stage="6.5_world_realization",
        manifest_format="visual-reasoning-compiler/world-layout-manifest/v1",
        status="complete",
        inputs={
            **base_inputs,
            "scene_graph": {"path": str(graph_path)},
            "spatial_fusion": {"path": str(fusion_path)} if fusion_path else None,
        },
    )
    validation_path = _write_sidecar(
        root=project_root,
        project_name=project_name(schema),
        suffix="spatial-validation",
        payload=validation,
        stage="6.6_spatial_validation",
        manifest_format="visual-reasoning-compiler/spatial-validation-manifest/v1",
        status=str(validation.get("status")),
        inputs={
            **base_inputs,
            "world_layout": {"path": str(world_path)},
            "spatial_roles": {"path": str(role_path)},
            "spatial_fusion": {"path": str(fusion_path)} if fusion_path else None,
        },
    )
    return {
        "spatial_roles": roles,
        "scene_graph": graph,
        "spatial_fusion": fusion,
        "world_layout": realization,
        "spatial_validation": validation,
        "paths": {
            "spatial_roles": role_path,
            "scene_graph": graph_path,
            "spatial_fusion": fusion_path,
            "world_layout": world_path,
            "spatial_validation": validation_path,
        },
    }


def project_name(schema: UnitySchema) -> str:
    return str(schema.project_name)


__all__ = [
    "SpatialPipelineError",
    "freeze_spatial_sidecars",
    "load_spatial_inputs",
    "prepare_spatial_layout",
]
