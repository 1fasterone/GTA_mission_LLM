"""Tests for per-entity asset resolution (src/unity_entity_asset_resolver.py)."""

import json
from pathlib import Path

import pytest

from src.unity_entity_asset_resolver import resolve_entity_assets, texture_dirs_for
from src.unity_model_resolver import ResolvedModel
from src.unity_schema import EntitySpec, UnitySchema


# ── fixtures ──────────────────────────────────────────────────────────────────

CATALOG_ASSETS = [
    {"filename": "tree-pine.fbx",   "relpath": "tree-pine.fbx",
     "category": "nature", "keywords": ["tree", "pine", "forest", "nature"],
     "kind_hints": ["tree"], "generic_score": 0, "scale": 1.0},
    {"filename": "tree-tall.fbx",   "relpath": "tree-tall.fbx",
     "category": "nature", "keywords": ["tree", "forest", "nature"],
     "kind_hints": ["tree"], "generic_score": 0, "scale": 1.0},
    {"filename": "tree-large.fbx",  "relpath": "tree-large.fbx",
     "category": "nature", "keywords": ["tree", "forest", "nature"],
     "kind_hints": ["tree"], "generic_score": 0, "scale": 1.0},
    {"filename": "ramp-large.fbx",  "relpath": "ramp-large.fbx",
     "category": "track", "keywords": ["ramp", "slope", "skate", "jump"],
     "kind_hints": ["ramp"], "generic_score": 0, "scale": 1.0},
    {"filename": "pallet.fbx",      "relpath": "pallet.fbx",
     "category": "prop", "keywords": ["pallet", "crate", "box", "warehouse"],
     "kind_hints": ["prop"], "generic_score": 8, "scale": 1.0},
    {"filename": "banner.fbx",      "relpath": "banner.fbx",
     "category": "prop", "keywords": ["banner", "flag", "sign"],
     "kind_hints": ["prop"], "generic_score": 3, "scale": 1.0},
]


