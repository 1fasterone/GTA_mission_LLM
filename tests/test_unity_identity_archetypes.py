"""Tests for deterministic noun -> identity archetype resolution."""

import json
import shutil
from pathlib import Path

import pytest

from src.unity_identity_archetypes import load_identity_catalog, resolve_identity
from src.unity_entity_asset_resolver import resolve_entity_assets
from src.unity_schema import EntitySpec, UnitySchema


def test_catalog_has_the_requested_identity_families():
    archetypes, noun_map, rules = load_identity_catalog("lookups")
    expected = {
        "medieval_human_commoner", "medieval_human_worker",
        "medieval_human_knight", "medieval_human_noble",
        "medieval_human_servant", "fantasy_human",
        "fantasy_creature_humanoid", "creature_fantasy_beast",
        "sci-fi_human", "sci-fi_alien_humanoid", "soldier_infantry",
        "modern_security_guard", "modern_police", "civilian_modern",
        "npc_human", "creature_domestic", "creature_predator",
        "creature_generic", "vehicle_boat", "vehicle_car",
        "vehicle_aircraft", "vehicle_sci-fi_ship", "object_generic",
        "object_weapon_melee", "object_weapon_ranged", "object_structure",
    }
    assert expected <= set(archetypes)
    assert len(archetypes) == 27
    assert noun_map["paladin"] == "medieval_human_knight"
    assert any(rule["name"] == "water_vehicle_context" for rule in rules)


def test_identity_resolution_expands_reusable_attribute_bundles():
    peasant = resolve_identity(name="Peasant", kind="character")
    assert peasant.name == "medieval_human_commoner"
    assert peasant.attributes["identity"]["profession"] == "farmer"
    assert peasant.attributes["visual"]["equipment"] == ["hoe", "basket"]
    assert peasant.attributes["affordances"] == [
        "can_work", "can_flee", "can_carry", "can_operate_tools"
    ]
    assert peasant.confidence == 1.0
    assert peasant.confidence_tier == "direct_noun_map"

    knight = resolve_identity(name="Knight", kind="character")
    assert knight.attributes["identity"]["class"] == "noble"
    assert knight.attributes["visual"]["clothing_style"] == "armor"
    assert knight.attributes["affordances"] == ["can_fight", "can_flee", "can_patrol"]


def test_context_selects_modern_worker_bundle_and_peasant_variants():
    worker = resolve_identity(
        name="Worker", kind="character", context="inside a modern factory"
    )
    assert worker.name == "modern_human_worker"
    assert worker.attributes["identity"]["profession"] == "factory_worker"
    assert worker.attributes["environment"]["location_type"] == "factory"
    assert worker.confidence_tier == "context_match"

    battle = resolve_identity(
        name="Peasant", kind="character", context="flee across the battlefield"
    )
    assert battle.selected_variant == "battle"
    assert battle.attributes["environment"]["location_type"] == "battlefield"
    assert battle.attributes["visual"]["equipment"] == []
    assert battle.attributes["affordances"] == ["can_flee"]

    village = resolve_identity(
        name="Villager", kind="character", context="work in the village fields"
    )
    assert village.selected_variant == "village"
    assert village.attributes["affordances"] == [
        "can_work", "can_carry", "can_operate_tools"
    ]


def test_visual_attribute_rules_add_evidence_without_overriding_identity():
    result = resolve_identity(
        name="Peasant", kind="character", visual_tokens=["mud", "tool"]
    )
    assert result.attributes["visual"]["clothing_condition"] == "worn"
    assert result.attributes["visual"]["dirt_level"] == "high"
    assert "hand_tool" in result.attributes["visual"]["equipment"]
    assert result.attribute_evidence
    assert result.attribute_conflicts == ()
    # The lookup-authored identity remains stronger than corroborating visual
    # evidence; the visual rule is still recorded in attribute_evidence.
    assert result.attribute_confidence["visual"]["dirt_level"] == 1.0


def test_attribute_registry_rejects_unknown_fields_and_affordances(tmp_path):
    raw = json.loads(Path("lookups/unity/identity_archetypes.json").read_text())
    lookups = tmp_path / "lookups" / "unity"
    lookups.mkdir(parents=True)

    raw["attribute_bundles"]["peasant"]["attributes"]["identity"]["unknown"] = "x"
    (lookups / "identity_archetypes.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unknown field"):
        load_identity_catalog(str(tmp_path / "lookups"))

    raw = json.loads(Path("lookups/unity/identity_archetypes.json").read_text())
    raw["attribute_bundles"]["peasant"]["attributes"]["affordances"] = ["teleport"]
    (lookups / "identity_archetypes.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unknown value"):
        load_identity_catalog(str(tmp_path / "lookups"))


