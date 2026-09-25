"""Focused tests for the first deterministic Slice 6.5 realization boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.scene_graph import build_scene_graph
from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_realization import (
    SPATIAL_REALIZATION_FORMAT,
    SPATIAL_REALIZATION_MANIFEST_FORMAT,
    SpatialRealizationError,
    _schema_hash,
    realize_spatial_layout,
    validate_footprints,
    write_spatial_realization,
)


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="RealizationTest",
        company_name="Test",
        description="A deterministic 3D driving scene.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerBike", kind="vehicle", tag="Player"),
            EntitySpec(name="Crates", kind="prop", count=4, placement="scatter"),
        ],
    )


def test_realization_uses_measured_footprints_and_emits_world_contract() -> None:
    schema = _schema()
    graph = build_scene_graph(schema)

    realized = realize_spatial_layout(
        schema,
        footprints={"Crates": 4.0},
        relation_graph=graph,
    )

    assert realized["format"] == SPATIAL_REALIZATION_FORMAT
    assert realized["authority"]["world_coordinates_emitted_here"] is True
    assert realized["authority"]["relation_graph_used"] is True
    crates = next(row for row in realized["layout"]["entities"]
                  if row["entity"] == "Crates")
    assert crates["measured_footprint_m"] == 4.0
    assert crates["instance_positions"]
    assert crates["group_radius"] > 0.0
    assert realized["inputs"]["relation_graph_hash"] == graph["graph_hash"]


def test_authored_scene_transforms_are_tier_a() -> None:
    schema = _schema()
    authored = schema.entities[0]
    authored.instance_positions = [[12.0, 0.0, 3.0]]
    authored.instance_yaws = [45.0]
    authored.origin_offset = [12.0, 3.0]
    authored.group_radius = 1.0
    schema.meta["authored_models"] = {"PlayerBike": {"file": "player_bike.fbx"}}

    realized = realize_spatial_layout(schema, footprints={"Crates": 4.0})

    row = next(item for item in realized["layout"]["entities"]
               if item["entity"] == "PlayerBike")
    assert row["authored_transform"] is True
    assert row["instance_positions"] == [[12.0, 0.0, 3.0]]
    assert row["instance_yaws"] == [45.0]
    assert row["origin_offset"] == [12.0, 3.0]
    assert realized["authority"]["authored_transforms_preserved"] is True


def test_spatial_fusion_is_consumed_only_for_non_authored_scenery() -> None:
    schema = _schema()
    fusion = {
        "format": "visual-reasoning-compiler/spatial-fusion/v1",
        "status": "complete",
        "inputs": {"schema_sha256": _schema_hash(schema)},
        "bindings": [{
            "entity": "Crates",
            "status": "bound",
            "schema_constraints": {"role": "scenery"},
            "visual_constraints": {"center_xy": [0.85, 0.25]},
        }],
    }

    realized = realize_spatial_layout(schema, spatial_fusion=fusion)

    assert realized["authority"]["spatial_fusion_used"] is True
    assert realized["inputs"]["spatial_fusion_sha256"]
    composition = realized["layout"]["composition_constraints"]
    crate_row = next(row for row in composition if row["entity"] == "Crates")
    assert crate_row["status"] == "applied"
    assert next(row for row in realized["layout"]["entities"]
                if row["entity"] == "Crates")["origin_offset"] != [0.0, 0.0]


def test_unknown_or_invalid_footprints_abstain() -> None:
    schema = _schema()

    with pytest.raises(SpatialRealizationError, match="undeclared entity"):
        validate_footprints(schema, {"Unknown": 2.0})
    with pytest.raises(SpatialRealizationError, match="finite positive"):
        validate_footprints(schema, {"Crates": 0.0})


def test_writer_hash_links_realization_manifest(tmp_path: Path) -> None:
    schema_path = tmp_path / "RealizationTest.schema.json"
    footprint_path = tmp_path / "footprints.json"
    output = tmp_path / "RealizationTest.world-layout.json"
    manifest = tmp_path / "RealizationTest.world-layout.manifest.json"
    schema_path.write_text(_schema().model_dump_json(indent=2), encoding="utf-8")
    footprint_path.write_text(
        json.dumps({"format": "measured-footprints/v1", "footprints": {"Crates": 2.5}}),
        encoding="utf-8",
    )

    result = write_spatial_realization(
        schema_path,
        output,
        footprints_path=footprint_path,
        manifest=manifest,
    )

    assert result == manifest
    realized = json.loads(output.read_text(encoding="utf-8"))
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert realized["format"] == SPATIAL_REALIZATION_FORMAT
    assert manifest_data["format"] == SPATIAL_REALIZATION_MANIFEST_FORMAT
    assert manifest_data["outputs"]["world_layout"]["sha256"]
    assert manifest_data["inputs"]["footprints"]["sha256"]