@pytest.fixture
def env(tmp_path):
    """Asset files + lookups dir with a small catalog; returns (lookups, cfg)."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for a in CATALOG_ASSETS:
        (lib / a["relpath"]).write_bytes(b"FBX")
    catalog = {
        "version": 1,
        "vocabulary": {"categories": [], "tags": []},
        "assets": [{**a, "root": lib.as_posix(), "game_contexts": ["any"],
                    "format": "fbx", "pack": "test", "curated": False}
                   for a in CATALOG_ASSETS],
    }
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8")
    return lookups


def _schema(entities):
    return UnitySchema(project_name="T", company_name="C",
                       description="a skate park in a forest",
                       game_type="3D", platform="pc", render_pipeline="URP",
                       unity_version="6000.2", scenes=["Main"],
                       entities=entities)


def _default_models(tmp_path):
    src = tmp_path / "Prop.fbx"
    src.write_bytes(b"FBX")
    prop = ResolvedModel(kind="prop", src=str(src),
                         dest_rel="Models/Default/Prop/Prop.fbx",
                         resource_path="Models/Default/Prop/Prop", rig="generic")
    tree_src = tmp_path / "Tree.fbx"
    tree_src.write_bytes(b"FBX")
    tree = ResolvedModel(kind="tree", src=str(tree_src),
                         dest_rel="Models/Default/Tree/Tree.fbx",
                         resource_path="Models/Default/Tree/Tree", rig="generic")
    return {"prop": prop, "tree": tree}


# ── tier precedence ───────────────────────────────────────────────────────────

def test_explicit_kind_wins_over_catalog(env, tmp_path):
    all_models = _default_models(tmp_path)
    used = {"tree": all_models["tree"]}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Pines", kind="tree", count=3)]),
        used, all_models, lookups_dir=str(env))
    assert paths["Pines"] == "Models/Default/Tree/Tree"


def test_default_prop_kind_goes_to_catalog_not_prop_fbx(env, tmp_path):
    # The schema-default kind "prop" must NOT funnel to Prop.fbx when the name
    # carries meaning — that was the everything-is-one-model symptom.
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env))
    assert paths["PineTrees"].startswith("Models/Kenney/Nature/tree-")
    # the pick was registered for the copy/.meta chain
    assert any(k.startswith("asset:tree-") for k in used)


def test_model_hint_escape_hatch_keeps_default_prop(env, tmp_path):
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Thing", kind="prop", count=1,
                            model_hint="default prop")]),
        used, all_models, lookups_dir=str(env))
    assert paths["Thing"] == "Models/Default/Prop/Prop"


def test_model_hint_steers_the_match(env, tmp_path):
    all_models = _default_models(tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Decorations", kind="prop", count=2,
                            model_hint="hanging banner flag")]),
        {}, all_models, lookups_dir=str(env))
    assert paths["Decorations"] == "Models/Kenney/Prop/banner"


def test_catalog_pick_shape(env, tmp_path):
    used: dict = {}
    resolve_entity_assets(
        _schema([EntitySpec(name="JumpRamp", kind="prop", count=1)]),
        used, {}, lookups_dir=str(env))
    rm = used["asset:ramp-large"]
    assert rm.rig == "generic"
    assert rm.dest_rel == "Models/Kenney/Track/ramp-large.fbx"
    assert rm.resource_path == "Models/Kenney/Track/ramp-large"
    assert Path(rm.src).is_file()


# ── variety ───────────────────────────────────────────────────────────────────

def test_same_query_groups_rotate_distinct_models(env, tmp_path):
    entities = [EntitySpec(name=f"TreeCluster{i}", kind="prop", count=2,
                           model_hint="forest tree")
                for i in range(3)]
    paths_a = resolve_entity_assets(_schema(entities), {}, {}, lookups_dir=str(env))
    assert len(set(paths_a.values())) == 3          # three distinct tree models
    # deterministic across runs
    paths_b = resolve_entity_assets(_schema(entities), {}, {}, lookups_dir=str(env))
    assert paths_a == paths_b


def test_shared_model_registered_once(env, tmp_path):
    # 5 groups, only 3 tree variants — rotation wraps, models dedupe in used.
    entities = [EntitySpec(name=f"G{i}", kind="prop", count=1,
                           model_hint="forest tree") for i in range(5)]
    used: dict = {}
    resolve_entity_assets(_schema(entities), used, {}, lookups_dir=str(env))
    assert sum(1 for k in used if k.startswith("asset:tree-")) == 3


def test_plural_entity_name_matches_singular_keyword(env, tmp_path):
    # "PineTrees" (no hint) must still find the "tree"-keyword assets.
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        {}, {}, lookups_dir=str(env))
    assert paths["PineTrees"].startswith("Models/Kenney/Nature/tree-")


def test_description_words_alone_never_qualify_a_match(env, tmp_path):
    # Regression (TerminalHorizon, live): entity "AtmosphericClouds" (kind=prop,
    # no model_hint) grabbed steps.fbx because the SCHEMA DESCRIPTION word
    # "vertical" (weight 0.1) qualified the asset and the +0.5 prop kind bonus
    # carried it over the threshold. Description words must only break ties
    # between assets that already match a strong (name/kind/hint) token.
    import json as _json
    cat_path = env / "unity" / "asset_catalog.json"
    cat = _json.loads(cat_path.read_text(encoding="utf-8"))
    # steps-like asset: prop kind hint + a keyword that appears in the scene
    # description but not in the entity name
    root = cat["assets"][0]["root"]
    cat["assets"].append({
        "filename": "steps.fbx", "relpath": "steps.fbx", "root": root,
        "category": "stairs", "keywords": ["stairs", "steps", "vertical"],
        "kind_hints": ["prop"], "generic_score": 9, "scale": 1.0,
        "game_contexts": ["any"], "format": "fbx", "pack": "t", "curated": True,
    })
    cat_path.write_text(_json.dumps(cat), encoding="utf-8")
    (Path(root) / "steps.fbx").write_bytes(b"FBX")

    schema = UnitySchema(
        project_name="T", company_name="C",
        description="survival in a vertical atmospheric world",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", scenes=["Main"],
        entities=[EntitySpec(name="AtmosphericClouds", kind="prop", count=20)])
    with pytest.raises(ValueError, match="AtmosphericClouds"):
        resolve_entity_assets(schema, {}, {}, lookups_dir=str(env), strict=True)


# ── fallback policy ───────────────────────────────────────────────────────────

def test_strict_no_match_raises_with_suggestions(env, tmp_path):
    all_models = _default_models(tmp_path)
    with pytest.raises(ValueError) as exc:
        resolve_entity_assets(
            _schema([EntitySpec(name="Zzgrbl", kind="dragon", count=1)]),
            {}, all_models, lookups_dir=str(env), strict=True)
    msg = str(exc.value)
    assert "[assets.strict]" in msg
    assert "Zzgrbl" in msg
    assert "model_hint" in msg


def test_nonstrict_no_match_falls_back_to_prop(env, tmp_path):
    all_models = _default_models(tmp_path)
    used: dict = {}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Zzgrbl", kind="dragon", count=1)]),
        used, all_models, lookups_dir=str(env), strict=False)
    assert paths["Zzgrbl"] == "Models/Default/Prop/Prop"
    assert "prop" in used                        # fallback registered for copy


def test_missing_asset_file_is_skipped(env, tmp_path):
    # Catalog entry whose file vanished must not be picked.
    (Path(json.loads((env / "unity" / "asset_catalog.json")
                     .read_text())["assets"][0]["root"]) / "tree-pine.fbx").unlink()
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Pine", kind="prop", model_hint="pine tree")]),
        {}, {}, lookups_dir=str(env))
    assert "tree-pine" not in paths["Pine"]


# ── textures ──────────────────────────────────────────────────────────────────

def test_texture_dirs_for_catalog_picks_only(env, tmp_path):
    lib = tmp_path / "lib"
    (lib / "Textures").mkdir()
    (lib / "Textures" / "colormap.png").write_bytes(b"PNG")
    used: dict = {}
    resolve_entity_assets(
        _schema([EntitySpec(name="JumpRamp", kind="prop", count=1)]),
        used, _default_models(tmp_path), lookups_dir=str(env))
    dirs = texture_dirs_for(used)
    assert dirs == [lib / "Textures"]
    # default-pack-only projects ship no texture folders
    assert texture_dirs_for(_default_models(tmp_path)) == []
