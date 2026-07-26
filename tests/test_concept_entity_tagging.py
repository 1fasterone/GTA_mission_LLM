"""Enemy/Player tagging in the concept-entity rescue
(src/unity_concept_entities.py).

The parser itself (wrapped bullets, comma-delimited kind, count, model_hint) is
covered by tests/test_unity_concept_entities.py. This file covers the tagging
layer added on top: which rescued entities earn tag="Enemy" / tag="Player".
Both tags are runtime-load-bearing — AI.cs.j2 acquires its target via
FindGameObjectWithTag and EntitySpawner only adds a collider to a tagged group,
so an untagged hostile/avatar is inert scenery.
"""

from src.unity_concept_entities import inject_missing_concept_entities
from src.unity_schema import EntitySpec, UnitySchema


def _schema(prompt: str, entities=None) -> UnitySchema:
    s = UnitySchema(project_name="T", game_type="3D", platform="all",
                    render_pipeline="URP")
    s.entities = entities or []
    s.meta = {"user_prompt": prompt}
    return s


def _by_name(schema) -> dict:
    return {e.name: e for e in schema.entities}


# ── Enemy tagging ────────────────────────────────────────────────────────────

def test_hostile_non_vehicle_is_tagged_enemy():
    """The live gap: the old rule was gated on kind=="vehicle", so a hostile
    authored as a character came back Untagged."""
    schema = _schema(
        "ENTITIES\n- ScavengerDrone: hovering drones that patrol the stacks and\n"
        '  attack the player on sight (kind: character, model_hint "drone"), count 5.\n')
    inject_missing_concept_entities(schema)
    ent = _by_name(schema)["ScavengerDrone"]
    assert ent.tag == "Enemy"
    assert ent.count == 5


def test_enemy_kind_is_tagged_without_hostile_wording():
    schema = _schema("ENTITIES\n- Sentinel: a floating unit (kind: enemy).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["Sentinel"].tag == "Enemy"


def test_hostile_noun_in_pascalcase_name_is_enough():
    schema = _schema("ENTITIES\n- RivalRunner: a fast one (kind: character).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["RivalRunner"].tag == "Enemy"


def test_vehicle_ai_rival_still_tagged():
    """Pre-existing racing behavior must survive the broadening."""
    schema = _schema(
        "ENTITIES\n- Stormrider: an AI rival motorcycle that brakes late\n"
        "  (kind: vehicle).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["Stormrider"].tag == "Enemy"


def test_player_is_never_tagged_enemy_despite_hostile_wording():
    schema = _schema(
        "ENTITIES\n- PlayerDiver: a diver who sneaks past hostile drones and\n"
        "  outruns rival scavengers (kind: character).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["PlayerDiver"].tag != "Enemy"


def test_friendly_ai_npc_is_not_tagged_enemy():
    schema = _schema(
        "ENTITIES\n- Harbormaster: an AI companion who hands over tools\n"
        "  (kind: character).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["Harbormaster"].tag != "Enemy"


def test_benign_use_of_a_hostile_verb_is_not_enemy():
    schema = _schema(
        "ENTITIES\n- SalvageCrate: crates to hunt for scrap in (kind: prop).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["SalvageCrate"].tag != "Enemy"


# ── Player tagging (single-match invariant) ──────────────────────────────────

def test_player_avatar_is_tagged_player():
    schema = _schema(
        "ENTITIES\n- PlayerDiver: the player's salvage diver in a dive suit\n"
        '  (kind: character, model_hint "diver in wetsuit").\n')
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["PlayerDiver"].tag == "Player"


def test_player_vehicle_is_tagged_player():
    schema = _schema(
        "ENTITIES\n- PlayerBike: the player's sport motorcycle (kind: vehicle).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["PlayerBike"].tag == "Player"


def test_player_associated_prop_is_not_tagged_player():
    schema = _schema(
        "ENTITIES\n- PlayerWetsuit: the player's spare wetsuit (kind: prop).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["PlayerWetsuit"].tag != "Player"


def test_only_one_entity_is_tagged_player():
    schema = _schema(
        "ENTITIES\n"
        "- PlayerDiver: the player's diver (kind: character).\n"
        "- PlayerShadow: the player's echo twin (kind: character).\n")
    inject_missing_concept_entities(schema)
    assert sum(e.tag == "Player" for e in schema.entities) == 1


def test_existing_player_tag_is_not_duplicated():
    kept = EntitySpec(name="Hero", kind="character", count=1, tag="Player")
    schema = _schema(
        "ENTITIES\n- PlayerDiver: the player's diver (kind: character).\n", [kept])
    inject_missing_concept_entities(schema)
    assert sum(e.tag == "Player" for e in schema.entities) == 1
    assert _by_name(schema)["PlayerDiver"].tag != "Player"


def test_avatar_count_is_forced_to_one():
    schema = _schema(
        "ENTITIES\n- PlayerDiver: the player's diver (kind: character), count 4.\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["PlayerDiver"].count == 1


def test_npc_mentioning_the_player_is_not_tagged_player():
    schema = _schema(
        "ENTITIES\n- Harbormaster: the dockhand who gives the player objectives\n"
        "  (kind: character).\n")
    inject_missing_concept_entities(schema)
    assert _by_name(schema)["Harbormaster"].tag == "Untagged"
