"""Tests for per-entity asset resolution (src/unity_entity_asset_resolver.py)."""

import json
from pathlib import Path

import pytest

from src.unity_entity_asset_resolver import (resolve_entity_assets, texture_dirs_for,
                                             texture_dirs_with_res_root_for,
                                             load_kind_sizes)
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
    {"filename": "corsair.fbx",     "relpath": "corsair.fbx",
     "category": "aircraft", "keywords": ["plane", "corsair", "aircraft", "propeller"],
     "kind_hints": ["vehicle"], "generic_score": 0, "scale": 1.0},
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


def _vehicle_model(tmp_path):
    src = tmp_path / "Car.fbx"
    src.write_bytes(b"FBX")
    return ResolvedModel(kind="vehicle", src=str(src),
                         dest_rel="Models/Default/Vehicle/Car.fbx",
                         resource_path="Models/Default/Vehicle/Car", rig="generic")


def test_model_hint_overrides_default_pack_kind(env, tmp_path):
    # Live regression (TerminalHorizon): kind="vehicle" stopped at Car.fbx and
    # the "corsair plane" model_hint never reached the catalog.
    vehicle = _vehicle_model(tmp_path)
    used = {"vehicle": vehicle}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PlayerPlane", kind="vehicle", count=1,
                            model_hint="corsair plane")]),
        used, {"vehicle": vehicle}, lookups_dir=str(env))
    assert paths["PlayerPlane"] == "Models/Kenney/Aircraft/corsair"


def test_hinted_kind_falls_back_to_default_pack_on_catalog_miss(env, tmp_path):
    # A hint that matches nothing must fall back to the kind's default-pack
    # model — never to the generic prop, and never a strict hard-fail.
    vehicle = _vehicle_model(tmp_path)
    used = {"vehicle": vehicle}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="HoverPod", kind="vehicle", count=1,
                            model_hint="zzgrbl hoverpod")]),
        used, {"vehicle": vehicle}, lookups_dir=str(env), strict=True)
    assert paths["HoverPod"] == "Models/Default/Vehicle/Car"


def test_default_kind_hint_opts_back_into_tier1(env, tmp_path):
    # model_hint "default <kind>" is the deliberate escape hatch to tier 1.
    vehicle = _vehicle_model(tmp_path)
    used = {"vehicle": vehicle}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PlayerPlane", kind="vehicle", count=1,
                            model_hint="default vehicle")]),
        used, {"vehicle": vehicle}, lookups_dir=str(env))
    assert paths["PlayerPlane"] == "Models/Default/Vehicle/Car"


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


# ── procedural mesh routing ───────────────────────────────────────────────────

def _schema_with_meshes(entities, meshes):
    from src.unity_schema import ProceduralMesh
    s = _schema(entities)
    s.procedural_meshes = [ProceduralMesh(**m) for m in meshes]
    return s


def test_unmatched_entity_routes_to_procedural_mesh(env, tmp_path):
    # TerminalHorizon case: AtmosphericClouds entity + CloudLayer1 mesh in the
    # same schema — the mesh IS the content, strict must not fail.
    schema = _schema_with_meshes(
        [EntitySpec(name="AtmosphericClouds", kind="prop", count=20,
                    model_hint="atmospheric clouds")],
        [{"name": "CloudLayer1", "description": "volumetric cloud field",
          "mesh_type": "custom", "resolution": 64}])
    paths = resolve_entity_assets(schema, {}, {}, lookups_dir=str(env), strict=True)
    assert paths["AtmosphericClouds"] == "procedural:custom:64"


def test_procedural_route_prefers_best_token_overlap(env, tmp_path):
    schema = _schema_with_meshes(
        [EntitySpec(name="DebrisFields", kind="prop", count=12)],
        [{"name": "CloudLayer1", "description": "clouds", "mesh_type": "custom"},
         {"name": "DebrisField1", "description": "metal debris clusters",
          "mesh_type": "terrain", "resolution": 32}])
    paths = resolve_entity_assets(schema, {}, {}, lookups_dir=str(env), strict=True)
    assert paths["DebrisFields"] == "procedural:terrain:32"


def test_catalog_match_still_beats_procedural(env, tmp_path):
    # A real model wins over a procedural mesh with the same tokens.
    schema = _schema_with_meshes(
        [EntitySpec(name="PineTrees", kind="prop", count=3)],
        [{"name": "TreeField", "description": "trees", "mesh_type": "custom"}])
    paths = resolve_entity_assets(schema, {}, {}, lookups_dir=str(env), strict=True)
    assert paths["PineTrees"].startswith("Models/Kenney/Nature/tree-")


def test_no_token_overlap_still_fails_strict(env, tmp_path):
    schema = _schema_with_meshes(
        [EntitySpec(name="Zzgrbl", kind="dragon", count=1)],
        [{"name": "CloudLayer1", "description": "clouds", "mesh_type": "custom"}])
    with pytest.raises(ValueError, match="procedural_meshes"):
        resolve_entity_assets(schema, {}, {}, lookups_dir=str(env), strict=True)


# ── non-mesh kinds (ui / fx / background) ─────────────────────────────────────

def test_fx_kind_routes_to_particles_not_strict_fail(env, tmp_path):
    # Live regression (TerminalHorizon): kind="fx" entities ("StormCells")
    # hard-failed strict resolution — they never needed an FBX.
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="StormCells", kind="fx", count=6,
                            model_hint="storm cells fx")]),
        {}, {}, lookups_dir=str(env), strict=True)
    assert paths["StormCells"] == "fx:particles"


def test_ui_and_background_kinds_skip_spawn(env, tmp_path):
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="MenuUI", kind="ui", count=1,
                            model_hint="menu ui"),
                 EntitySpec(name="TitleBackground", kind="background", count=1,
                            model_hint="title background")]),
        {}, {}, lookups_dir=str(env), strict=True)
    assert paths["MenuUI"] == "skip:ui"
    assert paths["TitleBackground"] == "skip:background"


def test_fx_kind_still_prefers_catalog_match(env, tmp_path):
    # A non-mesh kind whose hint names a real asset gets the asset — the
    # particle/skip routing only fires when the catalog misses.
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="FallingLeaves", kind="fx", count=4,
                            model_hint="pine tree")]),
        {}, {}, lookups_dir=str(env), strict=True)
    assert paths["FallingLeaves"].startswith("Models/Kenney/Nature/tree-")


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


# ── 2D sprites ────────────────────────────────────────────────────────────────

def _add_sprites(env, tmp_path):
    import json as _json
    lib2d = tmp_path / "sprites"
    lib2d.mkdir(exist_ok=True)
    (lib2d / "fish_blue.png").write_bytes(b"PNG")
    cat_path = env / "unity" / "asset_catalog.json"
    cat = _json.loads(cat_path.read_text(encoding="utf-8"))
    cat["assets"].append({
        "filename": "fish_blue.png", "relpath": "fish_blue.png",
        "root": lib2d.as_posix(), "format": "png", "pack": "kenney_fishpack",
        "category": "creature", "keywords": ["fish", "swim", "creature", "enemy"],
        "kind_hints": ["enemy", "npc"], "game_contexts": ["2d"],
        "generic_score": 0, "scale": 1.0, "curated": False,
    })
    cat_path.write_text(_json.dumps(cat), encoding="utf-8")


def _schema_2d(entities):
    return UnitySchema(project_name="T", company_name="C",
                       description="an underwater side-scroller",
                       game_type="2D", platform="pc", render_pipeline="URP",
                       unity_version="6000.2", scenes=["Main"],
                       entities=entities)


def test_2d_game_prefers_sprite_over_model(env, tmp_path):
    _add_sprites(env, tmp_path)
    used: dict = {}
    paths = resolve_entity_assets(
        _schema_2d([EntitySpec(name="Fishes", kind="prop", count=5,
                               model_hint="swimming fish")]),
        used, {}, lookups_dir=str(env))
    assert paths["Fishes"] == "Sprites/Kenney/Creature/fish_blue"
    assert used["asset:fish_blue"].rig == "sprite"


def test_3d_game_never_picks_a_png(env, tmp_path):
    _add_sprites(env, tmp_path)
    with pytest.raises(ValueError):   # no fbx fish exists → strict fail, not a png pick
        resolve_entity_assets(
            _schema([EntitySpec(name="Fishes", kind="prop", count=5,
                                model_hint="swimming fish")]),
            {}, {}, lookups_dir=str(env), strict=True)


def test_sprite_picks_have_no_fbx_texture_dirs(env, tmp_path):
    _add_sprites(env, tmp_path)
    (tmp_path / "Textures").mkdir(exist_ok=True)   # decoy parent Textures dir
    used: dict = {}
    resolve_entity_assets(
        _schema_2d([EntitySpec(name="Fishes", kind="prop", count=5,
                               model_hint="swimming fish")]),
        used, {}, lookups_dir=str(env))
    assert texture_dirs_for(used) == []


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


def test_texture_dirs_with_res_root_routes_by_source():
    """Root-caused live: every Sketchfab-sourced model rendered black in Play
    mode because textures were always copied to the hardcoded
    Assets/Resources/Models/Kenney/Textures/ path — not an ancestor of a
    Sketchfab pick's own Assets/Resources/Models/Sketchfab/<Category>/
    location, so Unity's up-the-parent-chain search never found them. This
    pins the destination-selection fix: each texture dir must pair with the
    SAME res_root the model itself was copied under."""
    kenney_dir = Path("/lib/kenney/tree-pine.fbx")
    sketchfab_dir = Path("/lib/sketchfab/car-drag-racing.fbx")
    used = {
        "asset:tree-pine": ResolvedModel(
            kind="asset:tree-pine", src=str(kenney_dir),
            dest_rel="Models/Kenney/Nature/tree-pine.fbx",
            resource_path="Models/Kenney/Nature/tree-pine", rig="generic",
            source="kenney"),
        "asset:car-drag-racing": ResolvedModel(
            kind="asset:car-drag-racing", src=str(sketchfab_dir),
            dest_rel="Models/Sketchfab/Vehicle/car-drag-racing.fbx",
            resource_path="Models/Sketchfab/Vehicle/car-drag-racing", rig="generic",
            source="sketchfab"),
    }

    def _fake_is_dir(self):
        return self.name == "Textures"
    import unittest.mock as mock
    with mock.patch.object(Path, "is_dir", _fake_is_dir):
        pairs = texture_dirs_with_res_root_for(used)

    by_root = {res_root: tex_dir for tex_dir, res_root in pairs}
    assert by_root["Models/Kenney"] == kenney_dir.parent / "Textures"
    assert by_root["Models/Sketchfab"] == sketchfab_dir.parent / "Textures"


# ── concept layers (semantic classification: concept → layer → best asset) ────

