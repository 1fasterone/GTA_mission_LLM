"""parse_brief() — the --no-llm deterministic control arm.

No test file exercised this module directly before Solution #3
(docs/COMPILER_AUDIT.md, Gap B): src/unity_briefparse.py's own section
readers (_read_scenes / _read_story_beats / _read_audio / _read_title) were
extracted verbatim into src/unity_concept_scenes.py / _narrative.py / _audio.py
/ _title.py so the new rescue layers and this offline fill share one
implementation. This file anchors that refactor (and the offline arm in
general) against prompts/coldwater_circuit.txt, whose --no-llm output is
independently recorded in docs/LLM_ENRICHMENT_STRATEGY.md §1.6 as the
control-arm side of the project's own A/B study — a schema-fill drift here
would silently invalidate that study's numbers.

Eval discipline note (CLAUDE.md): parse_brief() is the control arm, not a
test of the Pipeline LLM — see the module's own docstring.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.unity_briefparse import BriefParseError, parse_brief

REPO_ROOT = Path(__file__).resolve().parents[1]
_COLDWATER = (REPO_ROOT / "prompts" / "coldwater_circuit.txt").read_text(encoding="utf-8")


def test_coldwater_circuit_matches_the_recorded_ab_study_numbers():
    """docs/LLM_ENRICHMENT_STRATEGY.md §1.6: title 'ColdwaterCircuit', 5/5
    scenes, 9/9 entities, 12/12 scripts, 6 narrative beats, 10/10 audio cues."""
    schema, _msgs = parse_brief(_COLDWATER)
    assert schema.project_name == "ColdwaterCircuit"
    assert schema.scenes == [
        "MainMenu", "ServiceTent", "BlackIceBay", "SnowdriftPass", "ThawShoreFinal"]
    assert len(schema.scene_specs) == 5
    assert len(schema.entities) == 9
    assert len(schema.scripts) == 12
    assert len(schema.story_beats) == 6
    assert len(schema.audio_tracks) == 10


def test_coldwater_circuit_fill_is_stamped_deterministic():
    schema, _msgs = parse_brief(_COLDWATER)
    assert schema.meta["fill"] == "deterministic"
    assert schema.meta["concept"] == ""
    assert schema.meta["user_prompt"] == _COLDWATER


def test_parse_is_deterministic():
    """Two parses of the same brief must produce the same schema — this is
    the whole point of a control arm."""
    s1, _ = parse_brief(_COLDWATER)
    s2, _ = parse_brief(_COLDWATER)
    assert s1.model_dump() == s2.model_dump()


def test_unstructured_prompt_abstains_loudly():
    with pytest.raises(BriefParseError, match=r"GAME TYPE|SCENES|ENTITIES"):
        parse_brief("a cozy game about a lighthouse keeper")


def test_empty_prompt_abstains_loudly():
    with pytest.raises(BriefParseError):
        parse_brief("")
