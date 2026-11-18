"""Focused tests for deterministic Slice 6.6 spatial validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_pipeline import prepare_spatial_layout
from src.unity_spatial_realization import realize_spatial_layout
from src.unity_spatial_roles import derive_spatial_roles
from src.unity_spatial_validation import (
    _MAX_NAMED_FINDINGS,
    SPATIAL_VALIDATION_FORMAT,
    SPATIAL_VALIDATION_MANIFEST_FORMAT,
    SpatialValidationError,
    _named_detail,
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


def test_validation_allows_separate_volume_layers_to_share_xz_footprints() -> None:
    source = UnitySchema(
        project_name="VolumeValidation",
        company_name="Test",
        description="An aircraft combat volume.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="fly",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerAircraft", kind="vehicle", tag="Player"),
            EntitySpec(name="EnemyAircraft", kind="enemy", placement="scatter"),
        ],
    )
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    rows = realized["layout"]["entities"]
    for row, elevation in zip(rows, (40.0, 80.0)):
        row["origin_offset"] = [0.0, 0.0]
        row["group_radius"] = 10.0
        row["elevation"] = elevation
        row["instance_positions"] = [[0.0, elevation, 0.0]]

    report = validate_spatial_layout(
        source,
        world_layout=realized,
        spatial_roles=roles,
    )

    group_check = next(
        check for check in report["checks"] if check["check"] == "group_footprints"
    )
    assert group_check["status"] == "pass"


def test_validation_uses_realized_instances_for_distributed_group_footprints() -> None:
    """Distributed scenery has several valid stops, not one solid disk.

    Every stop is packed against the others, but aggregating them around one
    centroid creates large overlapping enclosing circles.  Slice 6.6 must
    validate the baked runtime positions rather than reject that valid layout.
    """
    source = UnitySchema(
        project_name="DistributedValidation",
        company_name="Test",
        description="A ground arena with distributed scenery.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="walk",
        scenes=["Main"],
        entities=[
            EntitySpec(name="Player", kind="player", tag="Player"),
            *[
                EntitySpec(
                    name=f"Scenery{index}", kind="prop", count=6,
                    placement="scatter", spacing=2.0, scale=1.0,
                )
                for index in range(5)
            ],
        ],
    )

    prepared = prepare_spatial_layout(source, reset_generated_layout=True)
    group_check = next(
        check for check in prepared["spatial_validation"]["checks"]
        if check["check"] == "group_footprints"
    )

    assert not prepared["spatial_validation"]["errors"]
    assert group_check == {
        "check": "group_footprints",
        "status": "pass",
        "detail": "0 realized group footprint overlap(s)",
    }


def test_validation_rejects_realized_cross_group_instance_overlap() -> None:
    """The realized-instance path must still reject a physical collision."""
    source = UnitySchema(
        project_name="CrossGroupCollision",
        company_name="Test",
        description="A ground arena.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="walk",
        scenes=["Main"],
        entities=[
            EntitySpec(name="Player", kind="player", tag="Player"),
            EntitySpec(name="Crate", kind="prop"),
        ],
    )
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    player = next(row for row in realized["layout"]["entities"]
                  if row["entity"] == "Player")
    crate = next(row for row in realized["layout"]["entities"]
                 if row["entity"] == "Crate")
    crate["origin_offset"] = list(player["origin_offset"])
    crate["group_radius"] = float(player["group_radius"])
    crate["elevation"] = float(player["elevation"])
    crate["instance_positions"] = [list(player["instance_positions"][0])]

    report = validate_spatial_layout(
        source, world_layout=realized, spatial_roles=roles
    )
    group_check = next(
        check for check in report["checks"] if check["check"] == "group_footprints"
    )

    assert report["status"] == "fail"
    assert group_check["status"] == "fail"
    # The detail names the colliding pair, both points, the distance and the
    # distance the two bodies need, so a failed run is diagnosable from its
    # own report without a replay script.
    point = "[" + ", ".join(
        f"{value:.2f}" for value in player["instance_positions"][0]) + "]"
    assert group_check["detail"] == (
        "1 realized group footprint overlap(s): "
        f"Player {point} x Crate {point} 0.00 m apart < 0.95 m required "
        "(bodies 0.60 + 0.60 m, 0.25 m tolerance)"
    )


def test_validation_marks_airspace_reachability_as_not_applicable() -> None:
    source = UnitySchema(
        project_name="AirspaceValidation",
        company_name="Test",
        description="An aircraft flies through an airspace volume.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="fly",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerAircraft", kind="vehicle", tag="Player"),
            EntitySpec(name="EnemyAircraft", kind="enemy", placement="scatter"),
        ],
    )
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    fusion = {
        "format": "visual-reasoning-compiler/spatial-fusion/v1",
        "status": "complete",
        "inputs": {"schema_sha256": realized["inputs"]["schema_sha256"]},
        "bindings": [],
    }
    realized = realize_spatial_layout(
        source.model_copy(deep=True),
        spatial_fusion=fusion,
    )

    report = validate_spatial_layout(
        source,
        world_layout=realized,
        spatial_roles=roles,
        spatial_fusion=fusion,
    )

    reachability = next(
        check for check in report["checks"]
        if check["check"] == "applicable_reachability"
    )
    assert report["status"] == "pass"
    assert reachability["status"] == "pass"
    assert reachability["detail"].startswith("not applicable:")
    assert not report["warnings"]


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


def test_instance_footprint_detail_names_the_group_points_and_distances() -> None:
    """A within-group overlap names its group, its closest pair, the distance
    and the required minimum. Run 01 of the 2026-11-17 neon batch needed a
    replay script to learn that much."""
    source = UnitySchema(
        project_name="InstanceDetail",
        company_name="Test",
        description="A ground arena.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="walk",
        scenes=["Main"],
        entities=[
            EntitySpec(name="Player", kind="player", tag="Player"),
            EntitySpec(name="TunnelWalls", kind="structure", placement="ring",
                       count=2, scale=4.0),
        ],
    )
    roles = derive_spatial_roles(source)
    realized = realize_spatial_layout(source.model_copy(deep=True))
    walls = next(row for row in realized["layout"]["entities"]
                 if row["entity"] == "TunnelWalls")
    x, y, z = walls["instance_positions"][0]
    walls["instance_positions"] = [[x, y, z], [x + 4.0, y, z]]

    report = validate_spatial_layout(
        source, world_layout=realized, spatial_roles=roles
    )
    check = next(
        row for row in report["checks"] if row["check"] == "instance_footprints"
    )

    assert check["status"] == "fail"
    first = "[" + ", ".join(f"{value:.2f}" for value in (x, y, z)) + "]"
    second = "[" + ", ".join(f"{value:.2f}" for value in (x + 4.0, y, z)) + "]"
    assert check["detail"] == (
        "1 within-group instance overlap(s): TunnelWalls (1 pair(s)) closest "
        f"{first} x {second} 4.00 m apart < 4.60 m required"
    )


def test_named_detail_counts_every_finding_but_names_only_the_first() -> None:
    findings = [f"pair{index}" for index in range(_MAX_NAMED_FINDINGS + 2)]
    detail = _named_detail("10 overlap(s)", findings)

    assert detail.startswith("10 overlap(s): pair0; pair1")
    assert f"pair{_MAX_NAMED_FINDINGS - 1}" in detail
    assert f"pair{_MAX_NAMED_FINDINGS}" not in detail
    assert detail.endswith("; +2 more")
    assert _named_detail("0 overlap(s)", []) == "0 overlap(s)"