# A staircase and a real platform BOTH carry the keyword "platform". The
# staircase has a higher generic_score, so literal token→filename matching used
# to pick it for a platform entity ("StratoPlatform → steps.fbx"). Concept
# layers classify the entity into platform_walkway, where the real platform is
# a STRONG (category) member and the staircase is not.
_DRIFT_ASSETS = [
    {"filename": "steps.fbx", "relpath": "steps.fbx", "category": "stairs",
     "keywords": ["stairs", "steps", "staircase", "platform"],
     "kind_hints": ["prop", "building"], "generic_score": 9, "scale": 1.0},
    {"filename": "platform_high.fbx", "relpath": "platform_high.fbx",
     "category": "structure",
     "keywords": ["platform", "ledge", "walkway", "deck", "structure"],
     "kind_hints": ["building", "ground", "prop"], "generic_score": 1, "scale": 1.0},
    {"filename": "alien.fbx", "relpath": "alien.fbx", "category": "character",
     "keywords": ["alien", "creature", "monster", "npc"],
     "kind_hints": ["enemy", "npc"], "generic_score": 0, "scale": 1.0},
]

_CONCEPT_LAYERS = {
    "_config": {"kind_prior": 1.0, "layer_band": 0.6},
    "layers": {
        "platform_walkway": {
            "vocabulary": ["platform", "ledge", "walkway", "deck", "structure"],
            "kinds": ["platform", "structure"], "categories": ["platform", "structure"]},
        "stairs_ramp": {
            "vocabulary": ["stairs", "steps", "staircase", "ramp"],
            "kinds": ["ramp", "stairs"], "categories": ["stairs", "ramp"]},
        "creature_animal": {
            "vocabulary": ["creature", "monster", "dragon", "beast", "alien"],
            "kinds": ["creature", "enemy", "npc"], "categories": ["creature", "character"]},
    },
}


@pytest.fixture
def layered_env(tmp_path):
    """A catalog with the platform/steps drift pair + a concept_layers.json."""
    lib = tmp_path / "lib"
    lib.mkdir()
    for a in _DRIFT_ASSETS:
        (lib / a["relpath"]).write_bytes(b"FBX")
    catalog = {"version": 1, "vocabulary": {"categories": [], "tags": []},
               "assets": [{**a, "root": lib.as_posix(), "game_contexts": ["any"],
                           "format": "fbx", "pack": "t", "curated": False}
                          for a in _DRIFT_ASSETS]}
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8")
    (lookups / "unity" / "concept_layers.json").write_text(
        json.dumps(_CONCEPT_LAYERS), encoding="utf-8")
    return lookups


def test_concept_layer_beats_higher_generic_score_drift(layered_env):
    # The core StratoPlatform regression: without concept layers the staircase
    # (generic_score 9) out-points the real platform (generic_score 1) on the
    # shared "platform" keyword. The platform_walkway layer + category-align
    # bonus must flip that.
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="StratoPlatform", kind="platform", count=1)]),
        {}, {}, lookups_dir=str(layered_env), strict=True)
    assert paths["StratoPlatform"] == "Models/Kenney/Structure/platform_high"


def test_kind_constrains_layer_over_adjective(layered_env):
    # "PlatformSteps" kind=stairs: the name carries the adjective "platform"
    # (→ platform_walkway) but the structured kind pins the band to
    # stairs_ramp, so the staircase wins over the platform. Kind outranks a
    # stray descriptive token.
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PlatformSteps", kind="stairs", count=1)]),
        {}, {}, lookups_dir=str(layered_env), strict=True)
    assert paths["PlatformSteps"] == "Models/Kenney/Stairs/steps"


def test_best_in_layer_replaces_generic_prop_fallback(layered_env, tmp_path):
    # A "Wyverns" (kind=beast) has no literal catalog hit — no entity token,
    # not even the kind, appears in any asset's keywords — but "beast" names
    # the creature layer (vocab hit). The soft best-in-layer pick must choose
    # the real creature instead of collapsing to the generic prop.
    all_models = _default_models(tmp_path)   # provides a "prop" default
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Wyverns", kind="beast", count=1)]),
        {}, all_models, lookups_dir=str(layered_env), strict=False)
    assert paths["Wyverns"] == "Models/Kenney/Character/alien"


def test_concept_layer_absent_reverts_to_global_behavior(tmp_path):
    # Without concept_layers.json the best-in-layer rescue is gone: the same
    # "Wyverns" (no literal catalog hit) collapses to the generic prop, exactly
    # as before this feature. Proves the layer file is a pure, optional
    # enhancement — absent → today's behavior verbatim.
    lib = tmp_path / "lib"
    lib.mkdir()
    for a in _DRIFT_ASSETS:
        (lib / a["relpath"]).write_bytes(b"FBX")
    catalog = {"version": 1, "vocabulary": {"categories": [], "tags": []},
               "assets": [{**a, "root": lib.as_posix(), "game_contexts": ["any"],
                           "format": "fbx", "pack": "t", "curated": False}
                          for a in _DRIFT_ASSETS]}
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8")   # note: NO concept_layers.json
    all_models = _default_models(tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Wyverns", kind="beast", count=1)]),
        {}, all_models, lookups_dir=str(lookups), strict=False)
    assert paths["Wyverns"] == "Models/Default/Prop/Prop"


# ── Sketchfab Tier S (config-gated, runs before the Kenney catalog tiers) ─────

def _add_sketchfab_entry(lookups: Path, tmp_path: Path, *, uid="sk1",
                         filename="ultimate-pine-sk1.fbx",
                         keywords=("pine", "tree", "forest"),
                         kind_hints=("tree", "prop"), category="nature",
                         license="cc0", author_username="janedoe") -> dict:
    sk_root = tmp_path / "sketchfab_lib"
    sk_root.mkdir(exist_ok=True)
    (sk_root / filename).write_bytes(b"FBX")
    entry = {
        "filename": filename, "relpath": filename, "root": sk_root.as_posix(),
        "format": "fbx", "pack": "sketchfab", "source": "sketchfab",
        "category": category, "description": "test sketchfab pick",
        "keywords": list(keywords), "kind_hints": list(kind_hints),
        "game_contexts": ["any"], "generic_score": 0, "scale": 1.0,
        "curated": False, "query": "pine tree", "sketchfab_uid": uid,
        "sketchfab_url": f"https://sketchfab.com/3d-models/{uid}",
        "license": license, "author": "Jane Doe",
        "author_username": author_username, "author_url": "",
        "attribution": f'"Pine Tree" by Jane Doe is licensed under {license.upper()}.',
        "fetched_at": "2026-01-01T00:00:00Z",
    }
    (lookups / "unity" / "sketchfab_catalog.json").write_text(
        json.dumps({"version": 1, "entries": [entry]}), encoding="utf-8")
    return entry


def test_sketchfab_disabled_is_byte_identical_to_today(env, tmp_path):
    """The single most important regression guard for this feature: a
    frozen Sketchfab entry sitting in the cache must have ZERO effect unless
    sketchfab.enabled is explicitly true — absent/None/false all mean
    'today's Kenney-only behavior, untouched'."""
    _add_sketchfab_entry(env, tmp_path)
    all_models = _default_models(tmp_path)

    for sk_cfg in (None, {}, {"enabled": False}):
        used = {"prop": all_models["prop"]}
        paths = resolve_entity_assets(
            _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
            used, all_models, lookups_dir=str(env), sketchfab_cfg=sk_cfg)
        assert paths["PineTrees"].startswith("Models/Kenney/Nature/tree-")


def test_sketchfab_tier_wins_over_kenney_when_enabled(env, tmp_path):
    """'Query first, fall back to Kenney' means Tier S wins outright when it
    has a cached hit — it is never a mere tiebreaker against Kenney's score."""
    entry = _add_sketchfab_entry(env, tmp_path)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True})
    assert paths["PineTrees"] == f"Models/Sketchfab/Nature/{entry['filename']}".rsplit(".", 1)[0]
    assert any(k.startswith("asset:") for k in used)


def test_sketchfab_cache_miss_falls_through_to_kenney(env, tmp_path):
    """enabled=True but no Sketchfab entry matches this entity at all — Tier S
    must be a silent no-op, not an error, and Kenney's tiers still run."""
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["PineTrees"].startswith("Models/Kenney/Nature/tree-")


def test_sketchfab_auto_fetch_false_never_calls_fetch_and_freeze(env, tmp_path, monkeypatch):
    calls = {"n": 0}
    def _fake_fetch(*a, **kw):
        calls["n"] += 1
        return None
    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert calls["n"] == 0


def test_sketchfab_auto_fetch_registers_once_and_caches_across_calls(env, tmp_path, monkeypatch):
    """Simulates write()'s double _resolve_all_models() call in one run: the
    first resolution is the one place this feature is allowed to touch the
    network; the second must be a pure, idempotent cache hit — zero extra
    fetch_and_freeze calls."""
    calls = {"n": 0}

    def _fake_fetch(query, score_fn, *, client, blender_cfg, cache_dir,
                    lookups_dir, category, kind_hints, min_score,
                    preferred_author=None, vision_select_fn=None,
                    semantic_filter_fn=None):
        calls["n"] += 1
        entry = _add_sketchfab_entry(Path(lookups_dir), tmp_path, uid="live1",
                                     filename="live-fetched-pine.fbx")
        return entry

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    schema = _schema([EntitySpec(name="PineTrees", kind="prop", count=5)])
    sk_cfg = {"enabled": True, "auto_fetch": True}

    used1 = {"prop": all_models["prop"]}
    paths1 = resolve_entity_assets(schema, used1, all_models,
                                   lookups_dir=str(env), sketchfab_cfg=sk_cfg)
    assert calls["n"] == 1
    assert paths1["PineTrees"].startswith("Models/Sketchfab/")

    # Second call, fresh used_models (as _resolve_all_models rebuilds it each
    # invocation) but the SAME on-disk lookups dir — must be a pure cache hit.
    used2 = {"prop": all_models["prop"]}
    paths2 = resolve_entity_assets(schema, used2, all_models,
                                   lookups_dir=str(env), sketchfab_cfg=sk_cfg)
    assert calls["n"] == 1          # no second live fetch
    assert paths2["PineTrees"] == paths1["PineTrees"]


def test_sketchfab_fetch_failure_falls_through_cleanly(env, tmp_path, monkeypatch):
    """fetch_and_freeze returning None (no Blender, no license match, etc.) on
    auto_fetch must fall through to the Kenney tiers, never raise."""
    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", lambda *a, **kw: None)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert paths["PineTrees"].startswith("Models/Kenney/Nature/tree-")


# ── vision_select_fn gating: llm + sketchfab.vision_select must BOTH be set ──

def test_no_llm_never_builds_a_vision_select_fn(env, tmp_path, monkeypatch):
    """The common case (unity-validate's no-LLM replay, or any caller that
    doesn't pass llm at all): vision_select_fn must be None even with
    vision_select enabled in config — there's no LLM to call."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, vision_select_fn=None, **kw: seen.update(fn=vision_select_fn) or None)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True, "vision_select": True},
        llm=None)
    assert seen["fn"] is None


def test_llm_without_vision_select_config_never_builds_one(env, tmp_path, monkeypatch):
    """An llm IS available but the feature is off (today's default) — must
    stay byte-identical to before this parameter existed."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, vision_select_fn=None, **kw: seen.update(fn=vision_select_fn) or None)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True},  # vision_select absent
        llm=object())
    assert seen["fn"] is None


