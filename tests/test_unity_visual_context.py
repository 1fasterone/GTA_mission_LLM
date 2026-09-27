from types import SimpleNamespace

from src.unity_generator import UnityProjectGenerator
from src.unity_model_resolver import ResolvedModel
from src.unity_schema import EntitySpec, UnitySchema
from src.unity_visual_context import render_image_context, render_project_context


def test_image_context_records_grounded_queries_and_appearance(tmp_path):
    inventory = {
        "scene_summary": "A bright basement combat room.",
        "visible_elements": [{
            "label": "robot",
            "category": "character",
            "description": "A white industrial biped with broad shoulders.",
            "model_hint": "industrial robot",
        }],
        "sketchfab_queries": [{
            "label": "robot",
            "query": "industrial robot",
        }],
        "visible_text": [],
        "uncertainties": ["The rear doorway is partly obscured."],
    }

    rendered = render_image_context(
        tmp_path / "board.png", inventory, "A compact robot arena."
    )

    assert "Source board: board.png" in rendered
    assert "robot | category=character" in rendered
    assert "model_hint=industrial robot" in rendered
    assert "image_query=industrial robot" in rendered
    assert "A white industrial biped" in rendered


def test_project_context_records_step_3_query_and_exact_filename():
    schema = SimpleNamespace(
        entities=[SimpleNamespace(
            name="RobotEnemies",
            kind="enemy",
            sketchfab_query="industrial robot enemy",
            model_hint="robot",
        )],
        meta={
            "entity_model_bindings": {
                "RobotEnemies": {
                    "filename": "industrial-robot-8a31.fbx",
                    "resource_path": (
                        "Models/Sketchfab/characters/industrial-robot-8a31"
                    ),
                    "source": "sketchfab",
                }
            }
        },
    )

    rendered = render_project_context("# Visual Scene Context\n", schema)

    assert "query=industrial robot enemy" in rendered
    assert "filename=industrial-robot-8a31.fbx" in rendered
    assert "source=sketchfab" in rendered


def test_generator_stamps_the_exact_filename_used_for_entity_wiring(
    tmp_path, monkeypatch
):
    source = tmp_path / "industrial-robot-source.fbx"
    source.write_bytes(b"FBX")
    model = ResolvedModel(
        kind="asset:industrial-robot-8a31",
        src=str(source),
        dest_rel="Models/Sketchfab/characters/industrial-robot-8a31.fbx",
        resource_path="Models/Sketchfab/characters/industrial-robot-8a31",
        source="sketchfab",
        license="cc0",
    )
    schema = UnitySchema(
        project_name="RobotBasement",
        company_name="TestCo",
        description="A basement robot arena.",
        game_type="3D",
        platform="PC",
        render_pipeline="URP",
        unity_version="6000.0",
        scenes=["Main"],
        entities=[EntitySpec(
            name="RobotEnemies",
            kind="enemy",
            model_hint="industrial robot",
            sketchfab_query="industrial robot enemy",
        )],
    )
    monkeypatch.setattr(
        "src.unity_generator.resolve_default_models", lambda **_kwargs: {}
    )
    monkeypatch.setattr(
        "src.unity_generator.models_for_schema",
        lambda *_args, **_kwargs: {model.kind: model},
    )
    monkeypatch.setattr(
        "src.unity_generator.resolve_entity_assets",
        lambda *_args, **_kwargs: {"RobotEnemies": model.resource_path},
    )

    UnityProjectGenerator()._resolve_all_models(
        schema,
        lookups_dir="lookups",
        model_source_dir=None,
        model_overrides=None,
        composed_units=None,
        strict_assets=True,
        asset_cfg={},
        sketchfab_cfg={"enabled": True},
        llm=None,
    )

    assert schema.meta["entity_model_bindings"]["RobotEnemies"] == {
        "filename": "industrial-robot-8a31.fbx",
        "source_filename": "industrial-robot-source.fbx",
        "resource_path": "Models/Sketchfab/characters/industrial-robot-8a31",
        "source": "sketchfab",
        "license": "cc0",
        "attribution": None,
    }
