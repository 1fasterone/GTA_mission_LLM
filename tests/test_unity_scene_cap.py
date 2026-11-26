"""One scene cap (SCENE_CAP), loud everywhere.

Live subject: runs 03 and 10 of the 2026-11-14 neon_fugitive E2E batch. The
brief names 9 scenes. The Pipeline LLM wrote all 9, the old cap of 8 dropped the
9th (BossArena) from scene_specs at the Step 3 clamp and from scenes at
_programmatic_fix, and the two entities only BossArena listed spawned nowhere.

tests/fixtures/neon_fugitive_run10_fill_raw.json is the verbatim Step 3 response
(declared transformation in tests/fixtures/README.md). The unit cases are
engineer-written test data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.unity_schema_clamp as clamp_mod
from src.scene_graph import build_scene_graph
from src.unity_pipeline import (
    _check_consistency,
    _infer_mission_scenes,
    _programmatic_fix,
    _reconcile_scene_specs,
)
from src.unity_prompts import UNITY_SCHEMA_FILL_USER
from src.unity_scene_membership import hosting_scenes
from src.unity_schema import SCENE_CAP, EntitySpec, SceneSpec, UnitySchema
from src.unity_schema_clamp import (
    clamp_capped_lists,
    clamp_scene_names,
    scene_cap_drops,
    scene_cap_warnings,
)
from src.unity_spatial_roles import derive_spatial_roles

_FIXTURES = Path(__file__).parent / "fixtures"
_BOSS_ONLY = ["PlatformTraps", "EnergyGenerators"]


def _raw_run10() -> dict:
    return json.loads((_FIXTURES / "neon_fugitive_run10_fill_raw.json")
                      .read_text(encoding="utf-8"))


def _after_fill(raw: dict) -> UnitySchema:
    """The live Step 3 tail: clamp, then validate."""
    clamp_capped_lists(raw)
    return UnitySchema.model_validate(raw)


def _step5(schema: UnitySchema) -> UnitySchema:
    schema = _programmatic_fix(schema)
    schema, _ = _check_consistency(schema, reconcile_scenes=False)
    resolved, _ = _infer_mission_scenes(schema)
    schema.meta["mission_scenes"] = resolved
    schema, _ = _reconcile_scene_specs(schema)
    return schema


def _load(name: str) -> UnitySchema:
    return UnitySchema.model_validate(
        json.loads((_FIXTURES / name).read_text(encoding="utf-8")))


def _schema(scenes: list[str], specs: list[SceneSpec] | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="CapTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        scenes=scenes, scene_specs=specs or [],
        entities=[EntitySpec(name="Player", kind="player", tag="Player")])


# ── The live run ──────────────────────────────────────────────────────────────

def test_run10_raw_fill_declares_nine_scenes_with_the_boss_arena_last():
    """Guards the regression subject."""
    raw = _raw_run10()
    assert len(raw["scene_specs"]) == 9
    assert raw["scene_specs"][-1]["name"] == "BossArena"
    assert raw["scene_specs"][-1]["entities"] == _BOSS_ONLY


def test_run10_raw_fill_keeps_the_boss_arena_and_hosts_both_entities():
    raw = _raw_run10()
    msgs = clamp_capped_lists(raw)
    assert msgs == []                                  # 9 specs fit inside 12
    schema = _step5(UnitySchema.model_validate(raw))
    assert "BossArena" in schema.scenes
    assert scene_cap_warnings(schema) == []
    for name in _BOSS_ONLY:
        assert hosting_scenes(schema, name) == ["BossArena"]
    assert schema.meta["scene_membership"]["abstentions"] == []
    graph = build_scene_graph(schema, derive_spatial_roles(schema))
    assert graph["omitted_entities"] == []


def test_run10_at_the_old_cap_the_abstention_names_the_cap(monkeypatch):
    """Replays e7b11e0's cap of 8 on the same raw fill. The abstention says the
    cap took the only scene that listed each entity, and still places nothing."""
    original = clamp_mod._cap
    monkeypatch.setattr(clamp_mod, "_cap",
                        lambda f: 8 if f == "scene_specs" else original(f))
    monkeypatch.setattr(clamp_mod, "SCENE_CAP", 8)

    raw = _raw_run10()
    clamp_capped_lists(raw)
    record = raw["meta"]["schema_clamp"]["scene_specs"]
    assert record["dropped"] == ["BossArena"]
    assert record["dropped_entities"] == {"BossArena": _BOSS_ONLY}

    schema = _step5(UnitySchema.model_validate(raw))
    assert "BossArena" not in schema.scenes
    warning = scene_cap_warnings(schema)
    assert len(warning) == 1 and "DROPPED 1: BossArena" in warning[0]
    assert schema.meta["schema_clamp"]["scene_specs"]["dropped"] == ["BossArena"]
    rows = {r["entity"]: r for r in schema.meta["scene_membership"]["abstentions"]}
    assert set(rows) == set(_BOSS_ONLY)
    for name, row in rows.items():
        assert hosting_scenes(schema, name) == []
        assert row["missing"][0] == (
            "scene cap: Step 3 listed it only in ['BossArena'], which the scene "
            "cap (8) dropped (meta.schema_clamp.scene_specs)")
        assert row["remedy"].startswith("raise SCENE_CAP")


@pytest.mark.parametrize("run, orphans", [
    ("03", {"PlatformRings", "EnergyGenerators"}),
    ("10", {"PlatformTraps", "EnergyGenerators"}),
])
def test_saved_dumps_name_the_cap_as_a_possible_cause(run, orphans):
    """The dumps were clamped by the old code, whose record names the dropped
    scene but not what it listed. The abstention says exactly that."""
    schema = _load(f"neon_fugitive_run{run}_failed_llm.schema.json")
    resolved, _ = _infer_mission_scenes(schema)
    schema.meta["mission_scenes"] = resolved
    schema, _ = _reconcile_scene_specs(schema)
    rows = {r["entity"]: r for r in schema.meta["scene_membership"]["abstentions"]}
    assert set(rows) == orphans
    for row in rows.values():
        first = row["missing"][0]
        assert first.startswith("scene cap: the scene cap (8) dropped ['BossArena']")
        assert "predates dropped_entities" in first and "They may have" in first
        assert "so that BossArena fits" in row["remedy"]
        assert "scene_specs[].entities" in row["remedy"]


# ── The fixer's own cap ──────────────────────────────────────────────────────

def test_a_ninth_scene_is_kept():
    names = ["Main"] + [f"District{i}" for i in range(1, 9)]
    schema = _programmatic_fix(_schema(list(names)))
    assert schema.scenes == names
    assert scene_cap_warnings(schema) == []


def test_scenes_over_the_cap_are_dropped_by_name_with_what_they_hosted():
    names = ["Main"] + [f"Zone{i:02d}" for i in range(1, SCENE_CAP + 3)]
    over = names[SCENE_CAP:]
    specs = [SceneSpec(name=over[0], entities=["Player", "Boss"])]
    schema = _programmatic_fix(_schema(list(names), specs))
    assert schema.scenes == names[:SCENE_CAP]
    record = schema.meta["schema_clamp"]["scenes"]
    assert record["dropped"] == over
    assert record["cap"] == SCENE_CAP and record["received"] == len(names)
    assert record["dropped_entities"] == {over[0]: ["Player", "Boss"]}
    [warning] = scene_cap_warnings(schema)
    assert warning.startswith("[scene.cap] ") and ", ".join(over) in warning
    assert "SCENE_CAP" in warning
    assert [row["scene"] for row in scene_cap_drops(schema)] == over


def test_main_past_the_cap_is_moved_first_not_reported_dropped():
    names = [f"Zone{i:02d}" for i in range(1, SCENE_CAP + 1)] + ["Main"]
    schema = _programmatic_fix(_schema(list(names)))
    assert schema.scenes[0] == "Main"
    assert len(schema.scenes) == SCENE_CAP + 1      # the hub is not counted
    assert "schema_clamp" not in schema.meta


def test_the_step3_clamp_record_survives_the_scene_cap_record():
    schema = _schema(["Main"] + [f"Z{i}" for i in range(SCENE_CAP + 1)])
    schema.meta["schema_clamp"] = {"scripts": {"cap": 24, "dropped": ["X"]}}
    clamp_scene_names(schema)
    assert set(schema.meta["schema_clamp"]) == {"scripts", "scenes"}


def test_the_step3_prompt_states_the_enforced_scene_cap():
    prompt = UNITY_SCHEMA_FILL_USER.format(concept="c", schema_hints="",
                                           multimodal_block="", brief_block="")
    assert f"- scenes:            max {SCENE_CAP} scene names" in prompt
    assert f"- scene_specs:       max {SCENE_CAP} " in prompt
    assert "{scene_cap}" not in prompt
    assert "max 8 scene names" not in prompt