def test_llm_and_vision_select_together_build_an_entity_bound_closure(
        env, tmp_path, monkeypatch):
    """Both conditions met: a real callable reaches fetch_and_freeze, and
    calling it dispatches to select_best_candidate with THIS entity's own
    name/kind/hint — confirming the closure captured the right entity, not
    whichever one happened to run last in the loop."""
    seen_fn = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, vision_select_fn=None, **kw: seen_fn.update(fn=vision_select_fn) or None)
    calls = []
    monkeypatch.setattr(
        "src.unity_sketchfab_vision.select_best_candidate",
        lambda name, kind, desc, cands, *, client, llm: calls.append(
            (name, kind, desc)) or "picked-uid")

    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    fake_llm = object()
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5,
                            model_hint="tall pine forest tree")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True, "vision_select": True},
        llm=fake_llm)

    assert seen_fn["fn"] is not None
    result = seen_fn["fn"]([{"uid": "x"}])
    assert result == "picked-uid"
    assert calls == [("PineTrees", "prop", "tall pine forest tree")]


# ── semantic_filter_fn gating: sketchfab.semantic_select alone is enough ────
# Unlike vision_select_fn, this needs NO `llm` — it's a CLIP/embedding
# similarity score, not an LLM chat call — so it must be reachable even on
# unity-validate's no-LLM replay path, as long as the config flag is on.

def test_semantic_select_absent_never_builds_a_filter_fn(env, tmp_path, monkeypatch):
    """Today's default (config key absent): semantic_filter_fn must be None,
    byte-identical to before this parameter existed."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})  # semantic_select absent
    assert seen["fn"] is None


def test_semantic_select_enabled_builds_a_filter_fn_without_an_llm(env, tmp_path, monkeypatch):
    """The defining difference from vision_select_fn: no llm is threaded
    through at all here, and the closure must still be built — this is a
    deterministic embedding score, not something that needs the Pipeline
    LLM."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True, "semantic_select": True},
        llm=None)
    assert seen["fn"] is not None


def test_semantic_select_closure_binds_entity_query_and_thresholds(env, tmp_path, monkeypatch):
    """The built closure must dispatch to unity_semantic_embed.semantic_filter
    with THIS entity's own name/kind/query and the configured similarity
    floors — confirming it captured the right entity, not whichever one
    happened to run last in the loop, and that config thresholds actually
    reach the scorer."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    calls = []
    monkeypatch.setattr(
        "src.unity_semantic_embed.semantic_filter",
        lambda name, kind, concept_text, cands, *, client, min_image_similarity,
               min_text_similarity: calls.append(
            (name, kind, concept_text, min_image_similarity, min_text_similarity))
            or list(cands))

    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5,
                            model_hint="tall pine forest tree")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True, "semantic_select": True,
                       "min_image_similarity": 0.33, "min_text_similarity": 0.44},
        llm=None)

    assert seen["fn"] is not None
    result = seen["fn"]([{"uid": "x"}])
    assert result == [{"uid": "x"}]
    assert len(calls) == 1
    name, kind, concept_text, min_img, min_txt = calls[0]
    assert (name, kind) == ("PineTrees", "prop")
    assert concept_text  # the query text used to search — non-empty
    assert (min_img, min_txt) == (0.33, 0.44)


def test_sketchfab_tier_s_rejects_coincidental_kind_mismatch(env, tmp_path):
    """Root-caused live: 5 unrelated entities (pickup/crowd/enemy/prop x2) all
    resolved to the SAME collected Sketchfab prop ("traffic cone") because
    Tier S ran raw _score() with none of the _passes_kind_guard protection
    Tier 2b already has (the guard exists specifically for this failure class
    — see test_kind_guard_blocks_wood_plank_for_enemy below). A Sketchfab
    entry with a coincidentally-shared keyword ("crowd", from loose/broad
    uploader tags — its OWN kind_hints say "prop", not "crowd") must NOT
    outrank a real, correctly-tagged default for an unrelated entity."""
    entry = _add_sketchfab_entry(
        env, tmp_path, uid="cone1", filename="traffic-cone.fbx",
        keywords=("traffic", "cone", "road", "crowd"),  # "crowd" = coincidental tag
        kind_hints=("prop",), category="prop")
    all_models = _default_models(tmp_path)
    crowd_src = tmp_path / "Spectator.fbx"
    crowd_src.write_bytes(b"FBX")
    crowd = ResolvedModel(kind="crowd", src=str(crowd_src),
                          dest_rel="Models/Default/Crowd/Spectator.fbx",
                          resource_path="Models/Default/Crowd/Spectator", rig="generic")
    all_models["crowd"] = crowd
    used = {"prop": all_models["prop"], "crowd": crowd}

    paths = resolve_entity_assets(
        _schema([EntitySpec(name="BirdFlock", kind="crowd", model_hint="crowd", count=20)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})

    assert entry["filename"] not in paths["BirdFlock"]
    assert paths["BirdFlock"] == "Models/Default/Crowd/Spectator"


def test_kind_guard_blocks_wood_plank_for_enemy(env, tmp_path):
    """Same failure class via the Kenney tiers (the original hit-live
    regression the guard was built for) — kept here alongside the Tier S
    version above so both code paths are covered side by side."""
    plank = {"filename": "floor-plank.fbx", "relpath": "floor-plank.fbx",
             "category": "structure", "keywords": ["wood", "floor", "plank", "ground"],
             "kind_hints": ["structure"], "generic_score": 0, "scale": 1.0}
    (Path(env).parent / "lib" / "floor-plank.fbx").write_bytes(b"FBX")
    cat_path = env / "unity" / "asset_catalog.json"
    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    catalog["assets"].append({**plank, "root": (Path(env).parent / "lib").as_posix(),
                              "game_contexts": ["any"], "format": "fbx",
                              "pack": "test", "curated": False})
    cat_path.write_text(json.dumps(catalog), encoding="utf-8")

    all_models = _default_models(tmp_path)
    enemy_src = tmp_path / "Enemy.fbx"
    enemy_src.write_bytes(b"FBX")
    enemy = ResolvedModel(kind="enemy", src=str(enemy_src),
                          dest_rel="Models/Default/Enemy/Enemy.fbx",
                          resource_path="Models/Default/Enemy/Enemy", rig="generic")
    all_models["enemy"] = enemy
    used = {"prop": all_models["prop"], "enemy": enemy}

    paths = resolve_entity_assets(
        _schema([EntitySpec(name="GroundEnemies", kind="enemy", model_hint="ground", count=3)]),
        used, all_models, lookups_dir=str(env))

    assert "floor-plank" not in paths["GroundEnemies"]
    assert paths["GroundEnemies"] == "Models/Default/Enemy/Enemy"


# ── style coherence: sketchfab_query preference + author-affinity threading ──

def test_sketchfab_query_field_wins_over_model_hint_for_live_fetch(env, tmp_path, monkeypatch):
    """src/unity_sketchfab_translator.py's enriched sketchfab_query must be
    the query handed to a live fetch, not the mechanical model_hint/token
    fallback — that's the whole point of running the translator."""
    seen_queries = []

    def _fake_fetch(query, score_fn, **kw):
        seen_queries.append(query)
        return None

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5,
                            model_hint="pine tree",
                            sketchfab_query="grimdark low-fantasy pine tree")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert seen_queries == ["grimdark low-fantasy pine tree"]


def test_preferred_author_established_from_cache_hit_threads_to_next_entity(
        env, tmp_path, monkeypatch):
    """The FIRST entity resolves from an already-frozen cache entry (no live
    fetch at all) — its author must still become the preferred_author for
    the SECOND entity's live fetch."""
    _add_sketchfab_entry(env, tmp_path, uid="sk1", filename="pine-sk1.fbx",
                         author_username="opticube")

    seen_authors = []

    def _fake_fetch(query, score_fn, **kw):
        seen_authors.append(kw.get("preferred_author"))
        return None

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5),
                EntitySpec(name="StoneWalls", kind="prop", count=3,
                          model_hint="zzgrbl no catalog match")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    # PineTrees hit the cache (no fetch call); StoneWalls' live fetch must
    # have received the author established by PineTrees' cache hit.
    assert seen_authors == ["opticube"]


def test_preferred_author_established_from_live_fetch_threads_to_next_entity(
        env, tmp_path, monkeypatch):
    """The FIRST entity's live fetch succeeds and returns an author — the
    SECOND entity's fetch must be called with that author as preferred."""
    calls = []

    def _fake_fetch(query, score_fn, *, client, blender_cfg, cache_dir,
                    lookups_dir, category, kind_hints, min_score,
                    preferred_author=None, vision_select_fn=None,
                    semantic_filter_fn=None):
        calls.append(preferred_author)
        if len(calls) == 1:
            return _add_sketchfab_entry(
                Path(lookups_dir), tmp_path, uid="live1",
                filename="live-first.fbx", author_username="opticube")
        return None

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5),
                EntitySpec(name="StoneWalls", kind="prop", count=3,
                          model_hint="zzgrbl no catalog match")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert calls == [None, "opticube"]


def test_preferred_author_never_overwritten_by_later_fallback(env, tmp_path, monkeypatch):
    """Once established, preferred_author sticks for the rest of the run even
    when a later entity's own fetch falls back to a different author — no
    drift away from the first-established style anchor."""
    _add_sketchfab_entry(env, tmp_path, uid="sk1", filename="pine-sk1.fbx",
                         author_username="opticube")
    calls = []

    def _fake_fetch(query, score_fn, *, preferred_author=None, **kw):
        calls.append(preferred_author)
        return _add_sketchfab_entry(
            Path(kw["lookups_dir"]), tmp_path, uid=f"live{len(calls)}",
            filename=f"live{len(calls)}.fbx", author_username="someone_else")

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5),
                EntitySpec(name="StoneWalls", kind="prop", count=3,
                          model_hint="zzgrbl no catalog match a"),
                EntitySpec(name="Barrels", kind="prop", count=2,
                          model_hint="zzgrbl no catalog match b")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    # PineTrees establishes "opticube" from the cache; both later live
    # fetches must still be called with "opticube", even though each one's
    # OWN result comes back authored by "someone_else".
    assert calls == ["opticube", "opticube"]


# ── scale consistency: bounds-fit target, Sketchfab picks only ───────────────

def _kind_sizes_env(env, sizes, clamp_sizes=None):
    payload = {"sizes": sizes}
    if clamp_sizes is not None:
        payload["clamp_sizes"] = clamp_sizes
    (env / "unity" / "kind_sizes.json").write_text(
        json.dumps(payload), encoding="utf-8")
    return env


def test_load_kind_sizes_missing_file_returns_empty(tmp_path):
    assert load_kind_sizes(str(tmp_path / "nope")) == {}


def test_load_kind_sizes_drops_null_and_non_numeric(tmp_path):
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "kind_sizes.json").write_text(
        json.dumps({"sizes": {"vehicle": 4.5, "track": None, "prop": "bogus"}}),
        encoding="utf-8")
    assert load_kind_sizes(str(lookups)) == {"vehicle": 4.5}


