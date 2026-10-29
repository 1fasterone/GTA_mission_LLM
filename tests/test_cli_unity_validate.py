"""
`unity-validate --compose` — the no-LLM schema replay path (cli/unity_project.py).

Live regression: this command's own `composer.emit(...)` call never passed
`escort_followed_entities`, unlike unity_pipeline.py's `run()` (the fresh
`unity-create` path), which always has. A schema with an `escort` objective
recompiled through `unity-validate --compose` silently lost SEAM 8b — the
objective reported "no event source" and could never advance, even though
the SAME schema replayed correctly through a live `unity-create` run.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

import json

import pytest
import yaml

from src.unity_escort_follow import resolve_escort_follow
from src.unity_projectile_contract import apply_physical_projectile_contract
from src.unity_schema import EntitySpec, MissionObjective, MissionSpec, UnitySchema


def _escort_schema() -> UnitySchema:
    schema = UnitySchema(
        project_name="EscortRegressionCheck", company_name="TestCo", game_type="3D",
        platform="PC", render_pipeline="URP", unity_version="6000.0",
        color_space="Linear", description="test", features=[], packages=[],
        scenes=["Main"],
        entities=[EntitySpec(name="Buddy", kind="npc", count=1)],
        missions=[MissionSpec(id="m1", title="Escort Run", objectives=[
            MissionObjective(id="escort_buddy", description="Escort Buddy to safety",
                             type="escort", target_ref="Buddy"),
        ])],
    )
    # Same Step-5 pass a real unity-create run applies before ever saving a
    # schema.json — the saved-schema contract unity-validate replays against.
    resolve_escort_follow(schema)
    return schema


def _minimal_config(tmp_path) -> str:
    """Just enough for unity_validate() to run compose() without a live
    asset catalog: assets.strict=False accepts the logged prop fallback
    instead of hard-failing on an unmatched model."""
    cfg = {
        "unity_pipeline": {
            "lookups_dir": "lookups",
            "unity_template_dir": "templates/unity",
            "unity_output_dir": str(tmp_path / "out"),
            "inject_mcp_bridge": True,
        },
        "assets": {"strict": False},
        "sketchfab": {"enabled": False},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


def test_unity_validate_compose_wires_escort_objective(tmp_path):
    from cli.unity_project import unity_validate

    schema_path = tmp_path / "EscortRegressionCheck.schema.json"
    schema_path.write_text(_escort_schema().model_dump_json(indent=2), encoding="utf-8")

    # Typer's decorated function still takes plain kwargs when called
    # directly, but only if every one of them is passed explicitly — a
    # parameter left at its "default" receives the typer.Option(...)
    # sentinel object instead of the plain value, which is truthy.
    unity_validate(str(schema_path), config=_minimal_config(tmp_path),
                   blender_models=False, compose=True)

    gw = (tmp_path / "out" / "EscortRegressionCheck" / "Assets" / "Micros"
          / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "SEAM 8b: trigger" in gw
    assert 'ReportProgress("escort_buddy")' in gw
    assert "no event source" not in gw
    manifest = json.loads(
        (tmp_path / "out" / "EscortRegressionCheck" / "Project.build.manifest.json")
        .read_text(encoding="utf-8")
    )
    assert manifest["status"] == "replayed_unverified"


def test_unity_validate_compose_replays_physical_projectile_contract(tmp_path):
    """The saved-schema replay must preserve P2 delivery, not silently regress
    back to the ControllerSchema hitscan fallback."""
    from cli.unity_project import unity_validate

    schema = UnitySchema(
        project_name="ProjectileReplay", company_name="TestCo", game_type="3D",
        platform="PC", render_pipeline="URP", unity_version="6000.0",
        color_space="Linear", description="physical projectile test", scenes=["Main"],
    )
    apply_physical_projectile_contract(
        schema, "An FPS with a rifle and pooled physical bullets."
    )
    schema_path = tmp_path / "ProjectileReplay.schema.json"
    schema_path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")

    unity_validate(str(schema_path), config=_minimal_config(tmp_path),
                   blender_models=False, compose=True)

    source = (tmp_path / "out" / "ProjectileReplay" / "Assets" / "Micros"
              / "controller" / "Scripts" / "PlayerController.cs").read_text(encoding="utf-8")
    assert "private ProjectilePool[] _projectilePools;" in source
    assert "ResolveProjectileMuzzle()" in source


def test_frozen_collection_replay_rejects_binding_outside_the_saved_collection(tmp_path):
    from cli.unity_project import _frozen_collection_replay
    from src.unity_sketchfab_collection import frozen_collection_record

    cache_dir = tmp_path / "Models" / "Sketchfab"
    asset = cache_dir / "vehicle" / "rival-truck.fbx"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"frozen-truck")
    collection = frozen_collection_record({"written": [{
        "filename": asset.name,
        "relpath": "vehicle/rival-truck.fbx",
        "root": cache_dir.as_posix(),
        "source": "sketchfab",
        "category": "vehicle",
    }]}, cache_dir=cache_dir)
    schema = _escort_schema()
    schema.meta["llm_enrichment"] = {"sketchfab_collection": collection}
    _cache, locked, _entries = _frozen_collection_replay(schema)
    assert locked == {}

    selection = dict(collection["entries"][0])
    collection["entity_bindings"] = {"Buddy": selection}
    _cache, locked, _entries = _frozen_collection_replay(schema)
    assert locked == {"Buddy": selection}

    collection["entity_bindings"] = {"Buddy": {
        **selection,
        "filename": "replacement.fbx",
    }}
    with pytest.raises(ValueError, match="not in its collection"):
        _frozen_collection_replay(schema)


def test_compose_replay_uses_saved_selection_despite_changed_brief(tmp_path, monkeypatch):
    from cli.unity_project import unity_validate
    from src.unity_compose_selection import persist_selection

    schema = _escort_schema()
    schema.locomotion = "drive"
    persist_selection(schema, "Ride a motorcycle with an inventory.")
    schema_path = tmp_path / "selection.schema.json"
    config = _minimal_config(tmp_path)
    schema_path.write_text(schema.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr("src.unity_compose_selection.select_subsystems",
                        lambda *a, **kw: pytest.fail("replay re-interpreted brief"))
    unity_validate(str(schema_path), config=config, blender_models=False, compose=True)
    micros = tmp_path / "out" / schema.project_name / "Assets" / "Micros"
    before = {str(p.relative_to(micros)): p.read_bytes()
              for p in micros.rglob("*") if p.is_file()}
    schema.meta["user_prompt"] = "No inventory. A hovercraft, climbing and flying enemies."
    schema_path.write_text(schema.model_dump_json(), encoding="utf-8")
    unity_validate(str(schema_path), config=config, blender_models=False, compose=True)
    after = {str(p.relative_to(micros)): p.read_bytes()
             for p in micros.rglob("*") if p.is_file()}
    assert before
    assert before == after
