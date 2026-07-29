"""
Golden semantics for the GTA5 RAG corpus.

These lock down what an entry *means*, which is what retrieval actually ranks
on. Each case here corresponds to an observed retrieval failure against the
28,006-entry index:

  "civilian bystander walking down the street" -> A_C_Westy (a dog) at rank 2
  "baseball bat"                               -> a newspaper at rank 3
  "police siren wail"                          -> a dialogue bank over SIREN_DISTANT

Entry derivation is pure and deterministic, so none of this needs an index, a
model or a network — see test_gta_retrieval.py for the ranking-level checks
that do.
"""
from __future__ import annotations

import pytest

from rag.schema.animation import make_anim_entry
from rag.schema.audio import make_audio_entry
from rag.schema.base import RagEntry, build_chunk
from rag.schema.behavior import make_scenario_entry
from rag.schema.ped import make_ped_entry
from rag.schema.prop import make_prop_entry
from rag.schema.vehicle import make_vehicle_entry
from rag.schema.weapon import make_weapon_entry


def ped(name: str, **raw):
    return make_ped_entry({"Name": name, **raw})


def weapon(name: str, model: str, label: str, category="GROUP_MELEE", dmg="MELEE"):
    return make_weapon_entry({
        "Name": name, "ModelName": model, "Category": category,
        "DamageType": dmg, "TranslatedLabel": {"English": label},
    })


# ── animals are not people ────────────────────────────────────────────────────

ANIMALS = ["A_C_Boar", "A_C_Westy", "A_C_Chop", "A_C_Rottweiler", "A_C_Rhesus",
           "A_C_SharkTiger", "A_C_MtLion", "A_C_Pigeon", "A_C_Cow"]


@pytest.mark.parametrize("name", ANIMALS)
def test_animals_carry_no_human_aliases(name):
    e = make_ped_entry({"Name": name})
    forbidden = {"civilian", "pedestrian", "bystander", "office worker", "gang member"}
    assert not (forbidden & set(e.aliases)), f"{name} reads as a person: {e.aliases}"


@pytest.mark.parametrize("name", ANIMALS)
def test_animals_are_tagged_as_animals(name):
    e = make_ped_entry({"Name": name})
    assert "category.animal" in e.tags
    assert "faction.animal" in e.tags
    assert "role.civilian" not in e.tags
    assert "not a human" in e.description


def test_animal_species_words_are_searchable():
    assert "dog" in make_ped_entry({"Name": "A_C_Westy"}).aliases
    assert "shark" in make_ped_entry({"Name": "A_C_SharkHammer"}).aliases
    assert "mountain lion" in make_ped_entry({"Name": "A_C_MtLion"}).aliases


def test_unlisted_animal_still_resolves_as_an_animal():
    e = make_ped_entry({"Name": "A_C_Wolverine"})
    assert "category.animal" in e.tags and "animal" in e.aliases


# ── the named cast ────────────────────────────────────────────────────────────

def test_protagonists_are_reachable_by_name():
    for model, first in [("player_zero", "michael"),
                         ("player_one", "franklin"),
                         ("player_two", "trevor")]:
        e = make_ped_entry({"Name": model})
        assert first in e.aliases, f"{model} unreachable by '{first}'"
        assert "role.protagonist" in e.tags


def test_camel_case_cast_names_are_split():
    e = make_ped_entry({"Name": "CS_AmandaTownley"})
    assert e.label == "Amanda Townley"
    assert "amanda" in e.aliases and "amanda townley" in e.aliases


@pytest.mark.parametrize("name", ["CS_AmandaTownley", "ig_lestercrest", "csb_ashley"])
def test_story_cast_is_not_filed_as_service_staff(name):
    e = make_ped_entry({"Name": name})
    assert "faction.service" not in e.tags
    assert "faction.story" in e.tags


def test_lowercase_cast_names_survive():
    assert make_ped_entry({"Name": "cs_ashley"}).label == "Ashley"


# ── ordinary peds still decode ────────────────────────────────────────────────

def test_cop_still_decodes():
    e = make_ped_entry({"Name": "S_M_Y_Cop_01"})
    assert {"faction.law", "gender.male", "age.young", "threat.high"} <= set(e.tags)
    assert "police officer" in e.aliases


def test_unrecognised_ped_gets_no_invented_faction():
    """A wrong tag is worse than a missing one — slod_human is not 'service'."""
    e = make_ped_entry({"Name": "slod_human"})
    assert not [t for t in e.tags if t.startswith("faction.")]


def test_service_prefix_still_means_service():
    e = make_ped_entry({"Name": "s_m_m_migrant_01"})
    assert "faction.service" in e.tags


def test_ped_label_has_no_double_spaces():
    assert "  " not in make_ped_entry({"Name": "S_M_Y_Cop_01"}).label


# ── weapons: aliases must be earned ───────────────────────────────────────────

@pytest.mark.parametrize("model,name,label", [
    ("w_me_nightstick", "WEAPON_NIGHTSTICK", "Nightstick"),
    ("w_me_bat",        "WEAPON_BAT",        "Baseball Bat"),
    ("w_me_crowbar",    "WEAPON_CROWBAR",    "Crowbar"),
    ("w_me_hammer",     "WEAPON_HAMMER",     "Hammer"),
    ("w_me_gclub",      "WEAPON_GOLFCLUB",   "Golf Club"),
])
def test_blunt_weapons_are_not_blades(model, name, label):
    assert "blade" not in weapon(name, model, label).aliases


