"""
Phase 2 — "imagination alignment" tests.

Archetypes give a GTA mission a real gameplay structure (a race has a start,
laps, a finish and rivals; a heist has a grab and a getaway) instead of the
generic "drive here, kill there, drive here, wait" the LLM defaults to. These
tests are offline — they exercise detection, the deterministic anti-repetition
scaffold, and within-mission model dedup without any LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.archetypes import (
    detect_archetype, apply_scaffold, is_degenerate, track_for_location,
    track_from_text, render_structure_hint, ARCHETYPES, _SCAFFOLDS,
)
from src.mission_schema import MissionSchema, MissionStep, MissionPed
from src.resolver import ModelResolver

ROOT = Path(__file__).resolve().parent.parent


def _valid_models(category: str) -> set[str]:
    data = json.loads((ROOT / "lookups" / f"{category}.json").read_text(encoding="utf-8"))
    return set(data.get("valid_models", []))


# ── Detection ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("make a race mod",                 "race"),
    ("a street race through vinewood",  "race"),
    ("pull off a casino heist",         "heist"),
    ("rob the bank vault",              "heist"),
    ("deep sea dive for sunken treasure", "dive"),
    ("an underwater submarine mission", "dive"),
    ("a fighter jet dogfight",          "fly"),
    ("fly a helicopter through the city", "fly"),
    ("raid the gang compound",          "assault"),
    ("chase down the getaway driver",   "chase"),
    ("a quiet afternoon feeding ducks", None),
])
def test_detect_archetype(prompt, expected):
    a = detect_archetype(prompt)
    assert (a.name if a else None) == expected


def test_prompt_outweighs_concept():
    # A race keyword in the prompt beats heist keywords buried in the concept.
    a = detect_archetype("a drift race", concept="they rob a vault and steal the loot")
    assert a is not None and a.name == "race"


# ── Scaffold (anti-repetition guard) ──────────────────────────────────────────

def _degenerate(location: str = "vinewood") -> MissionSchema:
    return MissionSchema(
        mission_name="Probe", location_name=location,
        steps=[MissionStep(type="go_to", label="drive"),
               MissionStep(type="wait", label="wait")],
    )


def test_race_scaffold_builds_a_real_race():
    arch = detect_archetype("street race")
    schema = _degenerate("vinewood")
    assert is_degenerate(schema, arch)
    apply_scaffold(schema, arch)

    types = [s.type for s in schema.steps]
    assert "race_lap" in types and "get_in_vehicle" in types
    # Track is location-matched and real.
    race = next(s for s in schema.steps if s.type == "race_lap")
    assert race.params["track"] == "vinewood.circuit"
    assert race.params["lap_count"] >= 2
    # Rival field exists.
    assert sum(1 for v in schema.vehicles if v.role == "race_opponent") >= 2


def test_non_degenerate_output_is_trusted():
    arch = detect_archetype("race")
    schema = MissionSchema(
        mission_name="Y", location_name="airport",
        steps=[MissionStep(type="get_in_vehicle", label="go"),
               MissionStep(type="race_lap", label="laps",
                           params={"track": "airport.figure_8", "lap_count": 4})],
    )
    assert not is_degenerate(schema, arch)
    assert apply_scaffold(schema, arch) == []          # left untouched
    assert [s.type for s in schema.steps] == ["get_in_vehicle", "race_lap"]


def test_every_archetype_has_a_scaffold_and_signature():
    for arch in ARCHETYPES:
        assert arch.signature_steps, f"{arch.name} has no signature steps"
        assert arch.name in _SCAFFOLDS, f"{arch.name} has no scaffold builder"
        assert render_structure_hint(arch), f"{arch.name} has no structure hint"


def test_scaffold_models_are_valid_game_ids():
    """Every model an injected scaffold spawns must exist in the lookups."""
    veh_ok, obj_ok = _valid_models("vehicles"), _valid_models("objects")
    for arch in ARCHETYPES:
        schema = _degenerate("docks")
        apply_scaffold(schema, arch)
        for v in schema.vehicles:
            assert v.model in veh_ok, f"{arch.name}: bad vehicle model {v.model}"
        for o in schema.objects:
            assert o.model in obj_ok, f"{arch.name}: bad object model {o.model}"


def test_track_for_location_always_real():
    real_tracks = set(json.loads(
        (ROOT / "lookups" / "tracks.json").read_text(encoding="utf-8")).keys())
    for loc in ("airport", "vinewood", "highway", "docks", "sandy_shores",
                "downtown", "prison", "unknownville"):
        assert track_for_location(loc) in real_tracks


@pytest.mark.parametrize("text,location,expected", [
    ("a figure-8 race on the airport runway", "airport", "airport.figure_8"),
    ("street race down the freeway",           "downtown", "highway.circuit"),
    ("rally through the desert",               "sandy_shores", "sandy_shores.circuit"),
    ("a race in the container yard",           "docks", "docks.circuit"),
    ("",                                       "vinewood", "vinewood.circuit"),  # falls back to location
])
def test_track_from_text_infers_course(text, location, expected):
    assert track_from_text(text, location) == expected


def test_race_scaffold_preserves_course_intent():
    """A degenerate race whose prompt named a figure-8 runway should scaffold onto
    airport.figure_8 — not the bare location default — once meta carries intent."""
    arch = detect_archetype("figure-8 race on the airport runway")
    schema = _degenerate("airport")
    schema.meta["user_prompt"] = "figure-8 race on the airport runway"
    apply_scaffold(schema, arch)
    race = next(s for s in schema.steps if s.type == "race_lap")
    assert race.params["track"] == "airport.figure_8"


# ── Dedup ─────────────────────────────────────────────────────────────────────

def test_dedup_adds_variety_but_keeps_models_valid():
    r = ModelResolver(rag=None, lookups_dir="lookups", use_rag=False)
    peds = [
        MissionPed(description="corrupt cop",   model="s_m_y_cop_01", role="guard"),
        MissionPed(description="security guard", model="s_m_y_cop_01", role="guard"),
        MissionPed(description="cop",            model="s_m_y_cop_01", role="guard"),
    ]
    r._dedup_models(peds, "peds")
    models = [p.model for p in peds]
    valid = _valid_models("peds")
    assert all(m in valid for m in models), f"dedup produced invalid model: {models}"
    # The security guard should not stay identical to the cop — a real security
    # ped exists, so variety is applied without drifting to something unrelated.
    assert peds[1].model != "s_m_y_cop_01"


def test_dedup_keeps_identical_descriptions_identical():
    r = ModelResolver(rag=None, lookups_dir="lookups", use_rag=False)
    peds = [
        MissionPed(description="cop", model="s_m_y_cop_01", role="guard"),
        MissionPed(description="cop", model="s_m_y_cop_01", role="patrol"),
    ]
    r._dedup_models(peds, "peds")
    assert peds[0].model == peds[1].model      # same description → same model
