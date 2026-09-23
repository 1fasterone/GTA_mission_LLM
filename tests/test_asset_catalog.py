"""Tests for the deterministic asset catalog builder (src/unity_asset_catalog.py)."""

import json
from pathlib import Path

import pytest

from src.unity_asset_catalog import (
    asset_vocabulary,
    build_catalog,
    catalog_json,
    derive_entry,
    is_stale,
    load_catalog,
    scan_assets,
    tokenize,
    write_catalog,
    write_sketchfab_entry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_tree(root: Path, names: list[str]) -> Path:
    """Create dummy .fbx files (plus noise files) under root."""
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"FBX-DUMMY")
    return root


BASIC_NAMES = [
    "tree-pine.fbx",
    "ramp-large.fbx",
    "banner-hanging.fbx",
    "obstacle-box.fbx",
    "character-skate-boy.fbx",
    # excluded content:
    "Textures/colormap.png",
    "Textures/detail.fbx",          # model inside Textures/ — skipped
    "Players/Player_Run.fbx",       # anim clip library — skipped
    "Defaults/prop/Prop.fbx",       # default pack — skipped
    "tree-pine.fbx.meta",           # meta — wrong extension, skipped
    "material.mtl",                 # non-model — skipped
]


# ── scanner ───────────────────────────────────────────────────────────────────

def test_scan_excludes_textures_players_defaults_and_nonmodels(tmp_path):
    root = _make_tree(tmp_path / "lib", BASIC_NAMES)
    files = [p.name for p, _ in scan_assets([root])]
    assert files == sorted([
        "banner-hanging.fbx", "character-skate-boy.fbx",
        "obstacle-box.fbx", "ramp-large.fbx", "tree-pine.fbx",
    ])


def test_scan_missing_root_is_empty():
    assert scan_assets([Path("does/not/exist")]) == []


# ── entry derivation ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,category,must_have_keywords,kind_hints", [
    ("tree-pine.fbx",           "nature",    {"tree", "forest", "pine"},   ["tree"]),
    ("ramp-large.fbx",          "track",     {"ramp", "slope", "jump"},    ["ramp"]),
    ("rail-high.fbx",           "track",     {"rail", "grind"},            ["rail"]),
    ("banner-hanging.fbx",      "prop",      {"banner", "flag"},           ["prop"]),
    ("obstacle-box.fbx",        "obstacle",  {"obstacle", "barrier"},      ["obstacle", "hazard"]),
    ("character-skate-boy.fbx", "character", {"character", "npc"},         ["npc", "crowd"]),
    ("skateboard.fbx",          "vehicle",   {"skateboard", "board"},      ["vehicle"]),
    ("helix-large-left.fbx",    "track",     {"helix", "spiral"},          ["ramp", "rail", "road", "obstacle"]),
    ("floor-concrete.fbx",      "structure", {"floor", "ground"},          ["ground", "road"]),
    ("mystery-thing.fbx",       "prop",      {"mystery", "thing"},         ["prop"]),
])
def test_derive_entry_table(tmp_path, filename, category, must_have_keywords, kind_hints):
    root = _make_tree(tmp_path / "lib", [filename])
    entry = derive_entry(root / filename, root)
    assert entry["category"] == category
    assert must_have_keywords <= set(entry["keywords"])
    assert entry["kind_hints"] == kind_hints
    assert entry["format"] == "fbx"
    assert entry["curated"] is False


def test_noise_tokens_excluded_from_keywords(tmp_path):
    root = _make_tree(tmp_path / "lib", ["slant-solid-long-a.fbx"])
    entry = derive_entry(root / "slant-solid-long-a.fbx", root)
    assert "long" not in entry["keywords"]
    assert "a" not in entry["keywords"]
    assert "slant" in entry["keywords"]


def test_visual_compound_aliases_and_content_fingerprint(tmp_path):
    root = _make_tree(tmp_path / "lib", ["boat-speed-a.fbx", "checkpoint-arch.fbx"])
    boat = derive_entry(root / "boat-speed-a.fbx", root)
    arch = derive_entry(root / "checkpoint-arch.fbx", root)
    assert "speedboat" in boat["keywords"]
    assert "archway" in arch["keywords"]
    catalog = build_catalog([root])
    assert len(catalog["provenance"]["source_tree_fingerprint"]) == 32


def test_tokenize_camel_case():
    assert tokenize("PineTreeGrove") == ["pine", "tree", "grove"]
    assert tokenize("banner-hanging-double-high") == ["banner", "hanging", "double", "high"]


# ── catalog build: determinism + curated merge ────────────────────────────────