def test_sketchfab_pick_gets_fit_size_from_kind_sizes(env, tmp_path):
    _kind_sizes_env(env, {"vehicle": 4.5})
    entry = _add_sketchfab_entry(env, tmp_path, filename="corsair-sk.fbx",
                                 keywords=("corsair", "plane", "aircraft"),
                                 kind_hints=("vehicle",), category="vehicle")
    all_models = _default_models(tmp_path)
    used = {}
    resolve_entity_assets(
        _schema([EntitySpec(name="PlayerPlane", kind="vehicle", count=1,
                            model_hint="corsair plane")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True})
    picks = [m for k, m in used.items() if k.startswith("asset:")]
    assert picks and picks[0].source == "sketchfab"
    assert picks[0].fit_size == 4.5


def test_kenney_pick_never_gets_fit_size(env, tmp_path):
    # Even with a matching kind_sizes entry, a Kenney/catalog pick's scale is
    # already pack-internally consistent — bounds-fit is Sketchfab-only.
    _kind_sizes_env(env, {"vehicle": 4.5})
    all_models = _default_models(tmp_path)
    used = {}
    resolve_entity_assets(
        _schema([EntitySpec(name="PlayerPlane", kind="vehicle", count=1,
                            model_hint="corsair plane")]),
        used, all_models, lookups_dir=str(env))
    picks = [m for k, m in used.items() if k.startswith("asset:")]
    assert picks and picks[0].source == "kenney"
    assert picks[0].fit_size == 0.0


def test_sketchfab_pick_with_unmapped_kind_gets_no_fit(env, tmp_path):
    # kind_sizes.json has no entry for "prop" here — fit_size stays 0.0, not
    # some arbitrary default.
    _kind_sizes_env(env, {"vehicle": 4.5})
    _add_sketchfab_entry(env, tmp_path)   # kind_hints=("tree","prop")
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="PineTrees", kind="prop", count=5)]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True})
    picks = [m for k, m in used.items() if k.startswith("asset:")]
    assert picks and picks[0].source == "sketchfab"
    assert picks[0].fit_size == 0.0


# ── scale consistency: shrink-only clamp, ANY catalog source ─────────────────

def test_load_clamp_sizes_missing_file_returns_empty(tmp_path):
    from src.unity_entity_asset_resolver import load_clamp_sizes
    assert load_clamp_sizes(str(tmp_path / "nope")) == {}


def test_load_clamp_sizes_drops_null_nonnumeric_and_nonpositive(tmp_path):
    from src.unity_entity_asset_resolver import load_clamp_sizes
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "kind_sizes.json").write_text(
        json.dumps({"sizes": {"vehicle": 4.5},
                    "clamp_sizes": {"vehicle": 13.5, "track": None,
                                    "prop": "bogus", "ship": -1}}),
        encoding="utf-8")
    assert load_clamp_sizes(str(lookups)) == {"vehicle": 13.5}


def test_kenney_pick_gets_negative_clamp_sentinel(env, tmp_path):
    # The Sketchfab-only uniform fit never covers Kenney picks — the clamp
    # (shrink-only ceiling) must, emitted as a NEGATIVE fit_size.
    _kind_sizes_env(env, {"vehicle": 4.5}, clamp_sizes={"vehicle": 13.5})
    all_models = _default_models(tmp_path)
    used = {}
    resolve_entity_assets(
        _schema([EntitySpec(name="PlayerPlane", kind="vehicle", count=1,
                            model_hint="corsair plane")]),
        used, all_models, lookups_dir=str(env))
    picks = [m for k, m in used.items() if k.startswith("asset:")]
    assert picks and picks[0].source == "kenney"
    assert picks[0].fit_size == -13.5


def test_sketchfab_uniform_fit_wins_over_clamp(env, tmp_path):
    # A Sketchfab pick with a mapped kind keeps the exact positive fit target;
    # the clamp only fills the fit_size == 0 gap.
    _kind_sizes_env(env, {"vehicle": 4.5}, clamp_sizes={"vehicle": 13.5})
    _add_sketchfab_entry(env, tmp_path, filename="corsair-sk.fbx",
                         keywords=("corsair", "plane", "aircraft"),
                         kind_hints=("vehicle",), category="vehicle")
    all_models = _default_models(tmp_path)
    used = {}
    resolve_entity_assets(
        _schema([EntitySpec(name="PlayerPlane", kind="vehicle", count=1,
                            model_hint="corsair plane aircraft")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True})
    picks = [m for k, m in used.items() if k.startswith("asset:")]
    assert picks and picks[0].source == "sketchfab"
    assert picks[0].fit_size == 4.5


def test_unclamped_kind_keeps_zero_fit(env, tmp_path):
    _kind_sizes_env(env, {"vehicle": 4.5}, clamp_sizes={"vehicle": 13.5})
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _schema([EntitySpec(name="Trees", kind="tree", count=3,
                            model_hint="pine tree")]),
        used, all_models, lookups_dir=str(env))
    picks = [m for k, m in used.items() if k.startswith("asset:")]
    assert all(m.fit_size == 0.0 for m in picks)


# ── genre-aware resolution (racing must never pick aircraft) ─────────────────

_VEHICLE_LAYERS_RAW = {
    "layers": {
        "vehicle_ground": {
            "vocabulary": ["car", "cars", "racer", "racing", "race", "kart"],
            "kinds": ["vehicle"], "categories": [],
        },
        "aircraft_aerial": {
            "vocabulary": ["plane", "planes", "aircraft", "corsair", "propeller"],
            "kinds": ["vehicle"], "categories": [],
        },
        "vegetation": {
            "vocabulary": ["tree", "trees", "pine", "forest"],
            "kinds": ["tree"], "categories": ["nature"],
        },
    },
}

_GENRE_BIAS_RAW = {
    "racing": {"vehicle_ground": 0.75, "aircraft_aerial": -1.5},
}

_RACE_CAR = {
    "filename": "race-car.fbx", "relpath": "race-car.fbx",
    "category": "vehicle", "keywords": ["car", "race", "racer"],
    "kind_hints": ["vehicle"], "generic_score": 0, "scale": 1.0,
}


def _genre_env(env, extra_assets=()):
    """Add concept layers + genre bias + extra catalog assets to an env dir."""
    (env / "unity" / "concept_layers.json").write_text(
        json.dumps(_VEHICLE_LAYERS_RAW), encoding="utf-8")
    (env / "unity" / "genre_layer_bias.json").write_text(
        json.dumps(_GENRE_BIAS_RAW), encoding="utf-8")
    if extra_assets:
        cat_path = env / "unity" / "asset_catalog.json"
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        lib = Path(cat["assets"][0]["root"])
        for a in extra_assets:
            (lib / a["relpath"]).write_bytes(b"FBX")
            cat["assets"].append({**a, "root": lib.as_posix(),
                                  "game_contexts": ["any"], "format": "fbx",
                                  "pack": "test", "curated": False})
        cat_path.write_text(json.dumps(cat), encoding="utf-8")
    return env


def _racing_schema(entities):
    s = _schema(entities)
    s.meta["genre"] = "racing"
    return s


def test_membership_kind_hint_demoted_by_sibling_vocabulary():
    # The DriftArcade root cause: vehicle_ground / aircraft_aerial both declare
    # kinds:["vehicle"], so an airplane was a STRONG vehicle_ground member on
    # its kind_hint alone and round-robined into car slots. An asset whose own
    # vocabulary places it in a sibling layer now belongs strongly ONLY there.
    from src.unity_concept_layers import ConceptLayers
    layers = ConceptLayers(_VEHICLE_LAYERS_RAW)

    plane = {"filename": "corsair.fbx", "category": "aircraft",
             "keywords": ["plane", "corsair", "propeller"],
             "kind_hints": ["vehicle"]}
    broad, strong = layers.membership(plane)
    assert "aircraft_aerial" in strong
    assert "vehicle_ground" not in strong
    assert "vehicle_ground" not in broad

    car = {"filename": "race-car.fbx", "category": "vehicle",
           "keywords": ["car", "race"], "kind_hints": ["vehicle"]}
    broad, strong = layers.membership(car)
    assert "vehicle_ground" in strong
    assert "aircraft_aerial" not in strong

    # No vocabulary signal anywhere → today's behavior preserved: strong in
    # every layer sharing the kind.
    mystery = {"filename": "widget.fbx", "category": "misc",
               "keywords": ["widget"], "kind_hints": ["vehicle"]}
    _, strong = layers.membership(mystery)
    assert {"vehicle_ground", "aircraft_aerial"} <= strong


def test_racing_genre_never_resolves_vehicles_to_aircraft(env, tmp_path):
    # Generic vehicle entities under genre=racing must all land on the car —
    # without the bias they tie with the plane inside the variety band and the
    # rotation deals planes out (the ring-of-airplanes screenshot).
    _genre_env(env, extra_assets=[_RACE_CAR])
    all_models = _default_models(tmp_path)
    used = {}
    paths = resolve_entity_assets(
        _racing_schema([EntitySpec(name="RaceCars", kind="vehicle", count=6),
                        EntitySpec(name="Opponents", kind="vehicle", count=4),
                        EntitySpec(name="TrafficVehicles", kind="vehicle", count=3)]),
        used, all_models, lookups_dir=str(env))
    for name in ("RaceCars", "Opponents", "TrafficVehicles"):
        assert "corsair" not in paths[name], f"{name} resolved to an airplane"
    assert not any("corsair" in k for k in used)


def test_universal_genre_keeps_vehicle_variety(env, tmp_path):
    # Genre gating: with no genre, an entity whose OWN tokens name both
    # domains ("racer plane") still reaches both car and aircraft picks —
    # the bias must only activate when schema.meta.genre is set.
    _genre_env(env, extra_assets=[_RACE_CAR])
    all_models = _default_models(tmp_path)
    used = {}
    resolve_entity_assets(
        _schema([EntitySpec(name=f"Vehicles{i}", kind="vehicle", count=1,
                            model_hint="racer plane corsair car")
                 for i in range(4)]),
        used, all_models, lookups_dir=str(env))
    picked = {k for k in used if k.startswith("asset:")}
    assert any("corsair" in k for k in picked)
    assert any("race-car" in k for k in picked)


def test_racing_strict_fails_fast_without_racing_content(env, tmp_path):
    _genre_env(env)   # no car in catalog; corsair is the only "vehicle"
    all_models = _default_models(tmp_path)
    with pytest.raises(ValueError, match="racing"):
        resolve_entity_assets(
            _racing_schema([EntitySpec(name="PineTrees", kind="tree", count=5)]),
            {"tree": all_models["tree"]}, all_models,
            lookups_dir=str(env), strict=True)


