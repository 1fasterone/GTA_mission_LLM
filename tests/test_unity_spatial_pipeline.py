"""Regression tests for the main Unity spatial-sidecar handoff."""

from __future__ import annotations

import json
from pathlib import Path

from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_identity import semantic_schema_hash
from src.unity_spatial_pipeline import (
    freeze_spatial_sidecars,
    prepare_spatial_layout,
)


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="PipelineSpatial",
        company_name="Test",
        description="A deterministic route with scenery.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerBike", kind="vehicle", tag="Player"),
            EntitySpec(
                name="Track", kind="track", placement="path",
                path_points=[[0, 0, 0], [0, 0, 30]], path_width=8,
            ),
            EntitySpec(name="Trees", kind="prop", placement="scatter", count=4),
        ],
    )


def test_prepare_and_freeze_cover_the_final_deterministic_tail(tmp_path: Path) -> None:
    schema = _schema()
    before_hash = semantic_schema_hash(schema)

    prepared = prepare_spatial_layout(schema, reset_generated_layout=True)

    assert prepared["spatial_roles"]["status"] == "closed"
    assert prepared["scene_graph"]["status"] == "closed"
    assert prepared["world_layout"]["status"] == "complete"
    assert semantic_schema_hash(schema) == before_hash

    project_root = tmp_path / schema.project_name
    project_root.mkdir()
    schema_path = project_root / f"{schema.project_name}.schema.json"
    schema_path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
    frozen = freeze_spatial_sidecars(project_root, schema)

    assert frozen["spatial_validation"]["format"].endswith("spatial-validation/v1")
    assert frozen["paths"]["spatial_roles"].is_file()
    assert frozen["paths"]["scene_graph"].is_file()
    assert frozen["paths"]["world_layout"].is_file()
    assert frozen["paths"]["spatial_validation"].is_file()
    validation = json.loads(
        frozen["paths"]["spatial_validation"].read_text(encoding="utf-8")
    )
    assert validation["inputs"]["schema_sha256"] == semantic_schema_hash(schema)


def test_runtime_behavior_reconciliation_does_not_stale_spatial_identity() -> None:
    schema = _schema()
    before_hash = semantic_schema_hash(schema)

    schema.entities[0].behavior = "VehicleController"

    assert semantic_schema_hash(schema) == before_hash


def test_post_schema_asset_query_enrichment_does_not_stale_spatial_identity() -> None:
    schema = _schema()
    before_hash = semantic_schema_hash(schema)

    schema.entities[0].sketchfab_query = "low poly racing bike"

    assert semantic_schema_hash(schema) == before_hash


def test_final_freeze_can_consume_generator_measured_footprints(tmp_path: Path) -> None:
    schema = _schema()
    project_root = tmp_path / schema.project_name
    project_root.mkdir()
    (project_root / f"{schema.project_name}.schema.json").write_text(
        schema.model_dump_json(indent=2), encoding="utf-8"
    )

    frozen = freeze_spatial_sidecars(
        project_root,
        schema,
        measured_footprints={"Trees": 2.5},
    )
    layout = json.loads(
        frozen["paths"]["world_layout"].read_text(encoding="utf-8")
    )
    trees = next(row for row in layout["layout"]["entities"] if row["entity"] == "Trees")
    assert trees["measured_footprint_m"] == 2.5