def test_build_is_byte_idempotent(tmp_path):
    root = _make_tree(tmp_path / "lib", BASIC_NAMES)
    a = catalog_json(build_catalog([root]))
    b = catalog_json(build_catalog([root]))
    assert a == b
    # assets sorted by relpath regardless of creation order
    names = [x["filename"] for x in build_catalog([root])["assets"]]
    assert names == sorted(names)


def test_curated_fields_win(tmp_path):
    root = _make_tree(tmp_path / "lib", ["tree-pine.fbx"])
    curated = tmp_path / "kenney_catalog.json"
    curated.write_text(json.dumps({"assets": [{
        "filename": "tree-pine.fbx",
        "category": "flora",
        "keywords": ["evergreen"],
        "generic_score": 9,
        "scale": 2.5,
    }]}), encoding="utf-8")
    entry = build_catalog([root], curated_path=curated)["assets"][0]
    assert entry["category"] == "flora"
    assert entry["keywords"] == ["evergreen"]
    assert entry["generic_score"] == 9
    assert entry["scale"] == 2.5
    assert entry["curated"] is True


def test_missing_curated_file_is_fine(tmp_path):
    root = _make_tree(tmp_path / "lib", ["tree-pine.fbx"])
    catalog = build_catalog([root], curated_path=tmp_path / "nope.json")
    assert len(catalog["assets"]) == 1
    assert catalog["assets"][0]["curated"] is False


def test_vocabulary_block(tmp_path):
    root = _make_tree(tmp_path / "lib", BASIC_NAMES)
    vocab = build_catalog([root])["vocabulary"]
    assert "nature" in vocab["categories"]
    assert vocab["categories"] == sorted(vocab["categories"])
    assert "tree" in vocab["tags"]
    assert len(vocab["tags"]) <= 40


# ── 2D sprites ────────────────────────────────────────────────────────────────

def test_sprite_entries_derived_and_merged(tmp_path):
    from src.unity_asset_catalog import derive_sprite_entry
    lib = _make_tree(tmp_path / "lib", ["tree-pine.fbx"])
    sprites = tmp_path / "sprites"
    sprites.mkdir()
    for n in ("fish_blue.png", "seaweed_green_a.png", "terrain_sand_top_a.png",
              "hud_number_3.png", "background_rock_a.png"):
        (sprites / n).write_bytes(b"PNG")
    (sprites / "notes.txt").write_bytes(b"x")     # non-sprite file skipped

    catalog = build_catalog([lib], sprite_roots=[sprites])
    by_name = {a["filename"]: a for a in catalog["assets"]}
    assert len(catalog["assets"]) == 6            # 1 fbx + 5 png
    fish = by_name["fish_blue.png"]
    assert fish["format"] == "png"
    assert fish["category"] == "creature"
    assert {"fish", "swim", "creature"} <= set(fish["keywords"])
    assert fish["kind_hints"] == ["enemy", "npc", "crowd"]
    assert by_name["seaweed_green_a.png"]["category"] == "vegetation"
    assert by_name["terrain_sand_top_a.png"]["category"] == "environment"
    assert by_name["hud_number_3.png"]["kind_hints"] == []   # UI: not spawnable
    # byte-idempotent with sprites included
    assert catalog_json(catalog) == catalog_json(
        build_catalog([lib], sprite_roots=[sprites]))


def test_sprite_pack_label_uses_pack_folder(tmp_path):
    from src.unity_asset_catalog import derive_sprite_entry
    root = tmp_path / "Models" / "FishPack" / "PNG" / "Default"
    root.mkdir(parents=True)
    (root / "fish_red.png").write_bytes(b"PNG")
    entry = derive_sprite_entry(root / "fish_red.png", root)
    assert entry["pack"] == "kenney_fishpack"


# ── staleness / write / load ──────────────────────────────────────────────────

def test_write_and_staleness(tmp_path):
    root = _make_tree(tmp_path / "lib", ["tree-pine.fbx"])
    catalog = build_catalog([root])
    out = tmp_path / "lookups" / "unity" / "asset_catalog.json"
    assert is_stale(catalog, out)          # not written yet
    write_catalog(catalog, out)
    assert not is_stale(catalog, out)      # fresh
    _make_tree(root, ["ramp-large.fbx"])   # library changed
    assert is_stale(build_catalog([root]), out)


def test_content_change_is_stale_even_when_paths_are_unchanged(tmp_path):
    root = _make_tree(tmp_path / "lib", ["tree-pine.fbx"])
    catalog = build_catalog([root])
    out = tmp_path / "asset_catalog.json"
    write_catalog(catalog, out)
    assert catalog["provenance"]["builder"] == "src.unity_asset_catalog.build_catalog"
    assert catalog["provenance"]["model_roots"] == [root.as_posix()]
    (root / "tree-pine.fbx").write_bytes(b"changed source bytes")
    assert is_stale(build_catalog([root]), out)


