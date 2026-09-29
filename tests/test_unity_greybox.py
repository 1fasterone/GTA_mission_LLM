"""Deterministic dungeon greybox IR, spatial authority and Step 7 gates."""

from __future__ import annotations

import json

from src.unity_greybox import (
    GreyboxConstraints,
    GenerationMode,
    generate_dungeon_greybox,
    greybox_plus_ir_for_schema,
    greybox_ir_for_schema,
    stamp_greybox_plus_reference,
    unity_greybox_asset_payload,
    validate_coordinates,
    validate_dungeon_greybox,
    validate_greybox_plus_ir,
    validate_topology,
)
from src.unity_playability import evaluate
from src.unity_schema import UnitySchema, UnityScript


def _greybox_schema() -> UnitySchema:
    return UnitySchema(
        project_name="GreyboxAcceptance",
        company_name="Test",
        description="deterministic greybox",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="none",
        scenes=["Main"],
        scripts=[UnityScript(
            name="GreyboxLevelGenerator",
            type="greybox_level_generator",
            description="ProBuilder dungeon greybox",
        )],
        meta={"greybox": {"seed": 17, "theme": "ignored", "twist_tags": ["ignored"]}},
    )


def _greybox_plus_schema(tmp_path) -> UnitySchema:
    schema = _greybox_schema()
    reference = tmp_path / "industrial_yard_reference.png"
    reference.write_bytes(b"deterministic visual evidence")
    stamp_greybox_plus_reference(
        schema, reference, "tank zone; pipe corridor; elevated catwalk"
    )
    return schema


def test_same_seed_produces_byte_stable_ir_and_geometry_only_clears_theme() -> None:
    first = generate_dungeon_greybox(
        17, mode=GenerationMode.GEOMETRY_ONLY,
        theme="thematic input", twist_tags=["twist"], biome_id="biome",
    )
    second = generate_dungeon_greybox(17)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.theme == "none"
    assert first.twist_tags == []
    assert first.biome_id == ""
    assert first.hierarchy.root == "Dungeon_Greybox"
    assert first.hierarchy.children["Dungeon_Greybox"] == [
        "Geometry", "Rooms", "Corridors", "Elevation", "Markers"
    ]
    unity_payload = unity_greybox_asset_payload(first)
    assert unity_payload["mode"] == 0
    assert unity_payload["rooms"][0]["gridPosition"] == {"x": 0, "y": 0, "z": 0}
    assert set(unity_payload["rooms"][0]["worldPosition"]) == {"x", "y", "z"}
    assert unity_payload["constraints"]["maxBranching"] == 3
    assert {primitive["parent"] for primitive in unity_payload["unityPrimitives"]} >= {
        "Geometry", "Corridors"
    }


def test_forced_loop_and_elevation_stay_valid() -> None:
    ir = generate_dungeon_greybox(
        6,
        constraints=GreyboxConstraints(loop_frequency=1.0, elevation_frequency=1.0),
    )
    assert ir.loop_count == 1
    assert ir.elevation_changes
    assert validate_dungeon_greybox(ir).accepted


def test_topology_and_coordinate_validators_reject_bad_ir() -> None:
    ir = generate_dungeon_greybox(1)
    bad = ir.model_copy(deep=True)
    bad.rooms[1].world_position[0] += 0.000001
    bad.boss_depth = 1

    assert validate_coordinates(bad)
    assert validate_topology(bad)
    report = validate_dungeon_greybox(bad)
    assert not report.accepted
    assert report.coordinate_errors
    assert report.topology_errors


def test_schema_greybox_becomes_the_spatial_authority() -> None:
    schema = _greybox_schema()
    ir = greybox_ir_for_schema(schema)
    assert ir.theme == "none"
    assert schema.meta["greybox_ir"]["theme"] == "none"

    schema.entities = []
    report = evaluate(schema)
    assert report.checks["greybox_topology"] is True
    assert report.checks["greybox_coordinates"] is True


def test_missing_greybox_acceptance_evidence_is_an_error() -> None:
    schema = _greybox_schema()
    report = evaluate(schema)
    assert report.checks["greybox_topology"] is False
    assert report.checks["greybox_coordinates"] is False
    assert {finding.check for finding in report.errors} >= {
        "greybox_topology", "greybox_coordinates"
    }


def test_greybox_plus_ir_is_image_bound_and_rejects_transform_or_tree_drift(
    tmp_path,
) -> None:
    schema = _greybox_plus_schema(tmp_path)
    plus = greybox_plus_ir_for_schema(schema)

    assert plus.accepted is True
    assert plus.theme == "none"
    assert plus.hierarchy.root == "GreyboxPlus_Level"
    assert plus.reference.image_name == "industrial_yard_reference.png"
    assert validate_greybox_plus_ir(plus).accepted
    assert {primitive.tier.value for primitive in plus.primitives} >= {
        "macro", "meso", "micro"
    }

    moved = plus.model_copy(deep=True)
    moved.primitives[0].world_position[0] += 0.25
    assert not validate_greybox_plus_ir(moved).accepted

    changed_tree = plus.model_copy(deep=True)
    changed_tree.hierarchy.children["Geometry"].append("Unexpected")
    assert not validate_greybox_plus_ir(changed_tree).accepted


def test_greybox_plus_generator_emits_planning_sidecar_without_art_or_gameplay(
    tmp_path,
) -> None:
    from src.unity_generator import UnityProjectGenerator

    schema = _greybox_plus_schema(tmp_path)
    files = UnityProjectGenerator().generate(schema, {})
    sidecar_path = "GreyboxAcceptance/Assets/Greybox/GreyboxAcceptance.greybox_plus.json"

    assert sidecar_path in files
    sidecar = json.loads(files[sidecar_path])
    assert sidecar == schema.meta["greybox_plus_ir"]
    assert sidecar["accepted"] is True
    assert sidecar["theme"] == "none"
    assert "GreyboxAcceptance/Assets/Scenes/Main.unity" not in files
    assert not any(
        segment in path
        for path in files
        for segment in ("/Assets/Materials/", "/Assets/Textures/", "/Assets/Art/")
    )
    assert not any(
        forbidden in content
        for content in files.values()
        for forbidden in ("GameManager", "PlayerController", "EntitySpawner")
    )


def test_generator_replaces_old_editor_level_with_greybox_tool(
    full_3d_schema: UnitySchema, tmp_path
) -> None:
    from src.unity_generator import UnityProjectGenerator
    from src.csharp_syntax_gate import check_files

    full_3d_schema.scripts = [UnityScript(
        name="LevelGenerator",
        type="level_generator",
        description="legacy request mapped to the deterministic greybox",
    )]
    full_3d_schema.meta = {"greybox": {"seed": 5, "theme": "ignored"}}
    files = UnityProjectGenerator().generate(full_3d_schema, {})
    editor = files["FullProject/Assets/Editor/GreyboxLevelGenerator.cs"]
    assert "public static class GreyboxLevelGenerator" in editor
    assert "Dungeon_Greybox" in editor
    assert "int seed" in editor
    assert "LevelGeometry" not in editor
    assert "room_01" not in editor
    assert "AddRoom" not in editor
    assert "Materials" not in editor
    assert "FullProject/Assets/Greybox/FullProject.greybox.json" in files
    assert "FullProject/Assets/Greybox/FullProject.greybox.unity.json" in files

    editor_path = tmp_path / "GreyboxLevelGenerator.cs"
    editor_path.write_text(editor, encoding="utf-8")
    assert check_files([editor_path]).ok
