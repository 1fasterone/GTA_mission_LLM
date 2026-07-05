"""Tests for the project-wide Kenney shortlist (src/unity_asset_resolver.py),
focused on concept-layer theming of the KenneyImporter grab-bag."""

import json

import pytest

from src.unity_asset_resolver import resolve_assets
from src.unity_schema import EntitySpec, UnitySchema


# A skydiving/aerial-combat theme: aircraft + debris. The catalog also holds a
# high-generic staircase and a crate that literal+generic scoring would surface
# but which belong to no project concept.
_ASSETS = [
    {"filename": "corsair.fbx", "category": "vehicle",
     "keywords": ["plane", "aircraft", "corsair", "fighter"],
     "kind_hints": ["vehicle"], "generic_score": 0},
    {"filename": "avion.fbx", "category": "vehicle",
     "keywords": ["plane", "aircraft", "airplane"],
     "kind_hints": ["vehicle"], "generic_score": 0},
    {"filename": "debris-bolt.fbx", "category": "obstacle",
     "keywords": ["debris", "scrap", "bolt", "wreckage"],
     "kind_hints": ["obstacle"], "generic_score": 0},
    {"filename": "debris-plate.fbx", "category": "obstacle",
     "keywords": ["debris", "scrap", "plate"],
     "kind_hints": ["obstacle"], "generic_score": 0},
    {"filename": "steps.fbx", "category": "stairs",
     "keywords": ["stairs", "steps", "staircase"],
     "kind_hints": ["prop"], "generic_score": 9},        # high generic filler
    {"filename": "box.fbx", "category": "prop",
     "keywords": ["box", "crate", "cargo"],
     "kind_hints": ["prop"], "generic_score": 8},         # high generic filler
]

_LAYERS = {
    "_config": {},
    "layers": {
        "aircraft_aerial": {
            "vocabulary": ["plane", "aircraft", "airplane", "corsair"],
            "kinds": ["vehicle"], "categories": []},
        "debris_wreckage": {
            "vocabulary": ["debris", "scrap", "wreckage", "bolt"],
            "kinds": ["obstacle"], "categories": []},
        "stairs_ramp": {
            "vocabulary": ["stairs", "steps", "staircase"],
            "kinds": [], "categories": ["stairs"]},
        "container_cargo": {
            "vocabulary": ["box", "crate", "cargo"],
            "kinds": [], "categories": []},
    },
}


def _write_catalog(lookups, assets, dup=None):
    entries = [{**a, "root": "Models/FBX format", "relpath": a["filename"],
                "game_contexts": ["any"], "format": "fbx", "pack": "t",
                "scale": 1.0, "curated": False} for a in assets]
    if dup:                       # same filename under a second pack (dedupe test)
        entries.append({**entries[dup], "pack": "t2"})
    (lookups / "unity").mkdir(parents=True, exist_ok=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps({"version": 1, "assets": entries}), encoding="utf-8")


def _aerial_schema():
    return UnitySchema(
        project_name="Sky", company_name="C",
        description="aerial combat dodging falling debris",
        game_type="3D", platform="pc", render_pipeline="URP",
        visual_style="low_poly", unity_version="6000.2", scenes=["Main"],
        entities=[
            EntitySpec(name="Corsairs", kind="prop", model_hint="corsair plane aircraft"),
            EntitySpec(name="FallingDebris", kind="prop", model_hint="debris scrap wreckage"),
        ])


def test_shortlist_themed_to_project_concepts(tmp_path):
    lookups = tmp_path / "lookups"
    _write_catalog(lookups, _ASSETS)
    (lookups / "unity" / "concept_layers.json").write_text(
        json.dumps(_LAYERS), encoding="utf-8")
    picks = {a["filename"] for a in resolve_assets(_aerial_schema(),
                                                   lookups_dir=str(lookups))}
    assert {"corsair.fbx", "debris-bolt.fbx"} <= picks   # on-theme included
    assert "steps.fbx" not in picks                       # off-theme filler dropped
    assert "box.fbx" not in picks                         # even high generic-score


def test_shortlist_without_concept_layers_keeps_generic_filler(tmp_path):
    # No concept_layers.json → original behavior: the high-generic staircase
    # rides the generic baseline into the list. Proves the theming is a pure,
    # optional layer on top of the old resolver.
    lookups = tmp_path / "lookups"
    _write_catalog(lookups, _ASSETS)               # note: NO concept_layers.json
    picks = {a["filename"] for a in resolve_assets(_aerial_schema(),
                                                   lookups_dir=str(lookups))}
    assert "steps.fbx" in picks


def test_shortlist_dedupes_by_filename(tmp_path):
    lookups = tmp_path / "lookups"
    _write_catalog(lookups, _ASSETS, dup=0)        # corsair.fbx under two packs
    (lookups / "unity" / "concept_layers.json").write_text(
        json.dumps(_LAYERS), encoding="utf-8")
    names = [a["filename"] for a in resolve_assets(_aerial_schema(),
                                                   lookups_dir=str(lookups))]
    assert names.count("corsair.fbx") == 1


def test_2d_game_still_returns_empty(tmp_path):
    lookups = tmp_path / "lookups"
    _write_catalog(lookups, _ASSETS)
    (lookups / "unity" / "concept_layers.json").write_text(
        json.dumps(_LAYERS), encoding="utf-8")
    schema = _aerial_schema()
    schema.game_type = "2D"
    assert resolve_assets(schema, lookups_dir=str(lookups)) == []
