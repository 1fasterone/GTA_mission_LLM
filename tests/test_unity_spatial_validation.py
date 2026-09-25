"""Focused tests for deterministic Slice 6.6 spatial validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_realization import realize_spatial_layout
from src.unity_spatial_roles import derive_spatial_roles
from src.unity_spatial_validation import (
    SPATIAL_VALIDATION_FORMAT,
    SPATIAL_VALIDATION_MANIFEST_FORMAT,
    SpatialValidationError,
    validate_spatial_layout,
    write_spatial_validation,
)


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="ValidationTest",
        company_name="Test",
        description="A boat follows a river to an arch.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerBoat", kind="vehicle", tag="Player"),
            EntitySpec(
                name="RiverRoute", kind="river", placement="path",
                path_points=[[0, 0, 0], [0, 0, 30]], path_width=8,
            ),
            EntitySpec(name="CheckpointArch", kind="checkpoint"),
        ],
    )


def _fusion(schema: UnitySchema) -> dict:
    from src.unity_spatial_realization import _schema_hash

    return {
        "format": "visual-reasoning-compiler/spatial-fusion/v1",
        "status": "complete",
        "inputs": {"schema_sha256": _schema_hash(schema)},
        "bindings": [{
            "entity": "CheckpointArch",
            "status": "bound",
            "schema_constraints": {"role": "checkpoint"},
            "visual_constraints": {"center_xy": [0.5, 0.5]},
        }],
    }


def test_validation_passes_realized_route_layout() -> None:
    source = _schema()
    roles = derive_spatial_roles(source)
    fusion = _fusion(source)
    realized = realize_spatial_layout(source.model_copy(deep=True), spatial_fusion=fusion)

    report = validate_spatial_layout(
        source,
        world_layout=realized,
        spatial_roles=roles,
        spatial_fusion=fusion,
    )

    assert report["format"] == SPATIAL_VALIDATION_FORMAT
    assert report["status"] == "pass"
    assert not report["errors"]
    assert {row["check"] for row in report["checks"]} >= {
        "entity_role_coverage", "group_footprints", "instance_footprints",
        "world_bounds", "route_adjacency", "applicable_reachability",
    }


def test_validation_rejects_out_of_bounds_position() -> None:
    source = _schema()
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    player = next(row for row in realized["layout"]["entities"]
                  if row["entity"] == "PlayerBoat")
    player["instance_positions"] = [[9999.0, 0.0, 9999.0]]

    report = validate_spatial_layout(
        source, world_layout=realized, spatial_roles=roles
    )

    assert report["status"] == "fail"
    assert any("world_bounds" in error for error in report["errors"])


def test_validation_rejects_stale_world_layout_hash() -> None:
    source = _schema()
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    realized["inputs"]["schema_sha256"] = "stale"

    with pytest.raises(SpatialValidationError, match="does not match"):
        validate_spatial_layout(source, world_layout=realized, spatial_roles=roles)


def test_validation_writer_hash_links_report(tmp_path: Path) -> None:
    source = _schema()
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    schema_path = tmp_path / "ValidationTest.schema.json"
    layout_path = tmp_path / "ValidationTest.world-layout.json"
    roles_path = tmp_path / "ValidationTest.spatial-roles.json"
    output = tmp_path / "ValidationTest.spatial-validation.json"
    manifest = tmp_path / "ValidationTest.spatial-validation.manifest.json"
    schema_path.write_text(source.model_dump_json(indent=2), encoding="utf-8")
    layout_path.write_text(json.dumps(realized), encoding="utf-8")
    roles_path.write_text(json.dumps(roles), encoding="utf-8")

    result = write_spatial_validation(
        schema_path,
        layout_path,
        roles_path,
        output,
        manifest=manifest,
    )

    assert result == manifest
    assert json.loads(output.read_text(encoding="utf-8"))["format"] == SPATIAL_VALIDATION_FORMAT
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["format"] == SPATIAL_VALIDATION_MANIFEST_FORMAT
    assert manifest_data["outputs"]["spatial_validation"]["sha256"]
