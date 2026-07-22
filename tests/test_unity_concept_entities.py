"""Concept-named entity rescue: brief "ENTITIES" bullet parsing.

Regression cover for a live E2E failure (harbor_light_salvage): every rescued
entity degraded to kind="prop", count=6 because the ~80-col wrapped bullets
hid the "(kind: ...)" clause from a line-by-line parse, the kind capture was
only comma-blind, and "count N" was never read at all.
"""
from __future__ import annotations

from src.unity_concept_entities import (
    extract_named_entities, inject_missing_concept_entities,
)
from src.unity_schema import EntitySpec, UnitySchema


def _schema(user_prompt: str, entities: list[EntitySpec] | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="ConceptEntityTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        entities=entities or [], meta={"user_prompt": user_prompt},
    )


# ── parsing ───────────────────────────────────────────────────────────────────

def test_wrapped_bullet_keeps_its_kind():
    """The "(" and ")" straddle a line break — the common case in a brief
    wrapped at ~80 cols, and the reason every entity used to become a prop."""
    brief = (
        "ENTITIES\n"
        "- PlayerDiver: the player's salvage diver in a dive suit (kind: character,\n"
        "  model_hint \"diver in wetsuit\"), starting at the lighthouse door.\n"
    )
    ents = extract_named_entities(brief)
    assert len(ents) == 1
    assert ents[0]["name"] == "PlayerDiver"
    assert ents[0]["kind"] == "character"
    assert ents[0]["model_hint"] == "diver in wetsuit"
    assert "kind:" not in ents[0]["description"]


def test_single_line_bullet_with_trailing_model_hint():
    brief = ('ENTITIES\n'
             '- Harbormaster: the dockside quest giver (kind: npc, model_hint '
             '"harbor worker").\n')
    ent = extract_named_entities(brief)[0]
    assert ent["kind"] == "npc"          # not 'npc, model_hint "harbor worker"'
    assert ent["model_hint"] == "harbor worker"
    assert ent["count"] is None


def test_explicit_count_is_parsed():
    brief = ("ENTITIES\n"
             "- PaddockTent: three team support tents clustered in the paddock\n"
             "  (kind: prop, model_hint \"paddock tent\"), count 3.\n"
             "- Barnacle: crusted shells on the pilings (kind: prop), count 12.\n")
    ents = extract_named_entities(brief)
    assert [e["count"] for e in ents] == [3, 12]
    assert [e["kind"] for e in ents] == ["prop", "prop"]


def test_bare_kind_bullet():
    brief = "ENTITIES\n- Lighthouse: the stone tower on the point (kind: building).\n"
    ent = extract_named_entities(brief)[0]
    assert ent == {"name": "Lighthouse", "kind": "building",
                   "description": "the stone tower on the point",
                   "model_hint": "", "count": None}


def test_slash_delimited_kind_still_takes_the_first_token():
    brief = "ENTITIES\n- PlayerCar: the player's toy car (kind: vehicle / car).\n"
    assert extract_named_entities(brief)[0]["kind"] == "vehicle"


def test_unindented_trailing_prose_is_not_folded_into_the_last_bullet():
    """turbo_toys-style briefs close the list with a free-form sentence; it must
    not become part of BoostPad's description (or add phantom entities)."""
    brief = ("ENTITIES\n"
             "- Coin: spinning pickup collectible (kind: collectible).\n"
             "- BoostPad: a springy trigger pad (kind: prop).\n"
             "All four cars are cartoon toy cars and should resolve to real\n"
             "Sketchfab cartoon-car models.\n")
    ents = extract_named_entities(brief)
    assert [e["name"] for e in ents] == ["Coin", "BoostPad"]
    assert ents[1]["description"] == "a springy trigger pad"


def test_section_header_ends_the_list():
    brief = ("ENTITIES\n"
             "- Coin: a pickup (kind: collectible).\n"
             "\n"
             "AUDIO\n"
             "- Splash: a cue (kind: prop).\n")
    assert [e["name"] for e in extract_named_entities(brief)] == ["Coin"]


def test_no_entities_section_returns_empty():
    assert extract_named_entities("make me a racing game") == []
    assert extract_named_entities("") == []


def test_absurd_count_falls_back_to_the_kind_default():
    brief = "ENTITIES\n- Grit: sand grains (kind: prop), count 9999.\n"
    assert extract_named_entities(brief)[0]["count"] is None


# ── injection ─────────────────────────────────────────────────────────────────

def test_rescued_entities_carry_kind_count_and_model_hint():
    brief = (
        "ENTITIES\n"
        "- PlayerDiver: the player's salvage diver (kind: character, model_hint\n"
        "  \"diver in wetsuit\"), starting at the lighthouse door.\n"
        "- Lighthouse: the stone tower on the point (kind: building).\n"
        "- CrateStack: salvage crates on the quay (kind: prop), count 4.\n"
    )
    schema = _schema(brief)
    msgs = inject_missing_concept_entities(schema)
    assert len(msgs) == 1
    by_name = {e.name: e for e in schema.entities}
    assert set(by_name) == {"PlayerDiver", "Lighthouse", "CrateStack"}

    diver = by_name["PlayerDiver"]
    assert (diver.kind, diver.count) == ("character", 1)
    assert diver.model_hint == "diver in wetsuit"

    assert (by_name["Lighthouse"].kind, by_name["Lighthouse"].count) == ("building", 1)

    crates = by_name["CrateStack"]
    assert (crates.kind, crates.count) == ("prop", 4)


def test_explicit_count_promotes_single_placement_to_scatter():
    """"single" bakes exactly one position, so a repeated group must scatter."""
    brief = "ENTITIES\n- Gull: seabirds over the quay (kind: bird), count 5.\n"
    schema = _schema(brief)
    inject_missing_concept_entities(schema)
    gull = schema.entities[0]
    assert (gull.count, gull.placement) == (5, "scatter")


def test_countless_bullet_keeps_the_per_kind_default():
    brief = "ENTITIES\n- Barrel: scattered barrels (kind: prop).\n"
    schema = _schema(brief)
    inject_missing_concept_entities(schema)
    assert schema.entities[0].count == 6  # _KIND_DEFAULTS["prop"]


def test_llm_authored_entities_are_never_touched():
    brief = "ENTITIES\n- Lighthouse: the stone tower (kind: building), count 1.\n"
    kept = EntitySpec(name="Lighthouse", kind="prop", count=6)
    schema = _schema(brief, [kept])
    assert inject_missing_concept_entities(schema) == []
    assert schema.entities == [kept]


def test_rival_vehicle_still_gets_the_enemy_tag():
    brief = ("ENTITIES\n"
             "- RivalStormrider: an aggressive AI rival motorcycle that brakes\n"
             "  late (kind: vehicle, model_hint \"sport motorcycle\"), grid slot 2.\n")
    schema = _schema(brief)
    inject_missing_concept_entities(schema)
    rival = schema.entities[0]
    assert (rival.kind, rival.tag, rival.model_hint) == (
        "vehicle", "Enemy", "sport motorcycle")