def test_racing_strict_passes_with_a_vehicle_pick(env, tmp_path):
    _genre_env(env, extra_assets=[_RACE_CAR])
    all_models = _default_models(tmp_path)
    paths = resolve_entity_assets(
        _racing_schema([EntitySpec(name="RaceCars", kind="vehicle", count=6),
                        EntitySpec(name="PineTrees", kind="tree", count=5)]),
        {"tree": all_models["tree"]}, all_models,
        lookups_dir=str(env), strict=True)
    assert "race-car" in paths["RaceCars"]


def test_racing_strict_satisfied_by_default_pack_vehicle(env, tmp_path):
    # A racing game whose vehicle rides the default-pack Car.fbx is coherent —
    # the fail-fast must not fire just because the pick skipped the catalog.
    _genre_env(env)
    vehicle = _vehicle_model(tmp_path)
    all_models = {**_default_models(tmp_path), "vehicle": vehicle}
    paths = resolve_entity_assets(
        _racing_schema([EntitySpec(name="PlayerCar", kind="vehicle", count=1)]),
        {"vehicle": vehicle}, all_models, lookups_dir=str(env), strict=True)
    assert paths["PlayerCar"] == "Models/Default/Vehicle/Car"


def test_genre_prefixes_mechanical_sketchfab_query(env, tmp_path, monkeypatch):
    # The live-fetch fallback query for a racing project must carry the genre
    # ("racing ..."), but an LLM-authored sketchfab_query wins as-is.
    _genre_env(env)
    queries = []

    def _fake_fetch(query, **kw):
        queries.append(query)
        return None

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    all_models = _default_models(tmp_path)
    used = {"prop": all_models["prop"]}
    resolve_entity_assets(
        _racing_schema([EntitySpec(name="Grandstands", kind="prop", count=2,
                                   model_hint="zzgrbl nothing matches")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert queries and queries[0].startswith("racing ")


# ── signage embargo for surface kinds ─────────────────────────────────────────
# Live regression (SpringCreekMotocrossRacing): TrackSurface (kind="road",
# model_hint "road straight racing track") tiled a Sketchfab STOP SIGN as the
# racetrack ribbon. The sign's marketplace tags ("road", "street", "realistic")
# out-hit real road tiles, and fetch_and_freeze had stamped category="road" /
# kind_hints=["road"] onto it, so the kind guard and the +0.5 kind bonus both
# endorsed the match.

_ROAD_TILE = {
    "filename": "road-straight.fbx", "relpath": "road-straight.fbx",
    "category": "road", "keywords": ["road", "straight", "track", "lane",
                                     "segment"],
    "kind_hints": ["road"], "generic_score": 0, "scale": 1.0,
}

# The stop sign's tags exactly as frozen in the live failure (uid a9664def…).
_STOPSIGN_KW = ("3d", "asset", "environment", "game", "realistic", "road",
                "sign", "simulation", "stop", "street", "traffic", "urban")


def _append_assets(env, assets):
    """Add extra Kenney-tier assets (files + catalog entries) to an env dir."""
    cat_path = env / "unity" / "asset_catalog.json"
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    lib = Path(cat["assets"][0]["root"])
    for a in assets:
        (lib / a["relpath"]).write_bytes(b"FBX")
        cat["assets"].append({**a, "root": lib.as_posix(),
                              "game_contexts": ["any"], "format": "fbx",
                              "pack": "test", "curated": False})
    cat_path.write_text(json.dumps(cat), encoding="utf-8")


def _track_surface_entity():
    return EntitySpec(name="TrackSurface", kind="road", count=8,
                      model_hint="road straight racing track",
                      sketchfab_query="realistic road surface material")


def _stopsign_sketchfab_entry(env, tmp_path):
    return _add_sketchfab_entry(env, tmp_path, uid="a9664def",
                                filename="stopsign-a9664def.fbx",
                                keywords=_STOPSIGN_KW, kind_hints=("road",),
                                category="road")


def test_frozen_signage_never_tiles_a_road_entity(env, tmp_path):
    # The exact live failure: a frozen, road-self-certified stop sign in the
    # Sketchfab cache. Tier S must refuse it and fall through to a real tile.
    _append_assets(env, [_ROAD_TILE])
    _stopsign_sketchfab_entry(env, tmp_path)
    paths = resolve_entity_assets(
        _schema([_track_surface_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["TrackSurface"] == "Models/Kenney/Road/road-straight"


def test_auto_fetched_signage_is_rejected_post_fetch(env, tmp_path, monkeypatch):
    # Same class of failure via the live-fetch path: fetch_and_freeze returns
    # a signage model for a road query — the resolver's post-fetch re-score
    # must reject it instead of wiring it in.
    _append_assets(env, [_ROAD_TILE])

    def _fake_fetch(query, **kw):
        return _stopsign_sketchfab_entry(Path(kw["lookups_dir"]), tmp_path)

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    paths = resolve_entity_assets(
        _schema([_track_surface_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert paths["TrackSurface"] == "Models/Kenney/Road/road-straight"


def test_score_zeroes_signage_for_surface_kinds():
    from src.unity_entity_asset_resolver import _query_weights, _score
    entry = {"filename": "stopsign-a9664def.fbx",
             "keywords": list(_STOPSIGN_KW), "kind_hints": ["road"],
             "generic_score": 0}
    qw = _query_weights(_track_surface_entity(), set())
    assert _score(entry, qw, "road") == 0.0


def test_surface_kind_hint_naming_sign_lifts_the_embargo():
    # A road-kind entity that ASKS for a sign ("road sign") may still get one.
    from src.unity_entity_asset_resolver import _query_weights, _score
    e = EntitySpec(name="WarningSigns", kind="road", count=2,
                   model_hint="road sign")
    qw = _query_weights(e, set())
    entry = {"filename": "stopsign-a9664def.fbx",
             "keywords": list(_STOPSIGN_KW), "kind_hints": ["road"],
             "generic_score": 0}
    assert _score(entry, qw, "road") > 0.0


def test_signage_still_matches_non_surface_kinds(env, tmp_path):
    # kind="prop" + hint "stop sign": the sign IS the requested content — the
    # embargo is scoped to surface kinds only.
    _stopsign_sketchfab_entry(env, tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="StopSigns", kind="prop", count=2,
                            model_hint="stop sign")]),
        {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["StopSigns"] == "Models/Sketchfab/Road/stopsign-a9664def"


# ── style adjectives are never match evidence ─────────────────────────────────
# Live regression (SpringCreekMotocrossRacing, regenerated post-PR#62): the
# signage embargo protects SURFACE kinds, but the same frozen stop sign still
# won Barriers, Checkpoints, TrackBarriersOuter/Inner and CheckpointBanners
# (kinds wall/decoration). Every translated sketchfab_query starts with the
# run's style adjective ("realistic …"), the sign is marketplace-tagged
# "realistic", and _score only needs ONE strong hit — so a style word shared
# by every query and half the marketplace qualified the sign for any entity.

_BARRIER_BLOCK = {
    "filename": "barrier-block.fbx", "relpath": "barrier-block.fbx",
    "category": "prop", "keywords": ["barrier", "wall", "block"],
    "kind_hints": ["wall"], "generic_score": 0, "scale": 1.0,
}


def _barriers_entity():
    return EntitySpec(name="Barriers", kind="wall", count=6,
                      model_hint="race barrier",
                      sketchfab_query="realistic race barrier")


def test_style_tokens_are_weak_in_query_weights():
    from src.unity_entity_asset_resolver import _query_weights
    qw = _query_weights(_barriers_entity(), set())
    assert qw["realistic"] == pytest.approx(0.1)   # style adjective → weak
    assert qw["barrier"] == 2.0                     # the semantics stay strong
    e = EntitySpec(name="Cones", kind="obstacle", count=3,
                   sketchfab_query="low poly traffic cone")
    qw = _query_weights(e, set())
    assert qw["low"] == pytest.approx(0.1)
    assert qw["poly"] == pytest.approx(0.1)
    assert qw["cone"] == 2.0


def test_style_only_overlap_scores_zero():
    # The exact live pairing: a wall entity vs the frozen stop sign, whose
    # only shared token is the style adjective "realistic". No strong hit →
    # no match — a Sketchfab entry needs at least one NON-style strong hit.
    from src.unity_entity_asset_resolver import _query_weights, _score
    entry = {"filename": "stopsign-a9664def.fbx",
             "keywords": list(_STOPSIGN_KW), "kind_hints": ["road"],
             "generic_score": 0}
    qw = _query_weights(_barriers_entity(), set())
    assert _score(entry, qw, "wall") == 0.0


def test_style_tokens_still_break_ties_toward_matching_style():
    # Weak, not zero: between two real barrier matches, the one tagged with
    # the query's style adjective must still edge ahead.
    from src.unity_entity_asset_resolver import _query_weights, _score
    qw = _query_weights(_barriers_entity(), set())
    styled = {"filename": "barrier-a.fbx",
              "keywords": ["barrier", "realistic"], "kind_hints": [],
              "generic_score": 0}
    plain = {"filename": "barrier-b.fbx",
             "keywords": ["barrier", "concrete"], "kind_hints": [],
             "generic_score": 0}
    assert _score(styled, qw, "wall") > _score(plain, qw, "wall")


def test_frozen_stopsign_never_wins_a_wall_entity(env, tmp_path):
    # End-to-end mirror of the live failure: the poisoned stop sign sits in
    # the Sketchfab cache, a real barrier sits in the Kenney catalog. Tier S
    # must miss (style-only overlap) and fall through to the barrier.
    _append_assets(env, [_BARRIER_BLOCK])
    _stopsign_sketchfab_entry(env, tmp_path)
    paths = resolve_entity_assets(
        _schema([_barriers_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["Barriers"] == "Models/Kenney/Prop/barrier-block"


def test_frozen_stopsign_never_wins_a_decoration_entity(env, tmp_path):
    # The CheckpointBanners half of the live failure (kind="decoration") —
    # the base fixture's banner.fbx is the correct pick.
    _stopsign_sketchfab_entry(env, tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="CheckpointBanners", kind="decoration",
                            count=4,
                            sketchfab_query="realistic checkpoint banner")]),
        {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["CheckpointBanners"] == "Models/Kenney/Prop/banner"


def test_auto_fetched_style_only_match_is_rejected_post_fetch(env, tmp_path,
                                                              monkeypatch):
    # Live-fetch path: fetch_and_freeze hands back the stop sign for a wall
    # query — the post-fetch re-score must refuse to wire it in.
    _append_assets(env, [_BARRIER_BLOCK])

    def _fake_fetch(query, **kw):
        return _stopsign_sketchfab_entry(Path(kw["lookups_dir"]), tmp_path)

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    paths = resolve_entity_assets(
        _schema([_barriers_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert paths["Barriers"] == "Models/Kenney/Prop/barrier-block"


# ── contextual marketplace tags are never identity ────────────────────────────
# Live regression (SpringCreekMotocrossRacing, regenerated post-PR#63): the
# signage embargo protects surface kinds from SIGNS and the style-token cap
# kills adjective-only matches, but CONTEXTUAL tags still qualified as strong
# hits. TrackSegments (kind="track") tiled a Sketchfab TRAFFIC CONE — the
# cone's tags ("race", "road", "street") describe where a cone lives, not what
# it is, and Tier S wins before the Kenney tiers ever score a real track tile.
# "Debris" (kind="obstacle") got a survivors' TENT via its "debris"/"crash"
# tags. Both entries are honestly stamped (category prop/obstacle) — the fix
# is (1) a positive surface gate (strong road_track/terrain_ground membership
# required for surface kinds) and (2) identity evidence (a strong query token
# in the uploader's own model name, or ≥2 strong tag hits).

_TRACK_TILE = {
    "filename": "track-straight.fbx", "relpath": "track-straight.fbx",
    "category": "track", "keywords": ["track", "straight", "road", "segment"],
    "kind_hints": ["track", "road"], "generic_score": 0, "scale": 1.0,
}

# The cone's tags as frozen in the live failure (uid 48a50313…): category and
# kind_hints honestly say prop — the poison is purely the contextual tag soup.
_CONE_KW = ("barrel", "barrier", "cartoon", "city", "cone", "construction",
            "drive", "element", "exterior", "object", "obstacle", "orange",
            "parking", "race", "road", "street", "traffic", "urban")

# The tent's tags as frozen live (uid 844bef28…): "debris" and "crash" are
# scene context ("built near crashed aircraft debris"), the model is a tent.
_TENT_KW = ("aircraft", "awning", "base", "camp", "canopy", "crash", "debris",
            "environment", "outpost", "survival", "survivors", "tent",
            "zombies")

# Mirrors the shape of lookups/unity/concept_layers.json for the layers this
# guard consults: road_track's strong categories are the real file's, so the
# gate is exercised exactly as in production.
_SURFACE_GUARD_LAYERS = {
    "layers": {
        "road_track": {
            "vocabulary": ["road", "track", "street", "lane", "segment"],
            "kinds": ["road", "track"], "categories": ["track", "rail"]},
        "debris_wreckage": {
            "vocabulary": ["debris", "wreckage", "rubble", "scrap"],
            "kinds": ["obstacle"], "categories": []},
    },
}


def _contextual_env(env):
    _append_assets(env, [_TRACK_TILE])
    (env / "unity" / "concept_layers.json").write_text(
        json.dumps(_SURFACE_GUARD_LAYERS), encoding="utf-8")
    return env


def _track_segments_entity():
    return EntitySpec(name="TrackSegments", kind="track", count=12,
                      model_hint="race track segments",
                      sketchfab_query="realistic race track road segment")


def _cone_sketchfab_entry(env, tmp_path):
    return _add_sketchfab_entry(env, tmp_path, uid="48a50313",
                                filename="traffic-cone-48a50313.fbx",
                                keywords=_CONE_KW, kind_hints=("prop",),
                                category="prop")


def test_contextual_tags_never_tile_a_track_entity(env, tmp_path):
    # The exact live failure: the cone's "race"/"road" tags are strong hits
    # (2 of them — identity evidence alone can't refuse it), but it is not a
    # strong road_track member (category=prop), so the surface gate must
    # block it and let the Kenney tiers pick the real tile.
    _contextual_env(env)
    _cone_sketchfab_entry(env, tmp_path)
    paths = resolve_entity_assets(
        _schema([_track_segments_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["TrackSegments"] == "Models/Kenney/Track/track-straight"


def test_auto_fetched_contextual_cone_is_rejected_post_fetch(env, tmp_path,
                                                             monkeypatch):
    # Live-fetch path: fetch_and_freeze hands back a cone for the track query
    # — the post-fetch acceptance must apply the same surface gate.
    _contextual_env(env)

    def _fake_fetch(query, **kw):
        return _cone_sketchfab_entry(Path(kw["lookups_dir"]), tmp_path)

    monkeypatch.setattr("src.sketchfab_freeze.fetch_and_freeze", _fake_fetch)
    paths = resolve_entity_assets(
        _schema([_track_segments_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": True})
    assert paths["TrackSegments"] == "Models/Kenney/Track/track-straight"


def test_real_sketchfab_track_section_still_passes_surface_gate(env, tmp_path):
    # Positive control: an entry the taxonomy places strongly in road_track
    # (category="track", exactly how a real frozen dirt-track model is
    # stamped) must still win Tier S for the same entity — the gate rejects
    # contextual imposters, not surface content.
    _contextual_env(env)
    entry = _add_sketchfab_entry(env, tmp_path, uid="sk9",
                                 filename="dirt-track-section-sk9.fbx",
                                 keywords=("dirt", "track", "race", "section"),
                                 kind_hints=("track",), category="track")
    paths = resolve_entity_assets(
        _schema([_track_segments_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["TrackSegments"] == "Models/Sketchfab/Track/dirt-track-section-sk9"
    assert entry["filename"].startswith("dirt-track-section")


def _obstacle_model(tmp_path):
    src = tmp_path / "Obstacle.fbx"
    src.write_bytes(b"FBX")
    return ResolvedModel(kind="obstacle", src=str(src),
                         dest_rel="Models/Default/Obstacle/Obstacle.fbx",
                         resource_path="Models/Default/Obstacle/Obstacle",
                         rig="generic")


def test_contextual_tent_never_wins_a_debris_entity(env, tmp_path):
    # The Debris half of the live failure: the tent's kind_hints=["obstacle"]
    # (stamped at freeze time for a DIFFERENT entity) match Debris's kind, so
    # the kind guard endorses it and "debris" is a strong tag hit — but it is
    # ONE contextual tag, and the uploader's own name ("survivors-tent") says
    # nothing about debris. Identity evidence must refuse it.
    _contextual_env(env)
    _add_sketchfab_entry(env, tmp_path, uid="844bef28",
                         filename="survivors-tent-844bef28.fbx",
                         keywords=_TENT_KW, kind_hints=("obstacle",),
                         category="obstacle")
    obstacle = _obstacle_model(tmp_path)
    all_models = {**_default_models(tmp_path), "obstacle": obstacle}
    used = {"obstacle": obstacle}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Debris", kind="obstacle", count=6,
                            model_hint="debris obstacle",
                            sketchfab_query="realistic dirt track debris")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert "survivors-tent" not in paths["Debris"]
    assert paths["Debris"] == "Models/Default/Obstacle/Obstacle"


def test_entity_that_asks_for_a_tent_still_gets_the_tent(env, tmp_path):
    # Positive control: when the entity NAMES the object ("survivor camp
    # tent"), the filename identity check passes and the same frozen entry is
    # a perfectly good pick — the guard is per-query, not a blocklist.
    _contextual_env(env)
    _add_sketchfab_entry(env, tmp_path, uid="844bef28",
                         filename="survivors-tent-844bef28.fbx",
                         keywords=_TENT_KW, kind_hints=("obstacle",),
                         category="obstacle")
    obstacle = _obstacle_model(tmp_path)
    all_models = {**_default_models(tmp_path), "obstacle": obstacle}
    used = {"obstacle": obstacle}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="SurvivorTents", kind="obstacle", count=2,
                            model_hint="survivor camp tent")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["SurvivorTents"] == "Models/Sketchfab/Obstacle/survivors-tent-844bef28"


def test_apex_cones_still_resolve_to_the_cone(env, tmp_path):
    # Positive control for the cone: ApexCones (the entity the cone was
    # actually frozen for, uid e4c3efc3… stamps: category/kind_hints
    # "obstacle") keeps matching — "cone" hits the uploader's model name.
    _contextual_env(env)
    _add_sketchfab_entry(env, tmp_path, uid="e4c3efc3",
                         filename="traffic-cone-e4c3efc3.fbx",
                         keywords=("cone", "traffic", "traffic-cone"),
                         kind_hints=("obstacle",), category="obstacle")
    obstacle = _obstacle_model(tmp_path)
    all_models = {**_default_models(tmp_path), "obstacle": obstacle}
    used = {"obstacle": obstacle}
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="ApexCones", kind="obstacle", count=8,
                            model_hint="traffic cone",
                            sketchfab_query="realistic apex marker cone")]),
        used, all_models, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["ApexCones"] == "Models/Sketchfab/Obstacle/traffic-cone-e4c3efc3"


def test_surface_gate_inactive_without_concept_layers(env, tmp_path):
    # Without concept_layers.json the taxonomy can't judge membership, so the
    # surface gate must stand down — identity evidence is then the only
    # contextual check. The cone still loses here (its 2 strong tag hits pass
    # identity evidence, but the guard's job is done by _passes_kind_guard /
    # scoring elsewhere) — this test pins that the gate itself never turns a
    # missing optional file into a hard embargo.
    _append_assets(env, [_TRACK_TILE])   # note: NO concept_layers.json
    entry = _add_sketchfab_entry(env, tmp_path, uid="sk9",
                                 filename="dirt-track-section-sk9.fbx",
                                 keywords=("dirt", "track", "race", "section"),
                                 kind_hints=("track",), category="track")
    paths = resolve_entity_assets(
        _schema([_track_segments_entity()]), {}, {}, lookups_dir=str(env),
        sketchfab_cfg={"enabled": True, "auto_fetch": False})
    assert paths["TrackSegments"] == "Models/Sketchfab/Track/dirt-track-section-sk9"
    assert entry["filename"].startswith("dirt-track-section")


def test_style_pipeline_query_vocabulary_is_fully_capped():
    # Every query token the style pipeline can prefix onto a sketchfab_query
    # (sketchfab_style._STYLE) must be neutralized by _query_weights — via
    # _STYLE_TOKENS or the stopword list. Keeps the two tables in sync.
    from src.sketchfab_style import _STYLE
    from src.unity_asset_catalog import tokenize
    from src.unity_entity_asset_resolver import _STOPWORDS, _STYLE_TOKENS
    for _sid, _triggers, query_tokens in _STYLE:
        for tok in query_tokens:
            for t in tokenize(tok):
                assert t in _STYLE_TOKENS or t in _STOPWORDS, (
                    f"style query token {t!r} ({tok!r}) can become a strong "
                    "query weight — add it to _STYLE_TOKENS")


# ── structural path roles: road/wall tiles need role evidence ────────────────

STRUCTURAL_ASSETS = [
    # a "road-tagged" sign — the exact live failure: kind_hints claim road,
    # keywords include road, but the filename shows it's a sign
    {"filename": "stopsign.fbx", "relpath": "stopsign.fbx",
     "category": "road", "keywords": ["road", "sign", "stop", "traffic"],
     "kind_hints": ["road"], "generic_score": 5, "scale": 1.0},
    {"filename": "straight.fbx", "relpath": "straight.fbx",
     "category": "track", "keywords": ["straight", "track", "lane", "segment"],
     "kind_hints": ["road"], "generic_score": 0, "scale": 1.0},
    {"filename": "fence.fbx", "relpath": "fence.fbx",
     "category": "structure", "keywords": ["fence", "barrier", "wall"],
     "kind_hints": ["wall"], "generic_score": 0, "scale": 1.0},
]


def _structural_env(tmp_path, assets):
    lib = tmp_path / "slib"
    lib.mkdir()
    for a in assets:
        (lib / a["relpath"]).write_bytes(b"FBX")
    catalog = {
        "version": 1,
        "vocabulary": {"categories": [], "tags": []},
        "assets": [{**a, "root": lib.as_posix(), "game_contexts": ["any"],
                    "format": "fbx", "pack": "test", "curated": False}
                   for a in assets],
    }
    lookups = tmp_path / "slookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8")
    return lookups


def _track_surface():
    return EntitySpec(name="TrackSurface", kind="road", count=8,
                      placement="path", model_hint="road straight racing track",
                      path_points=[[0, 0, 0], [10, 0, 0], [20, 0, 10]],
                      path_width=8.0)


def _barrier_rail():
    return EntitySpec(name="TrackBarriersOuter", kind="wall", count=8,
                      placement="path", model_hint="barrier fence guardrail",
                      path_points=[[0, 0, 5], [10, 0, 5], [20, 0, 15]],
                      path_width=1.0)


def test_road_ribbon_picks_road_evidenced_model_over_sign(tmp_path):
    lookups = _structural_env(tmp_path, STRUCTURAL_ASSETS)
    paths = resolve_entity_assets(_schema([_track_surface()]), {}, {},
                                  lookups_dir=str(lookups))
    assert "straight" in paths["TrackSurface"].lower()
    assert "stopsign" not in paths["TrackSurface"].lower()


def test_barrier_rail_picks_fence_not_sign(tmp_path):
    lookups = _structural_env(tmp_path, STRUCTURAL_ASSETS)
    paths = resolve_entity_assets(_schema([_barrier_rail()]), {}, {},
                                  lookups_dir=str(lookups))
    assert "fence" in paths["TrackBarriersOuter"].lower()


def test_road_ribbon_with_only_sign_available_goes_primitive(tmp_path):
    # No role-evidenced asset in the library: the ribbon must spawn as
    # primitive tiles (no model path at all), never as a chain of stop signs
    # and never as the generic prop fallback.
    lookups = _structural_env(tmp_path, [STRUCTURAL_ASSETS[0]])
    paths = resolve_entity_assets(_schema([_track_surface()]), {}, {},
                                  lookups_dir=str(lookups))
    assert "TrackSurface" not in paths


def test_road_ribbon_never_takes_prop_fallback(tmp_path):
    src = tmp_path / "Prop.fbx"
    src.write_bytes(b"FBX")
    prop = ResolvedModel(kind="prop", src=str(src),
                         dest_rel="Models/Default/Prop/Prop.fbx",
                         resource_path="Models/Default/Prop/Prop", rig="generic")
    lookups = _structural_env(tmp_path, [STRUCTURAL_ASSETS[0]])
    paths = resolve_entity_assets(_schema([_track_surface()]),
                                  {"prop": prop}, {"prop": prop},
                                  lookups_dir=str(lookups))
    assert paths.get("TrackSurface") != "Models/Default/Prop/Prop"
    assert "TrackSurface" not in paths


def test_strict_mode_does_not_fail_on_modelless_ribbon(tmp_path):
    lookups = _structural_env(tmp_path, [STRUCTURAL_ASSETS[0]])
    paths = resolve_entity_assets(_schema([_track_surface()]), {}, {},
                                  lookups_dir=str(lookups), strict=True)
    assert "TrackSurface" not in paths


def test_non_path_road_entity_unaffected_by_structural_guard(tmp_path):
    # A road-kind entity WITHOUT path_points isn't structural geometry —
    # the guard must not reject its candidates.
    lookups = _structural_env(tmp_path, STRUCTURAL_ASSETS)
    e = EntitySpec(name="RoadSigns", kind="road", count=3,
                   model_hint="stop sign traffic road")
    paths = resolve_entity_assets(_schema([e]), {}, {},
                                  lookups_dir=str(lookups))
    assert "stopsign" in paths.get("RoadSigns", "").lower()


# ── distribution sampling (assets.sample_temperature) ─────────────────────────

from src.unity_entity_asset_resolver import _softmax_weights, _stable_seed  # noqa: E402


class TestSoftmaxWeights:
    def test_sums_to_one(self):
        w = _softmax_weights([1.0, 0.6, 0.3], 0.5)
        assert sum(w) == pytest.approx(1.0)

    def test_higher_score_gets_more_mass(self):
        w = _softmax_weights([1.0, 0.6, 0.3], 0.5)
        assert w[0] > w[1] > w[2]

    def test_low_temperature_concentrates_on_the_best(self):
        assert _softmax_weights([1.0, 0.5], 0.01)[0] > 0.99

    def test_high_temperature_flattens_toward_uniform(self):
        w = _softmax_weights([1.0, 0.5], 50.0)
        assert abs(w[0] - w[1]) < 0.02

    def test_stable_for_large_scores(self):
        # max-subtraction must keep exp() from overflowing
        w = _softmax_weights([900.0, 899.0], 0.5)
        assert sum(w) == pytest.approx(1.0)


class TestStableSeed:
    def test_deterministic(self):
        assert _stable_seed(1, 0, ("a.fbx",)) == _stable_seed(1, 0, ("a.fbx",))

    def test_varies_by_seed_draw_and_candidates(self):
        base = _stable_seed(1, 0, ("a.fbx",))
        assert _stable_seed(2, 0, ("a.fbx",)) != base
        assert _stable_seed(1, 1, ("a.fbx",)) != base
        assert _stable_seed(1, 0, ("b.fbx",)) != base

    def test_not_derived_from_salted_builtin_hash(self):
        # A hash()-derived seed would change per interpreter run; this digest
        # is pinned, so a regression to hash() fails here.
        assert _stable_seed(0, 0, ("tree.fbx",)) == _stable_seed(0, 0, ("tree.fbx",))
        assert isinstance(_stable_seed(0, 0, ("tree.fbx",)), int)


@pytest.fixture
def many_trees(tmp_path):
    """A catalog with 20 near-equal tree variants — enough that 'best match'
    and 'sample the distribution' give visibly different answers."""
    lib = tmp_path / "lib"
    lib.mkdir()
    assets = []
    for i in range(20):
        fn = f"tree-{i:02d}.fbx"
        (lib / fn).write_bytes(b"FBX")
        assets.append({"filename": fn, "relpath": fn, "category": "nature",
                       "keywords": ["tree", "forest"], "kind_hints": ["tree"],
                       "generic_score": 20 - i, "scale": 1.0,
                       "root": lib.as_posix(), "game_contexts": ["any"],
                       "format": "fbx", "pack": "test", "curated": False})
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps({"version": 1, "vocabulary": {"categories": [], "tags": []},
                    "assets": assets}), encoding="utf-8")
    return lookups


def _tree_groups(n=8):
    return [EntitySpec(name=f"Trees{i}", kind="tree", count=3,
                       model_hint="tree forest") for i in range(n)]


def _distinct(lookups, cfg):
    paths = resolve_entity_assets(_schema(_tree_groups()), {}, {},
                                  lookups_dir=str(lookups), asset_cfg=cfg)
    return set(paths.values())


class TestDistributionSampling:
    def test_default_is_unchanged_best_match_rotation(self, many_trees):
        # temperature absent → the historical narrow band (variety_top_k=5).
        assert len(_distinct(many_trees, {})) <= 5

    def test_sampling_reaches_beyond_the_variety_band(self, many_trees):
        hot = _distinct(many_trees, {"sample_temperature": 0.6, "sample_seed": 1})
        cold = _distinct(many_trees, {})
        assert len(hot) > len(cold)

    def test_sampling_is_reproducible(self, many_trees):
        cfg = {"sample_temperature": 0.6, "sample_seed": 3}
        assert _distinct(many_trees, cfg) == _distinct(many_trees, cfg)

    def test_different_seed_gives_a_different_cast(self, many_trees):
        a = _distinct(many_trees, {"sample_temperature": 0.6, "sample_seed": 1})
        b = _distinct(many_trees, {"sample_temperature": 0.6, "sample_seed": 99})
        assert a != b

    def test_zero_temperature_matches_disabled(self, many_trees):
        assert _distinct(many_trees, {"sample_temperature": 0.0}) == \
               _distinct(many_trees, {})

    def test_pool_caps_the_distribution(self, many_trees):
        # A pool of 2 can never surface more than 2 distinct models.
        picks = _distinct(many_trees, {"sample_temperature": 5.0,
                                       "sample_pool": 2, "sample_seed": 1})
        assert len(picks) <= 2

    def test_sampling_never_picks_below_min_score(self, many_trees):
        # Candidates are score-filtered before _register; sampling must not
        # resurrect anything the threshold already rejected.
        picks = _distinct(many_trees, {"sample_temperature": 5.0,
                                       "min_match_score": 99.0})
        assert not picks

    def test_uses_no_global_rng(self, many_trees):
        import random as _r
        _r.seed(0)
        first = _r.random()
        _distinct(many_trees, {"sample_temperature": 0.6, "sample_seed": 1})
        _r.seed(0)
        assert _r.random() == first


# ── kind-scoped candidate filtering ───────────────────────────────────────────

# The live ScoreBoard → skateboard regression in miniature
# (tools/out/resolver_tiers.json, run ood_sort). BOTH assets answer the entity's
# "board" token; only one of them declares itself a vehicle. Scores are tuned so
# the wrong one WINS on keyword overlap alone — otherwise the gate would look
# effective when ordinary scoring was already doing the work.
_SCOPE_ASSETS = [
    {"filename": "skateboard.fbx", "relpath": "skateboard.fbx",
     "category": "vehicle",
     "keywords": ["skateboard", "board", "vehicle", "sports", "ride"],
     "kind_hints": ["vehicle"], "generic_score": 4, "scale": 1.0},
    {"filename": "display-board.fbx", "relpath": "display-board.fbx",
     "category": "prop",
     "keywords": ["board", "display", "screen", "panel", "readout"],
     "kind_hints": ["prop"], "generic_score": 0, "scale": 1.0},
]

_SCOPE_LAYERS = {
    "_config": {"kind_prior": 1.0, "layer_band": 0.6},
    "layers": {
        "vehicle_ground": {
            "vocabulary": ["car", "truck", "kart", "racer", "wheel"],
            "kinds": ["vehicle"], "categories": ["vehicle"]},
        "character_humanoid": {
            "vocabulary": ["character", "human", "person"],
            "kinds": ["character", "npc", "player"], "categories": ["character"]},
    },
}


def _write_lookups(tmp_path, assets, layers=None):
    """A lookups dir holding `assets` (files written to disk) and, optionally,
    a concept_layers.json — the two-arg form is the 'taxonomy absent' case."""
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    for a in assets:
        (lib / a["relpath"]).write_bytes(b"FBX")
    catalog = {"version": 1, "vocabulary": {"categories": [], "tags": []},
               "assets": [{**a, "root": lib.as_posix(), "game_contexts": ["any"],
                           "format": "fbx", "pack": "t", "curated": False}
                          for a in assets]}
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True, exist_ok=True)
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8")
    if layers is not None:
        (lookups / "unity" / "concept_layers.json").write_text(
            json.dumps(layers), encoding="utf-8")
    return lookups


def _scoreboard():
    return EntitySpec(name="ScoreBoard", kind="ui", count=1,
                      model_hint="score board ui")


def test_ui_entity_never_takes_a_vehicle_asset(tmp_path):
    """The live regression: "ScoreBoard" shares exactly ONE keyword with
    skateboard.fbx ("board") and that was enough to win Tier 2b outright — no
    concept layer classifies a "ui" entity, and _passes_kind_guard only rejects
    a match whose sole strong token is the bare kind word. The asset's own
    category/kind_hints say "vehicle", which is the signal the gate reads."""
    lookups = _write_lookups(tmp_path, _SCOPE_ASSETS, _SCOPE_LAYERS)
    paths = resolve_entity_assets(_schema([_scoreboard()]), {}, {},
                                  lookups_dir=str(lookups), strict=False)
    assert paths["ScoreBoard"] == "Models/Kenney/Prop/display-board"


def test_kind_scope_is_inert_without_concept_layers(tmp_path):
    """Same catalog, same entity, no concept_layers.json: the resolver reverts
    to global literal matching and skateboard.fbx wins on score again. Proves
    the gate is a pure enhancement on top of the optional taxonomy — and that
    the fixture really does reproduce the bug rather than testing a case
    ordinary scoring already got right."""
    lookups = _write_lookups(tmp_path, _SCOPE_ASSETS)
    paths = resolve_entity_assets(_schema([_scoreboard()]), {}, {},
                                  lookups_dir=str(lookups), strict=False)
    assert paths["ScoreBoard"] == "Models/Kenney/Vehicle/skateboard"


def test_entity_that_names_the_skateboard_still_gets_one(tmp_path):
    """Identity evidence — a strong query token in the asset's OWN filename —
    is the escape hatch, the same rule _sk_contextual_guard applies to
    marketplace tag soup. "board" is not "skateboard", but an entity that
    actually says skateboard reaches straight through the gate."""
    lookups = _write_lookups(tmp_path, _SCOPE_ASSETS, _SCOPE_LAYERS)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Skateboards", kind="prop", count=1,
                            model_hint="skateboard")]),
        {}, {}, lookups_dir=str(lookups), strict=False)
    assert paths["Skateboards"] == "Models/Kenney/Vehicle/skateboard"


def test_in_domain_entity_takes_the_vehicle_without_naming_it(tmp_path):
    """Scope, not filename evidence, is the primary warrant: a kind="vehicle"
    entity whose tokens reach vehicle_ground gets the vehicle asset even though
    it never says "skateboard"."""
    lookups = _write_lookups(tmp_path, _SCOPE_ASSETS, _SCOPE_LAYERS)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="RacerRides", kind="vehicle", count=1)]),
        {}, {}, lookups_dir=str(lookups), strict=False)
    assert paths["RacerRides"] == "Models/Kenney/Vehicle/skateboard"


def test_out_of_domain_asset_cannot_return_via_the_raw_fallback(tmp_path):
    """Tier 2b relaxes _passes_kind_guard when guarding would empty the field
    and no tier-1 default exists to fall back on — which is exactly the branch
    a "ui" entity takes. The scope gate therefore filters `candidates` itself,
    ahead of that split, so the relaxation can never hand back an out-of-domain
    asset. The entity ends with no 3D spawn (kind "ui" is owned by SceneSetup),
    which is the honest answer."""
    assets = [
        # Only strong hit IS the bare kind word "ui", and "ui" is not in its
        # filename, so _passes_kind_guard rejects it and `guarded` empties.
        {"filename": "hud-panel.fbx", "relpath": "hud-panel.fbx",
         "category": "vehicle", "keywords": ["ui"],
         "kind_hints": ["prop"], "generic_score": 0, "scale": 1.0},
    ]
    lookups = _write_lookups(tmp_path, assets, _SCOPE_LAYERS)
    paths = resolve_entity_assets(_schema([_scoreboard()]), {}, {},
                                  lookups_dir=str(lookups), strict=True)
    assert paths["ScoreBoard"] == "skip:ui"


def test_kind_scope_applies_to_sketchfab_tier_s(tmp_path):
    """Tier S is not exempt. The frozen entry below clears _passes_kind_guard
    AND _sk_contextual_guard (two strong tokens hit its tags, so it reads as
    identity rather than context) and Tier S wins outright over Kenney when it
    hits — only the domain scope stops it, after which Tier S falls through to
    the Kenney tiers exactly as a cache miss does.

    kind="decoration" rather than the "ui" of the sibling tests: "ui" is in
    _SKIP_KINDS, which gates Tier S off entirely, so a ui entity could never
    exercise this path.
    """
    lookups = _write_lookups(tmp_path, _SCOPE_ASSETS, _SCOPE_LAYERS)
    _add_sketchfab_entry(lookups, tmp_path, uid="sk-deck",
                         filename="pro-deck-sk1.fbx",
                         keywords=("board", "score", "deck", "ride"),
                         kind_hints=("vehicle",), category="vehicle")
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="ScoreBoards", kind="decoration", count=1,
                            model_hint="score board")]),
        {}, {}, lookups_dir=str(lookups),
        sketchfab_cfg={"enabled": True, "auto_fetch": False}, strict=False)
    assert paths["ScoreBoards"] == "Models/Kenney/Prop/display-board"


# ── neutral-humanoid policy for unmatched characters ─────────────────────────
#
# The LLM reaches for kind="character" (also "human"/"person"), which the asset
# library never uses as a kind_hint — it tags people npc/player/crowd/enemy.
# Live failure: a "balance a tray of drinks while walking across a room" prompt
# produced a Waiters entity that resolved to character-skate-boy.fbx, matching
# on the bare word "character" and nothing else. Nearest-match is the wrong
# policy for people: a grey capsule reads as "placeholder, replace me", while a
# skate boy reads as a decision the pipeline made about who works at the
# restaurant. Confidently wrong is worse than obviously unfinished.

_PEOPLE_ASSETS = [
    # Both entries are verbatim shapes from lookups/unity/asset_catalog.json.
    # Note kind_hints: neither says "character" — that is the whole problem.
    {"filename": "character-skate-boy.fbx", "relpath": "character-skate-boy.fbx",
     "category": "character",
     "keywords": ["character", "player", "human", "person", "skater", "boy"],
     "kind_hints": ["npc", "crowd"], "generic_score": 4, "scale": 1.0},
    {"filename": "astronautA.fbx", "relpath": "astronautA.fbx",
     "category": "character",
     "keywords": ["astronaut", "spaceman", "person", "npc", "character"],
     "kind_hints": ["player", "npc"], "generic_score": 0, "scale": 1.0},
]

_PEOPLE_LAYERS = {
    "_config": {"kind_prior": 1.0, "layer_band": 0.6},
    "layers": {
        "character_humanoid": {
            "vocabulary": ["character", "human", "person", "astronaut"],
            "kinds": ["character", "npc", "player", "crowd"],
            "categories": ["character"]},
    },
}


def _neutral_humanoid(tmp_path):
    """The anonymous Spectator that lookups/unity/default_models.json maps
    character / human / person to."""
    src = tmp_path / "Spectator.fbx"
    src.write_bytes(b"FBX")
    return ResolvedModel(kind="character", src=str(src),
                         dest_rel="Models/Default/Character/Spectator.fbx",
                         resource_path="Models/Default/Character/Spectator",
                         rig="generic")


def _people_env(tmp_path, name, layers):
    root = tmp_path / name
    root.mkdir()
    return _write_lookups(root, _PEOPLE_ASSETS, layers)


def test_unmatched_character_degrades_to_neutral_humanoid(tmp_path):
    """The live failure, through Tier 2a (concept-layer match). skate-boy is a
    STRONG character_humanoid member and collects the layer-alignment bonus, so
    scoring alone hands it the slot — nothing in the library is a waiter. The
    kind guard, which Tier 2a did not run at all, has to refuse it."""
    lookups = _people_env(tmp_path, "a", _PEOPLE_LAYERS)
    spectator = _neutral_humanoid(tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Waiters", kind="character", count=3,
                            model_hint="waiter")]),
        {"character": spectator}, {"character": spectator},
        lookups_dir=str(lookups))
    assert "skate" not in paths["Waiters"]
    assert paths["Waiters"] == "Models/Default/Character/Spectator"