def test_load_catalog_falls_back_to_curated(tmp_path):
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "kenney_catalog.json").write_text(
        json.dumps({"assets": [{"filename": "x.fbx"}]}), encoding="utf-8")
    assert load_catalog(str(lookups))["assets"][0]["filename"] == "x.fbx"
    assert load_catalog(str(tmp_path / "empty")) == {}


# ── Sketchfab catalog merge ────────────────────────────────────────────────────

def test_load_catalog_tags_legacy_entries_kenney(tmp_path):
    """Pre-existing catalogs predate the `source` field — load_catalog must
    backfill source="kenney" rather than leaving it absent."""
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps({"vocabulary": {}, "assets": [{"filename": "tree.fbx"}]}),
        encoding="utf-8")
    assets = load_catalog(str(lookups))["assets"]
    assert assets[0]["source"] == "kenney"


def test_load_catalog_merges_sketchfab_partition(tmp_path):
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps({"vocabulary": {"categories": ["nature"], "tags": ["tree"]},
                   "assets": [{"filename": "tree.fbx", "source": "kenney"}]}),
        encoding="utf-8")
    (lookups / "unity" / "sketchfab_catalog.json").write_text(
        json.dumps({"version": 1, "entries": [
            {"filename": "red-heel-abc.fbx", "source": "sketchfab",
             "license": "by", "sketchfab_uid": "abc"},
        ]}), encoding="utf-8")

    catalog = load_catalog(str(lookups))
    sources = {a["filename"]: a["source"] for a in catalog["assets"]}
    assert sources == {"tree.fbx": "kenney", "red-heel-abc.fbx": "sketchfab"}
    # vocabulary is untouched by the merge — stays scoped to the Kenney build.
    assert catalog["vocabulary"] == {"categories": ["nature"], "tags": ["tree"]}


def test_load_catalog_sketchfab_only_no_base_catalog(tmp_path):
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "sketchfab_catalog.json").write_text(
        json.dumps({"version": 1, "entries": [
            {"filename": "x.fbx", "source": "sketchfab", "sketchfab_uid": "u1"},
        ]}), encoding="utf-8")
    assets = load_catalog(str(lookups))["assets"]
    assert [a["filename"] for a in assets] == ["x.fbx"]


def test_write_sketchfab_entry_dedupes_by_uid(tmp_path):
    out_path = tmp_path / "lookups" / "unity" / "sketchfab_catalog.json"
    write_sketchfab_entry({"filename": "old.fbx", "sketchfab_uid": "u1"}, out_path)
    data = write_sketchfab_entry({"filename": "new.fbx", "sketchfab_uid": "u1"}, out_path)
    assert len(data["entries"]) == 1
    assert data["entries"][0]["filename"] == "new.fbx"

    write_sketchfab_entry({"filename": "other.fbx", "sketchfab_uid": "u2"}, out_path)
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert {e["sketchfab_uid"] for e in on_disk["entries"]} == {"u1", "u2"}


def test_write_sketchfab_entry_byte_idempotent(tmp_path):
    out_path = tmp_path / "sketchfab_catalog.json"
    write_sketchfab_entry({"filename": "x.fbx", "sketchfab_uid": "u1"}, out_path)
    first = out_path.read_bytes()
    write_sketchfab_entry({"filename": "x.fbx", "sketchfab_uid": "u1"}, out_path)
    assert out_path.read_bytes() == first


# ── prompt vocabulary helper ──────────────────────────────────────────────────

def test_asset_vocabulary_renders_and_degrades(tmp_path):
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(json.dumps({
        "vocabulary": {"categories": ["nature", "track"], "tags": ["tree", "ramp"]},
        "assets": [],
    }), encoding="utf-8")
    text = asset_vocabulary(str(lookups))
    assert "model_hint" in text
    assert "nature, track" in text
    assert "tree, ramp" in text
    assert asset_vocabulary(str(tmp_path / "missing")) == ""


# ── real-tree smoke ───────────────────────────────────────────────────────────

@pytest.mark.skipif(not (REPO_ROOT / "Models" / "FBX format").is_dir(),
                    reason="repo Models/FBX format not present")
def test_real_tree_smoke():
    root = REPO_ROOT / "Models" / "FBX format"
    catalog = build_catalog([root],
                            curated_path=REPO_ROOT / "lookups" / "unity" / "kenney_catalog.json")
    assets = catalog["assets"]
    assert len(assets) >= 170
    for a in assets:
        assert (root / a["relpath"]).is_file()
        assert a["keywords"], f"no keywords for {a['filename']}"
    # curated skate/marble entries merged in
    assert any(a["curated"] for a in assets)