def test_distinctive_nouns_resolve_to_identity_not_the_literal_noun():
    assert resolve_identity(name="Paladin", kind="character").name == "medieval_human_knight"
    assert resolve_identity(model_hint="office worker", kind="npc").name == "civilian_modern"
    assert resolve_identity(name="Speedboat", kind="vehicle").name == "vehicle_boat"
    result = resolve_identity(name="Wolf", kind="enemy")
    assert result.name == "creature_predator"
    assert result.rule == "noun_map"
    assert result.used_fallback is False


def test_context_rescues_shallow_human_nouns():
    worker = resolve_identity(
        name="Worker", kind="character", context="a medieval forge near the castle"
    )
    assert worker.name == "medieval_human_worker"
    assert worker.rule == "medieval_worker_context"
    assert worker.used_fallback is True

    commoner = resolve_identity(
        name="Human", kind="npc", context="a medieval village market"
    )
    assert commoner.name == "medieval_human_commoner"
    assert commoner.rule == "medieval_context"


def test_context_rescues_shallow_vehicle_nouns():
    boat = resolve_identity(
        name="Unit", kind="vehicle", context="cross the open water to the harbor"
    )
    assert boat.name == "vehicle_boat"
    assert boat.rule == "water_vehicle_context"

    car = resolve_identity(
        name="Unit", kind="vehicle", context="a city street racing garage"
    )
    assert car.name == "vehicle_car"
    assert car.rule == "ground_vehicle_context"


def test_kind_fallbacks_are_explicit_and_stable():
    human = resolve_identity(name="Unit", kind="character")
    creature = resolve_identity(name="Unit", kind="creature")
    vehicle = resolve_identity(name="Unit", kind="vehicle")
    assert (human.name, human.rule, human.used_fallback) == ("npc_human", "generic_human_kind", True)
    assert (creature.name, creature.rule, creature.used_fallback) == ("creature_generic", "generic_creature_kind", True)
    assert (vehicle.name, vehicle.rule, vehicle.used_fallback) == ("vehicle_car", "generic_vehicle_kind", True)


def test_unrelated_props_do_not_gain_a_human_identity():
    result = resolve_identity(name="StoneWall", kind="prop", context="medieval castle")
    assert result.name == "object_generic"
    assert result.rule == "generic_object_kind"


def test_missing_lookup_data_abstains_instead_of_inventing_identity(tmp_path):
    result = resolve_identity(name="Knight", kind="character", lookups_dir=str(tmp_path))
    assert result.name is None
    assert result.rule == "no_identity_signal"


def test_identity_vocabulary_steers_a_shallow_catalog_query(tmp_path):
    """The identity result must affect deterministic asset selection, not just logs."""
    asset_root = tmp_path / "models"
    asset_root.mkdir()
    (asset_root / "cargo-boat.fbx").write_bytes(b"boat")
    (asset_root / "city-sedan.fbx").write_bytes(b"car")
    lookups = tmp_path / "lookups" / "unity"
    lookups.mkdir(parents=True)
    shutil.copyfile(
        "lookups/unity/identity_archetypes.json",
        lookups / "identity_archetypes.json",
    )
    catalog = {
        "version": 1,
        "assets": [
            {
                "filename": "cargo-boat.fbx", "relpath": "cargo-boat.fbx",
                "root": str(asset_root), "format": "fbx", "pack": "test",
                "source": "kenney", "category": "vehicle",
                "keywords": ["cargo", "boat", "ship", "watercraft", "vehicle"],
                "kind_hints": ["vehicle"], "game_contexts": ["any"],
                "generic_score": 0, "scale": 1.0,
            },
            {
                "filename": "city-sedan.fbx", "relpath": "city-sedan.fbx",
                "root": str(asset_root), "format": "fbx", "pack": "test",
                "source": "kenney", "category": "vehicle",
                "keywords": ["city", "sedan", "car", "vehicle"],
                "kind_hints": ["vehicle"], "game_contexts": ["any"],
                "generic_score": 0, "scale": 1.0,
            },
        ],
    }
    (lookups / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    schema = UnitySchema(
        project_name="WaterCrossing", company_name="Test",
        description="cross open water to the harbor",
        game_type="3D", platform="pc", render_pipeline="URP",
        visual_style="low_poly", unity_version="6000.2",
        entities=[EntitySpec(name="Unit", kind="vehicle")],
    )

    paths = resolve_entity_assets(
        schema, {}, {}, lookups_dir=str(tmp_path / "lookups"), strict=True
    )
    assert paths["Unit"].endswith("cargo-boat")
    assert schema.meta["identity_archetypes"]["Unit"]["archetype"] == "vehicle_boat"
    assert schema.meta["identity_archetypes"]["Unit"]["rule"] == "water_vehicle_context"
    assert schema.meta["identity_archetypes"]["Unit"]["confidence_tier"] == "context_match"
    assert schema.meta["identity_archetypes"]["Unit"]["attributes"] == {}