def test_unmatched_character_degrades_without_concept_layers(tmp_path):
    """Same entity with no concept_layers.json, so the pick comes from Tier 2b
    instead. Guard-rail rather than a reproduction — Tier 2b's guard already
    refused skate-boy whenever a default existed, which is exactly why the bug
    only ever surfaced through Tier 2a. Pinned so the two tiers cannot drift
    apart again."""
    lookups = _people_env(tmp_path, "b", None)
    spectator = _neutral_humanoid(tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Waiters", kind="character", count=3,
                            model_hint="waiter")]),
        {"character": spectator}, {"character": spectator},
        lookups_dir=str(lookups))
    assert paths["Waiters"] == "Models/Default/Character/Spectator"


def test_generic_person_words_are_not_a_specific_request(tmp_path):
    """"NPCs"/model_hint "npc character" names a CATEGORY, not a person, so it
    carries no more information than the kind field already did (see
    _GENERIC_PERSON_TOKENS). A bare role word must not license the library's
    nearest themed character — this is the entity the LLM invents when it
    embellishes a brief, and the neutral default is the honest answer."""
    lookups = _people_env(tmp_path, "c", _PEOPLE_LAYERS)
    spectator = _neutral_humanoid(tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="NPCs", kind="character", count=4,
                            model_hint="npc character")]),
        {"character": spectator}, {"character": spectator},
        lookups_dir=str(lookups))
    assert paths["NPCs"] == "Models/Default/Character/Spectator"


