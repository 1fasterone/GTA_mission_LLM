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
