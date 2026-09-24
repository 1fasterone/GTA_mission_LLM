"""Focused tests for deterministic Slice 6.2 schema spatial roles."""

from __future__ import annotations

import json
from pathlib import Path

from src.unity_schema import EntitySpec, MissionObjective, MissionSpec, UnitySchema
from src.unity_spatial_roles import (
    ROLE_ABSTAIN,
    ROLE_AIRBORNE_HAZARD,
    ROLE_CHECKPOINT,
    ROLE_HAZARD,
    ROLE_NAVIGATION_MARKER,
    ROLE_PLAYER_PATH,
    ROLE_SCENERY,
    ROLE_UI_ONLY,
    SPATIAL_ROLES_FORMAT,
    SPATIAL_ROLES_MANIFEST_FORMAT,
    derive_spatial_roles,
    write_spatial_roles,
)


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="RallyBoat",
        company_name="Test",
        description="A river route drive game with hazards and checkpoints.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=[
            EntitySpec(
                name="PlayerBoat", kind="vehicle", tag="Player",
                model_hint="speedboat", placement="single",
            ),
            EntitySpec(
                name="RiverRoute", kind="river", placement="path",
                path_points=[[0, 0, 0], [0, 0, 30]], path_width=8,
            ),
            EntitySpec(
                name="CheckpointArch", kind="checkpoint", placement="single",
            ),
            EntitySpec(
                name="RiverMines", kind="hazard", placement="scatter",
                count=4,
            ),
            EntitySpec(
                name="SkyMines", kind="hazard", model_hint="airborne mines",
                placement="scatter", count=2, elevation=12,
            ),
            EntitySpec(
                name="DirectionBuoys", kind="buoy", placement="line", count=3,
            ),
            EntitySpec(
                name="PineTrees", kind="prop", model_hint="pine tree",
                placement="scatter", count=6,
            ),
            EntitySpec(name="UnclearThing", kind="thing", placement="single"),
        ],
        missions=[MissionSpec(
            id="race",
            title="Race",
            objectives=[MissionObjective(
                id="reach_arch", description="Reach the checkpoint arch.",
                type="reach", target_ref="CheckpointArch",
            )],
        )],
        meta={
            "concept": (
                "The PlayerBoat follows the RiverRoute toward the "
                "CheckpointArch. SkyMines hang above the water."
            ),
        },
    )


def _roles(table: dict) -> dict[str, str]:
    return {row["entity"]: row["role"] for row in table["role_table"]}


def test_roles_are_closed_and_use_declared_schema_evidence() -> None:
    schema = _schema()
    before = schema.model_dump(mode="json")

    table = derive_spatial_roles(schema)

    assert table["format"] == SPATIAL_ROLES_FORMAT
    assert table["authority"]["llm_called_here"] is False
    assert table["authority"]["image_spatial_evidence_used_here"] is False
    assert {row["role"] for row in table["role_table"]} <= set(table["role_vocabulary"])
    assert _roles(table) == {
        "PlayerBoat": ROLE_PLAYER_PATH,
        "RiverRoute": ROLE_PLAYER_PATH,
        "CheckpointArch": ROLE_CHECKPOINT,
        "RiverMines": ROLE_HAZARD,
        "SkyMines": ROLE_AIRBORNE_HAZARD,
        "DirectionBuoys": ROLE_NAVIGATION_MARKER,
        "PineTrees": ROLE_SCENERY,
        "UnclearThing": ROLE_ABSTAIN,
    }
    assert schema.model_dump(mode="json") == before


def test_checkpoint_adjacency_is_required_but_does_not_create_entities() -> None:
    schema = _schema()
    schema.missions[0].objectives[0].target_ref = "ImageOnlyArch"

    table = derive_spatial_roles(schema)

    objective = table["objectives"][0]
    assert objective["status"] == "abstain"
    assert objective["target_entity"] is None
    assert len(table["role_table"]) == len(schema.entities)
    checkpoint = next(
        row for row in table["adjacency_requirements"]
        if row["entity"] == "CheckpointArch"
    )
    assert checkpoint["kind"] == "on_player_path"
    assert checkpoint["status"] == "bound"
    assert checkpoint["player_path_entities"] == ["PlayerBoat", "RiverRoute"]


def test_non_embodied_ui_roles_abstain_for_unproven_world_content() -> None:
    schema = UnitySchema(
        project_name="Board",
        game_type="2D",
        platform="pc",
        render_pipeline="URP",
        locomotion="none",
        scenes=["Main"],
        entities=[
            EntitySpec(name="SubmitButton", kind="button"),
            EntitySpec(name="BoardPanel", kind="panel"),
            EntitySpec(name="UnclearProp", kind="prop"),
        ],
    )

    table = derive_spatial_roles(schema)

    assert _roles(table) == {
        "SubmitButton": ROLE_UI_ONLY,
        "BoardPanel": ROLE_UI_ONLY,
        "UnclearProp": ROLE_ABSTAIN,
    }
    assert table["inputs"]["archetype"]["medium"] == "screen"


def test_role_table_and_manifest_are_hash_linked(tmp_path: Path) -> None:
    schema = _schema()
    schema_path = tmp_path / "RallyBoat.schema.json"
    output = tmp_path / "RallyBoat.spatial-roles.json"
    manifest = tmp_path / "RallyBoat.spatial-roles.manifest.json"
    schema_path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")

    result = write_spatial_roles(schema_path, output, manifest)

    assert result == manifest
    table = json.loads(output.read_text(encoding="utf-8"))
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert table["format"] == SPATIAL_ROLES_FORMAT
    assert manifest_data["format"] == SPATIAL_ROLES_MANIFEST_FORMAT
    assert manifest_data["stage"] == "6.2_schema_spatial_roles"
    assert manifest_data["outputs"]["spatial_roles"]["sha256"]
    assert manifest_data["inputs"]["schema"]["sha256"]