def test_named_character_still_gets_its_model(tmp_path):
    """Positive control — the policy is "degrade when unmatched", not "never
    match". "Astronauts" names something the library actually has, and
    "astronaut" appears in the asset's own filename, so it displaces the
    neutral default. Without this escape the character default would block
    every catalog pick for the kind, since nothing is tagged kind_hint
    "character"."""
    lookups = _people_env(tmp_path, "d", _PEOPLE_LAYERS)
    spectator = _neutral_humanoid(tmp_path)
    paths = resolve_entity_assets(
        _schema([EntitySpec(name="Astronauts", kind="character", count=2,
                            model_hint="astronaut")]),
        {"character": spectator}, {"character": spectator},
        lookups_dir=str(lookups))
    assert paths["Astronauts"] == "Models/Kenney/Character/astronautA"


def test_person_kinds_all_map_to_one_neutral_humanoid():
    """The data half of the policy: default_models.json must give the generic
    person kinds a real default, otherwise tier1 is None and every weak-match
    protection that keys off "a known-good default exists" is disabled."""
    kinds = json.loads(
        (Path("lookups") / "unity" / "default_models.json").read_text(
            encoding="utf-8"))["kinds"]
    people = [kinds[k]["model"] for k in ("character", "human", "person", "npc")]
    assert people == [people[0]] * len(people)
    assert "Spectator" in people[0]
