"""Step-3 fill hardening: the pre-validation clamp + the schema-fill re-roll.

Regression cover for a live crash (Gigantinut, a free-form racing brief):
schema-fill returned 155 `scripts` against a cap of 24, `model_validate` raised
`too_long`, and the whole run died at Step 3 — after both LLM calls, taking the
concept with it. The brief had no SCRIPTS section, so no checklist was involved;
the model simply kept writing script objects.

Two guarantees are under test:
  1. an over-cap list costs its own overflow, not the run — and the entries that
     did not survive are NAMED, never silently dropped;
  2. a fill the schema rejects outright gets one colder re-roll, the hardening
     src/pipeline.py and src/micro_pipeline.py have always had and Unity lacked.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.unity_pipeline import UnityPipeline
from src.unity_prompts import UNITY_CONCEPT_SYSTEM, UNITY_SCHEMA_FILL_SYSTEM
from src.unity_schema import UnitySchema
from src.unity_schema_clamp import (
    capped_fields, clamp_capped_lists, render_cap_reminder,
)


def _script(name: str) -> dict:
    return {"name": name, "type": "custom", "description": f"{name} system"}


def _fill(n: int, prefix: str = "System") -> list[dict]:
    """n scripts with distinct names — a model that keeps going, not one stuck."""
    return [_script(f"{prefix}{i:03d}") for i in range(n)]


# ══════════════════════════════════════════════════════════════════════════════
# Caps are read from the schema, never restated
# ══════════════════════════════════════════════════════════════════════════════

def test_caps_come_from_the_schema_itself():
    """A cap edited in unity_schema.py must move the clamp with it — a second
    hardcoded copy is how the two drift apart and the crash comes back."""
    caps = capped_fields()
    assert caps["scripts"] == 24
    assert caps["entities"] == 40
    assert caps["packages"] == 20
    assert caps["scene_specs"] == 8


def test_retry_reminder_states_the_enforced_numbers():
    reminder = render_cap_reminder()
    assert 'at most 24 "scripts"' in reminder
    assert 'at most 40 "entities"' in reminder


# ══════════════════════════════════════════════════════════════════════════════
# The clamp
# ══════════════════════════════════════════════════════════════════════════════

def test_the_gigantinut_fill_validates_after_clamping():
    """155 scripts, the exact live failure: clamped, then accepted."""
    raw = {"project_name": "Gigantinut", "game_type": "3D", "platform": "PC",
           "render_pipeline": "URP", "scripts": _fill(155)}
    msgs = clamp_capped_lists(raw)

    assert len(raw["scripts"]) == 24
    assert msgs, "a clamp this large must report itself"
    schema = UnitySchema.model_validate(raw)   # no ValidationError
    assert len(schema.scripts) == 24


def test_a_repetition_loop_collapses_to_one_entry():
    """The runaway shape: one object written over and over. Deduping by name
    leaves a list that fits without dropping any distinct system."""
    raw = {"scripts": _fill(5) + [_script("System000")] * 150}
    clamp_capped_lists(raw)

    names = [s["name"] for s in raw["scripts"]]
    assert names == [f"System{i:03d}" for i in range(5)]
    assert raw["meta"]["schema_clamp"]["scripts"]["dropped"] == []
    assert raw["meta"]["schema_clamp"]["scripts"]["duplicates_removed"] == {
        "System000": 150}


def test_every_dropped_entry_is_named_in_the_record():
    """Abstain loudly: shipping 24 of 30 systems is fine only if the run says
    which 6 are not in the game."""
    raw = {"scripts": _fill(30)}
    msgs = clamp_capped_lists(raw)

    dropped = raw["meta"]["schema_clamp"]["scripts"]["dropped"]
    assert dropped == [f"System{i:03d}" for i in range(24, 30)]
    assert "DROPPED 6" in msgs[0]
    assert "System029" in msgs[0]


def test_console_message_stays_short_but_the_record_is_complete():
    """131 names on one console line buries the rest of the run log; the meta
    record still carries all of them."""
    raw = {"scripts": _fill(155)}
    msgs = clamp_capped_lists(raw)

    assert "+119 more" in msgs[0]
    assert len(raw["meta"]["schema_clamp"]["scripts"]["dropped"]) == 131


def test_a_list_inside_its_cap_is_left_exactly_as_written():
    """Duplicate names below the cap belong to the programmatic fixer, not here."""
    scripts = _fill(20) + [_script("System000")]
    raw = {"scripts": list(scripts)}
    assert clamp_capped_lists(raw) == []
    assert raw["scripts"] == scripts
    assert "meta" not in raw


def test_entities_scene_specs_and_packages_are_clamped_too():
    raw = {
        "entities":    [{"name": f"E{i}"} for i in range(60)],
        "scene_specs": [{"name": f"S{i}"} for i in range(12)],
        "packages":    [{"description": f"package {i}"} for i in range(25)],
    }
    clamp_capped_lists(raw)

    assert len(raw["entities"]) == 40
    assert len(raw["scene_specs"]) == 8
    assert len(raw["packages"]) == 20


def test_packages_identify_on_package_id_before_description():
    """Packages have no name; two entries resolving to one Unity package are the
    same dependency however differently the model described them."""
    raw = {"packages": [{"package_id": "com.unity.cinemachine",
                         "description": "cinemachine"}] * 3
                       + [{"description": f"package {i}"} for i in range(22)]}
    clamp_capped_lists(raw)

    ids = [p.get("package_id") for p in raw["packages"]]
    assert ids.count("com.unity.cinemachine") == 1


def test_unnamed_entries_ride_through_to_pydantic():
    """An entry this pass cannot identify is not one it may judge — dropping it
    for having no name would hide a real schema error behind a clamp."""
    raw = {"scripts": [{"type": "custom", "description": "no name"}] * 3
                      + _fill(30)}
    clamp_capped_lists(raw)
    assert sum(1 for s in raw["scripts"] if "name" not in s) == 3


def test_existing_meta_survives_the_stamp():
    raw = {"meta": {"user_prompt": "a giant squirrel race"}, "scripts": _fill(30)}
    clamp_capped_lists(raw)
    assert raw["meta"]["user_prompt"] == "a giant squirrel race"
    assert "schema_clamp" in raw["meta"]


def test_clamp_is_deterministic():
    a = {"scripts": _fill(155) + [_script("System001")] * 40}
    b = {"scripts": _fill(155) + [_script("System001")] * 40}
    assert clamp_capped_lists(a) == clamp_capped_lists(b)
    assert a == b


# ══════════════════════════════════════════════════════════════════════════════
# The pipeline: clamp in place, and one re-roll on a bad fill
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def clamp_pipeline_config(tmp_path) -> dict:
    return {
        "lm_studio": {
            "base_url": "http://localhost:1234/v1", "model": "test-model",
            "temperature": 0.7, "max_tokens": 8192, "timeout": 30.0,
            "concept_temperature": 0.85, "schema_temperature": 0.2,
        },
        "unity_pipeline": {
            "lookups_dir": "lookups", "unity_template_dir": "templates/unity",
            "unity_output_dir": str(tmp_path),
        },
        "logging": {"show_prompts": False},
    }


_CONCEPT = "A chaotic kart racer menaced by a fifty-foot squirrel. PC, 3D."


def _chat_script(*fills: str):
    """A fake llm.chat that answers by ROLE, not by call order.

    Step 3 is not the only chat in a run — the Sketchfab query translator gets
    one too — so counting raw calls measures the wrong thing. Routing on the
    system prompt keeps these tests about schema fills.
    """
    remaining = list(fills)

    def _chat(*, system: str = "", user: str = "", **_kw) -> str:
        if system == UNITY_SCHEMA_FILL_SYSTEM:
            return remaining.pop(0)
        if system == UNITY_CONCEPT_SYSTEM:
            return _CONCEPT
        return ""

    return _chat


def _fill_calls(chat_mock) -> list:
    return [c for c in chat_mock.call_args_list
            if c.kwargs.get("system") == UNITY_SCHEMA_FILL_SYSTEM]


def _schema_json(**overrides) -> str:
    base = {
        "project_name": "Gigantinut", "company_name": "TestCo",
        "game_type": "3D", "platform": "PC", "render_pipeline": "URP",
        "unity_version": "6000.0", "color_space": "Linear",
        "description": "A kart racer with a giant squirrel",
        "features": ["input_system"], "scenes": ["Main", "Menu"],
        "scripts": [
            {"name": "GameManager", "type": "game_manager", "description": "GM"},
            {"name": "PlayerController", "type": "player", "description": "Driving"},
        ],
        "packages": [{"description": "universal render pipeline"}],
    }
    base.update(overrides)
    return json.dumps(base)


def test_a_runaway_fill_no_longer_kills_the_run(clamp_pipeline_config):
    """The Gigantinut crash, end to end: 155 scripts now cost 131 scripts, not
    the project — and cost no extra LLM call, because the clamp is deterministic."""
    pipeline = UnityPipeline(clamp_pipeline_config)
    runaway = [{"name": "GameManager", "type": "game_manager", "description": "GM"},
               {"name": "PlayerController", "type": "player", "description": "Drive"}]
    runaway += _fill(153)
    pipeline.llm.chat = MagicMock(
        side_effect=_chat_script(_schema_json(scripts=runaway)))

    root = pipeline.run("a giant squirrel wrecks a kart race")

    assert root.exists()
    assert len(_fill_calls(pipeline.llm.chat)) == 1


def test_a_rejected_fill_gets_one_colder_re_roll(clamp_pipeline_config):
    pipeline = UnityPipeline(clamp_pipeline_config)
    pipeline.llm.chat = MagicMock(
        side_effect=_chat_script("sorry, here is no JSON at all", _schema_json()))

    root = pipeline.run("a giant squirrel wrecks a kart race")

    assert root.exists()
    first, second = _fill_calls(pipeline.llm.chat)
    # The re-roll must be colder than the first attempt and must carry the nudge.
    assert second.kwargs["temperature"] < first.kwargs["temperature"]
    assert "REMINDER" in second.kwargs["user"]
    assert 'at most 24 "scripts"' in second.kwargs["user"]


def test_a_schema_valid_json_but_invalid_fill_also_gets_one_re_roll(clamp_pipeline_config):
    """The re-roll's except clause names two exceptions: ValueError, from
    extract_json on unparsable text (the case above), and pydantic's
    ValidationError, from JSON that parses fine but fails the schema itself —
    e.g. a required field the model just left out. Both must retry the same
    way; a fill that is valid JSON is not the same thing as a valid schema."""
    bad_json = json.dumps({
        "project_name": "Gigantinut", "game_type": "3D", "platform": "PC",
        # render_pipeline omitted — required, no default: model_validate raises
        # ValidationError, not extract_json raising ValueError.
    })
    pipeline = UnityPipeline(clamp_pipeline_config)
    pipeline.llm.chat = MagicMock(
        side_effect=_chat_script(bad_json, _schema_json()))

    root = pipeline.run("a giant squirrel wrecks a kart race")

    assert root.exists()
    first, second = _fill_calls(pipeline.llm.chat)
    assert second.kwargs["temperature"] < first.kwargs["temperature"]
    assert "REMINDER" in second.kwargs["user"]


def test_two_failed_fills_still_fail_loudly(clamp_pipeline_config):
    """The clamp is not a licence to limp on: an unusable fill twice over is a
    hard error naming the last one, not a default schema."""
    pipeline = UnityPipeline(clamp_pipeline_config)
    pipeline.llm.chat = MagicMock(
        side_effect=_chat_script("no json", "still no json"))

    with pytest.raises(RuntimeError, match="schema fill failed after 2 attempts"):
        pipeline.run("a giant squirrel wrecks a kart race")