@pytest.mark.parametrize("model,name,label", [
    ("w_me_knife_01",   "WEAPON_KNIFE",       "Knife"),
    ("w_me_machete_01", "WEAPON_MACHETE",     "Machete"),
    ("w_me_switchblade","WEAPON_SWITCHBLADE", "Switchblade"),
])
def test_bladed_weapons_keep_blade(model, name, label):
    assert "blade" in weapon(name, model, label).aliases


def test_weapons_carry_their_own_words():
    assert "baseball bat" in weapon("WEAPON_BAT", "w_me_bat", "Baseball Bat").aliases
    assert "baton" in weapon("WEAPON_NIGHTSTICK", "w_me_nightstick", "Nightstick").aliases


def test_longest_weapon_token_wins():
    e = weapon("WEAPON_STONE_HATCHET", "w_me_stone_hatchet", "Stone Hatchet")
    assert "stone hatchet" in e.aliases


# ── audio: a name is all we get, so decode it ─────────────────────────────────

@pytest.mark.parametrize("name", ["a_c_rhesus", "A_M_M_Business_01",
                                  "S_M_Y_Cop_01", "ig_lestercrest"])
def test_ped_shaped_audio_names_are_speech_banks(name):
    e = make_audio_entry(name)
    assert e.meta["bank_type"] == "speech"
    assert "ped dialogue" in e.aliases
    assert "not a sound effect" in e.description


def test_sound_subjects_are_spelled_out():
    e = make_audio_entry("SIREN_DISTANT")
    assert e.meta["subject"] == "siren wail"
    assert "siren wail" in e.aliases
    assert "subject.siren" in e.tags


def test_name_evidence_beats_the_source_file():
    """A ped speech bank stays a speech bank whichever dump lists it."""
    for src in ("scriptAudioBankNames.json", "ambientAudioBankNames.json",
                "musicEventNames.json"):
        assert make_audio_entry("a_c_rhesus", src).meta["bank_type"] == "speech"


def test_duplicate_names_collapse_by_fixed_precedence():
    """The same name ships in several dumps; scrape order must not decide type."""
    from rag.schema.audio import type_rank
    script  = make_audio_entry("AGENCY_HEIST_FIB_TOWER_ALARMS", "scriptAudioBankNames.json")
    ambient = make_audio_entry("AGENCY_HEIST_FIB_TOWER_ALARMS", "ambientAudioBankNames.json")
    assert script.id == ambient.id, "same name is one entry"
    # ambient is the more specific listing, so it wins regardless of scrape order
    assert type_rank(ambient.meta["bank_type"]) < type_rank(script.meta["bank_type"])


def test_dialogue_bank_is_not_described_as_a_sound():
    e = make_audio_entry("BKR_RESCUE_CONTACT_POLICE_STOP")
    assert e.meta["subject"] == ""
    assert "sound effect" not in e.description


# ── props: labels that read like words ────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("apa_mp_apa_yacht_jacuzzi_cam", "Apa Yacht Jacuzzi Cam"),
    ("prop_drug_package_02",         "Drug Package"),
    ("hei_prop_heist_cash_bag_01",   "Heist Cash Bag"),
])
def test_prop_labels_drop_dlc_prefixes(name, expected):
    assert make_prop_entry(name).label == expected


# ── the shared chunk contract ─────────────────────────────────────────────────

MAKERS = [
    lambda: make_ped_entry({"Name": "S_M_Y_Cop_01"}),
    lambda: make_vehicle_entry({"Name": "police", "DisplayName": {"English": "Police Cruiser"},
                                "Class": "EMERGENCY", "Type": "CAR", "Seats": 4,
                                "MaxSpeed": 40.0}),
    lambda: weapon("WEAPON_BAT", "w_me_bat", "Baseball Bat"),
    lambda: make_prop_entry("prop_drug_package_02"),
    lambda: make_audio_entry("SIREN_DISTANT"),
    lambda: make_scenario_entry("WORLD_HUMAN_GUARD_STAND"),
    lambda: make_anim_entry("amb@world_human_smoking@male@male_a@", ["base", "idle_a"]),
]


@pytest.mark.parametrize("make", MAKERS)
def test_no_shared_usage_boilerplate(make):
    """Text every entry of a kind carries adds no ranking signal, only centroid pull."""
    text = make().chunk_text.lower()
    for phrase in ("use for:", "use in:", "use with play_sound",
                   "mission spawnable objects", "npc cast", "getaway, patrol, chase"):
        assert phrase not in text, f"boilerplate survived: {phrase}"


@pytest.mark.parametrize("make", MAKERS)
def test_tags_appear_exactly_once(make):
    entry = make()
    if entry.tags:
        assert entry.chunk_text.count(", ".join(entry.tags)) == 1


@pytest.mark.parametrize("make", MAKERS)
def test_label_is_not_restated_in_the_description(make):
    entry = make()
    body = entry.chunk_text.split("Tags:")[-1]
    assert body.count(entry.label) <= 1


def test_build_chunk_states_each_fact_once():
    e = RagEntry(domain="gta5", kind="ped", canonical="x", label="X",
                 aliases=["a", "b"], tags=["t.1"], description="Only this.")
    text = build_chunk(e, "head", {"Name": "X", "Empty": ""})
    assert text.splitlines() == ["head", "Name: X", "Also called: a, b",
                                 "Tags: t.1", "Only this."]
