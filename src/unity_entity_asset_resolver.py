"""
Per-entity asset resolution — every entity group gets a real model from the
full asset catalog, not just the 20-kind default pack.

Tiering per entity (first hit wins):
  1. explicit kind  — e.kind resolved by the default pack / Blender overrides.
                      The schema default kind "prop" is NOT explicit (it would
                      starve the catalog tier — the root of the everything-is-
                      Prop.fbx symptom); `model_hint: "default prop"` is the
                      escape hatch for a deliberate default prop.
  2. catalog match  — keyword-overlap scoring of entity name + kind +
                      model_hint (+ low-weight scene description) against
                      lookups/unity/asset_catalog.json, with deterministic
                      variety: same-kind groups round-robin the top matches.
                      Every tier's candidate pool is KIND-SCOPED first (see
                      _passes_kind_scope): an asset whose own category /
                      kind_hints place it in a concept domain can only fill an
                      entity scoped to that domain, so a lone shared keyword
                      can no longer reach across the library
                      ("ScoreBoard" → skateboard.fbx via "board").
  3. fallback       — strict mode hard-fails with the query tokens and near
                      misses (FAIL-FAST doctrine); non-strict keeps the old
                      prop fallback + warning.

Deterministic: pure lookup + arithmetic, stable tie-breaks, no RNG, no LLM.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .unity_asset_catalog import load_catalog, tokenize
from .unity_concept_layers import load_concept_layers, load_genre_layer_bias
# One vocabulary decides what a group IS on a board, so the pass that LAYS IT
# OUT as a panel and the pass that BUILDS it agree. See the UISurface roles
# section of src/unity_layout_archetype.py.
from .unity_layout_archetype import ui_element_of
from .unity_model_resolver import ResolvedModel
from .unity_scenebind import authored_models
from .unity_schema import is_embodied

_log = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def load_kind_sizes(lookups_dir: str = "lookups") -> dict[str, float]:
    """lookups/unity/kind_sizes.json: entity kind -> target longest-axis size
    (meters), used to bounds-fit Sketchfab-sourced models at runtime (a
    Sketchfab car authored at 632 meters — a real, live-verified upload unit
    mistake — otherwise dwarfs everything else in the scene). Kenney/default-
    pack picks never consult this; their pack-native scale is already mutually
    consistent. Missing/malformed file, or a kind mapped to null, -> no entry
    -> no fit. Deterministic: pure lookup.
    """
    path = Path(lookups_dir) / "unity" / "kind_sizes.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {k: float(v) for k, v in (raw.get("sizes") or {}).items()
            if isinstance(v, (int, float))}


def load_clamp_sizes(lookups_dir: str = "lookups") -> dict[str, float]:
    """lookups/unity/kind_sizes.json `clamp_sizes`: entity kind -> shrink-only
    MAXIMUM longest-axis size (meters), applied to catalog picks of ANY source
    (Kenney included — the uniform `sizes` fit is Sketchfab-only, so a
    grossly-oversized Kenney/catalog vehicle had no guard at all). Emitted as
    a NEGATIVE fit_size sentinel; the spawner computes
    min(1, clamp/measured), so a well-scaled asset is never touched and
    nothing is ever enlarged. Missing/malformed file or table -> {} -> no
    clamp. Deterministic: pure lookup."""
    path = Path(lookups_dir) / "unity" / "kind_sizes.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {k: float(v) for k, v in (raw.get("clamp_sizes") or {}).items()
            if isinstance(v, (int, float)) and v > 0}

# Resources.Load roots for catalog picks. Distinct from the default pack's
# "Models/Default/" so the two never collide; 3 path parts keeps write()'s
# secondary-copy prefix strip (Assets/<root>/<Category>/<file>) working.
_RES_ROOT = "Models/Kenney"
_SPRITE_RES_ROOT = "Sprites/Kenney"
_SKETCHFAB_RES_ROOT = "Models/Sketchfab"
# Tier A — models the author placed by hand in the Scene Compiler
# (src/unity_sceneinput.py). Their own subtree so the texture copy walks the
# right ancestor chain (see texture_dirs_with_res_root_for).
_AUTHORED_RES_ROOT = "Models/SceneCompiler"
_AUTHORED_SPRITE_RES_ROOT = "Sprites/SceneCompiler"
# ResolvedModel.source -> the Resources-relative root it is copied under.
_RES_ROOT_BY_SOURCE = {
    "sketchfab": _SKETCHFAB_RES_ROOT,
    "scene_compiler": _AUTHORED_RES_ROOT,
}

# 2D games prefer sprites over 3D models when both match an entity — a fish
# PNG beats a marble-track FBX in a side-scroller. Applied only to entries
# that already cleared the strong-hit gate.
_SPRITE_2D_BONUS = 0.75

_DEFAULT_MIN_SCORE = 0.15
_DEFAULT_TOP_K = 5
_VARIETY_BAND = 0.85          # candidates within 85% of the best score rotate

# Distribution-sampling mode (opt-in via assets.sample_temperature > 0). Instead
# of "pick the best (then round-robin a narrow band)", score all candidates,
# turn the top _SAMPLE_POOL scores into a softmax probability distribution, and
# SAMPLE from it — so the same catalog can surface hundreds of distinct models
# across a scene instead of the same ~20. Kept reproducible by a SEEDED RNG
# (assets.sample_seed), honoring the compiler's determinism contract: same
# schema + same seed → byte-identical output, just drawn from the whole field.
# temperature → 0 concentrates on the top score (today's best-match behavior);
# larger temperature flattens toward uniform (more variety). The pool cap is the
# top-k analog: without it, the long tail of thousands of marginal matches would
# collectively outweigh the few good ones and pull a race car toward a fern.
_DEFAULT_SAMPLE_POOL = 40

# Bonus added to a candidate that is a STRONG member of the entity's concept
# layer (its category / kind_hint places it there, not a stray keyword). Large
# enough to lift an on-concept asset over an off-concept one that only edges
# ahead on generic_score — the exact flip behind "StratoPlatform → steps.fbx",
# where a staircase (higher generic_score, category=stairs) out-pointed a real
# platform (category=structure) purely on commonness.
_LAYER_ALIGN_BONUS = 1.0

# FAIL-FAST genre coherence (racing): a strict-mode racing project whose
# entities resolve to zero assets from these core layers — no drivable ground
# vehicle, no track/road piece — is not a racing game, it's a themed prop
# scatter. Better to hard-fail with an actionable message than ship it.
_RACING_CORE_LAYERS = frozenset({"vehicle_ground", "road_track"})

# Structural path roles: entities that ARE the track geometry (a tiled road
# ribbon, a barrier rail along it). A wrong model here doesn't just look
# off-theme — it becomes the road (hit live: TrackSurface AND both barrier
# rails all resolved to a Sketchfab stop sign whose frozen entry carried
# kind_hints=["road"], so the kind guard waved it through every tier and the
# whole circuit rendered as a chain of stretched stop signs). For these roles
# a candidate must show ROLE EVIDENCE IN ITS FILENAME — the same
# identity-evidence rule the contextual-tag guard uses — not merely a kind
# tag or a coincidental keyword. No evidence -> no model -> the spawner's
# primitive tile ribbon, which at least reads as a road.
_STRUCTURAL_ROLE_TOKENS: dict[str, frozenset] = {
    "road":  frozenset({"road", "street", "track", "path", "lane", "straight",
                        "bend", "curve", "corner", "asphalt", "tarmac",
                        "highway", "circuit", "raceway", "driveway"}),
    "track": frozenset({"road", "street", "track", "path", "lane", "straight",
                        "bend", "curve", "corner", "asphalt", "tarmac",
                        "highway", "circuit", "raceway", "driveway"}),
    "wall":  frozenset({"wall", "fence", "barrier", "guard", "rail", "railing",
                        "guardrail", "barricade", "hedge", "divider"}),
    "barrier": frozenset({"wall", "fence", "barrier", "guard", "rail",
                          "railing", "guardrail", "barricade", "hedge",
                          "divider"}),
}

# Generic query words that appear in almost any scene description — they carry
# no signal about which MODEL fits, so they never count as keyword hits.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "with", "for", "to",
    "game", "player", "level", "scene", "world", "entity", "entities",
    "group", "object", "objects", "item", "items", "prop", "props",
})

# Kinds whose entities are drivable/walkable SURFACE tiles laid along a path
# or floor. Signage is categorically wrong content for these — see
# _signage_blocked.
_SURFACE_KINDS = frozenset({"road", "track", "street", "path", "ground",
                            "floor", "terrain"})

# Concept layers whose STRONG members (category / kind_hints place them there)
# are legitimate surface content — the positive counterpart of the signage
# embargo's negative list. A Sketchfab entry may only fill a _SURFACE_KINDS
# entity when it belongs strongly to one of these; contextual keywords grant
# broad membership at most. Hit live (SpringCreekMotocrossRacing, regenerated
# post-PR#63): TrackSegments (kind="track") tiled a TRAFFIC CONE whose
# marketplace tags ("race", "road", "street") describe where a cone LIVES, not
# what it is — and Tier S wins outright before the Kenney tiers ever score a
# real road tile. The cone is honestly stamped category="prop", so this gate
# (unlike the signage embargo) needs no per-token list: not being surface
# content is already in its stamps. See _sk_contextual_guard in
# resolve_entity_assets.
_SURFACE_LAYERS = frozenset({"road_track", "terrain_ground"})

# Tokens that mark an asset as signage / roadside furniture rather than a
# surface piece. Deliberately an explicit list, not suffix matching —
# "design" also ends in "sign".
_SIGNAGE_TOKENS = frozenset({
    "sign", "signs", "signage", "signpost", "signposts", "signboard",
    "stopsign", "stopsigns", "roadsign", "roadsigns", "streetsign",
    "streetsigns", "trafficsign", "trafficsigns", "billboard", "billboards",
})

# Style adjectives — how a model LOOKS, never what it IS. The concept
# translator / style session prefixes nearly every sketchfab_query with the
# run's visual style ("realistic …", "low poly …"), and marketplace uploaders
# tag the same words religiously, so a style word is guaranteed to co-occur on
# both sides of almost any query/asset pair. Hit live
# (SpringCreekMotocrossRacing): a frozen stop sign tagged "realistic" became
# the winning Tier S match for EVERY entity whose translated query started
# with "realistic" — Barriers, Checkpoints, TrackBarriersOuter/Inner,
# CheckpointBanners (kinds wall/decoration) all resolved to it, because
# "realistic" was their only strong hit and _score only requires one.
#
# _query_weights caps these to scene-description weight (0.1): they still
# tie-break toward style-matching assets (a "realistic"-tagged model beats an
# otherwise-equal untagged one), but can never QUALIFY a match on their own —
# which also guarantees a Sketchfab entry can only win Tier S on at least one
# non-style strong hit. Superset of the tokenized query vocabulary in
# src/sketchfab_style.py's _STYLE table (kept in sync by test) plus common
# marketplace synonyms.
_STYLE_TOKENS = frozenset({
    # sketchfab_style._STYLE query tokens, tokenized ("low poly" → low, poly)
    "low", "poly", "lowpoly", "cartoon", "stylized", "toon", "handpainted",
    "realistic", "pbr", "ready", "voxel", "psx", "retro",
    # marketplace synonyms for the same styles
    "stylised", "photorealistic", "photoreal", "cartoony", "anime",
    "painterly", "high", "highpoly", "detailed", "textured", "minimalist",
    "faceted", "blocky",
})


def _slug(name: str) -> str:
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", name or "").split()
    return "".join(p.capitalize() for p in parts) or "Entity"


def _stable_seed(base_seed: int, draw: int, filenames: tuple[str, ...]) -> int:
    """Deterministic 64-bit RNG seed from the run seed, the per-candidate-set
    draw index, and the candidate identities.

    Uses hashlib, NOT the builtin hash(): Python salts str hashing per process
    (PYTHONHASHSEED), so a hash()-derived seed would pick different models on
    every interpreter start — silently breaking the reproducible-build contract
    this module exists to uphold. sha1 here is a stable digest, not a security
    primitive.
    """
    payload = f"{base_seed}|{draw}|{'|'.join(filenames)}".encode("utf-8")
    return int.from_bytes(hashlib.sha1(payload).digest()[:8], "big")


def _softmax_weights(scores: list[float], temperature: float) -> list[float]:
    """Normalize match scores into a probability distribution:
    p_i ∝ exp((s_i - max) / T).

    Subtracting the max keeps exp() numerically stable for any score range.
    T → 0 concentrates all mass on the best match (the historical
    "pick the best" behavior); larger T flattens toward uniform, so lower-
    scored-but-still-valid models get real probability mass and the catalog's
    long tail actually gets used.
    """
    top = max(scores)
    exps = [math.exp((s - top) / temperature) for s in scores]
    total = sum(exps) or 1.0
    return [e / total for e in exps]


def _entry_source(entry: dict, asset_root: Optional[str]) -> Optional[Path]:
    """Absolute path of a catalog entry's model file, or None if missing."""
    root = Path(entry.get("root") or asset_root or "Models/FBX format")
    src = root / entry.get("relpath", entry.get("filename", ""))
    return src if src.is_file() else None


# Query tokens at or above this weight are "strong" — they came from the
# entity's own name/kind/model_hint, not the scene description.
_STRONG_HIT = 1.0

# Kinds that describe engine-native content, not FBX models. They still get a
# catalog / procedural-mesh shot (a 2D "title background" IS a real sprite),
# but a miss routes to the matching content path instead of a strict hard-fail:
# fx-family → runtime ParticleSystem in EntitySpawner; ui/background-family →
# owned by SceneSetup / the sky pipeline, so the spawner skips the group.
_FX_KINDS = frozenset({"fx", "vfx", "effect", "effects", "particles", "weather"})
_SKIP_KINDS = frozenset({"ui", "hud", "menu", "background", "backdrop",
                         "skybox", "sky"})

# Role words that name a CATEGORY of person rather than a person. A strong hit
# on one of these says no more than the entity's own `kind` field already did:
# an entity called "NPCs" tagged npc, "Characters" tagged character. Folded into
# the kind variants for _PERSON_KINDS entities so the weak-match rules treat the
# whole family consistently — otherwise "NPCs" (kind="character") counts "npc"
# as a specific request and any npc-tagged alien satisfies it.
_GENERIC_PERSON_TOKENS = frozenset({
    "character", "characters", "human", "humans", "person", "people",
    "npc", "npcs", "crowd", "crowds", "player", "players",
    "avatar", "avatars", "figure", "figures",
})
_PERSON_KINDS = frozenset({"character", "characters", "human", "person",
                           "people", "npc", "crowd", "civilian"})


def _entry_tokens(entry: dict) -> set[str]:
    """Keyword tags + filename-stem tokens — the searchable identity of a
    catalog entry, exactly what _score matches query tokens against."""
    kw = set(k.lower() for k in entry.get("keywords", []))
    kw.update(t for t in tokenize(Path(entry.get("filename", "")).stem)
              if len(t) > 1)   # "fence-1x2" leaves a bare "x" — never a keyword
    return kw


def _signage_blocked(entry_tokens: set[str], query_weights: dict[str, float],
                     kind: str) -> bool:
    """True when a signage asset must not fill a surface-kind entity.

    Hit live: TrackSurface (kind="road", model_hint "road straight racing
    track") tiled a Sketchfab STOP SIGN as the racetrack ribbon. The sign's
    marketplace tags ("road", "street", "realistic") out-hit real road tiles
    on keyword overlap, and fetch-and-freeze had stamped kind_hints=["road"]
    on it, so both the kind-hint bonus and _passes_kind_guard endorsed the
    match. No score can make a sign drivable — block signage for surface
    kinds outright, unless the entity itself asks for a sign (a strong query
    token in _SIGNAGE_TOKENS, e.g. model_hint "road sign").
    """
    if kind not in _SURFACE_KINDS:
        return False
    if not (entry_tokens & _SIGNAGE_TOKENS):
        return False
    return not any(w >= _STRONG_HIT and t in _SIGNAGE_TOKENS
                   for t, w in query_weights.items())


def _score(entry: dict, query_weights: dict[str, float], kind: str) -> float:
    """Weighted keyword overlap + kind-hint bonus + generic tiebreak.

    Returns 0.0 unless a STRONG token (entity name / kind / model_hint, weight
    ≥ 1.0) matches. Scene-description words (weight 0.1) are tiebreakers among
    strong matches only — hit live: "AtmosphericClouds" grabbed steps.fbx
    because the description word "vertical" (0.1) qualified the asset and then
    the +0.5 prop kind bonus carried it over the threshold.
    """
    kw = _entry_tokens(entry)
    if _signage_blocked(kw, query_weights, kind):
        return 0.0
    hits = {t: w for t, w in query_weights.items() if t in kw}
    if not hits or max(hits.values()) < _STRONG_HIT:
        return 0.0
    score = sum(hits.values()) / max(1.0, len(kw)) ** 0.5
    if kind in entry.get("kind_hints", []):
        score += 0.5
    return score + entry.get("generic_score", 0) / 50.0


def _query_weights(entity, scene_words: set[str]) -> dict[str, float]:
    """Token → weight for one entity. model_hint and the concept-translated
    sketchfab_query count double, the scene description barely at all (it only
    breaks ties toward on-theme assets)."""
    weights: dict[str, float] = {}
    for w in scene_words:
        weights[w] = 0.1
    for t in tokenize(entity.name):
        if t not in _STOPWORDS and len(t) > 1:
            weights[t] = 1.0
    kind = (entity.kind or "").lower()
    if kind and kind not in _STOPWORDS and len(kind) > 1:
        weights[kind] = max(weights.get(kind, 0.0), 1.0)
    for t in tokenize(getattr(entity, "model_hint", "") or ""):
        if t not in _STOPWORDS and len(t) > 1:
            weights[t] = 2.0
    # Concept-translated query ("cartoon race car") — the SAME concept awareness
    # the Sketchfab tier gets also steers the LOCAL catalog tiers (2a/2b/2c), so
    # a Sketchfab miss falls back to an on-concept local model instead of
    # whatever literal entity-name token happened to match (the "PlayerCar →
    # character-skate-boy" failure mode).
    for t in tokenize(getattr(entity, "sketchfab_query", "") or ""):
        if t not in _STOPWORDS and len(t) > 1:
            weights[t] = max(weights.get(t, 0.0), 2.0)
    # Style adjectives never qualify a match — cap to scene-description
    # weight regardless of where the token came from, and do it BEFORE
    # plural bridging so a style word can't smuggle in a strong variant.
    # See _STYLE_TOKENS for the live failure this prevents.
    for t in _STYLE_TOKENS:
        if t in weights:
            weights[t] = min(weights[t], 0.1)
    # Singular/plural bridging for strong tokens: entity names are usually
    # plural ("PineTrees") while catalog keywords are singular ("tree").
    for tok, weight in list(weights.items()):
        if weight >= _STRONG_HIT:
            if tok.endswith("s") and len(tok) > 3:
                weights.setdefault(tok[:-1], weight)
            else:
                weights.setdefault(tok + "s", weight)
    return weights


def _tokens_with_plurals(text: str) -> set[str]:
    toks = {t for t in tokenize(text) if t not in _STOPWORDS}
    for t in list(toks):
        if t.endswith("s") and len(t) > 3:
            toks.add(t[:-1])
        else:
            toks.add(t + "s")
    return toks


def _procedural_route(entity, procedural_meshes) -> Optional[str]:
    """Match an unresolved entity to a schema procedural mesh by token overlap.

    The LLM often authors BOTH an entity group and a procedural mesh for the
    same concept ("AtmosphericClouds" entity + "CloudLayer1" mesh) — when no
    real model exists, the mesh IS the content, so route the group to the
    MeshBuilder path instead of failing. Returns a "procedural:<type>:<res>"
    path the spawner understands, or None.
    """
    ent_tokens = _tokens_with_plurals(entity.name) \
        | _tokens_with_plurals(getattr(entity, "model_hint", "") or "")
    best: Optional[tuple[int, str, str]] = None   # (-overlap, name, path)
    for m in procedural_meshes or []:
        mesh_tokens = _tokens_with_plurals(m.name) \
            | _tokens_with_plurals(getattr(m, "description", "") or "")
        overlap = len(ent_tokens & mesh_tokens)
        if overlap == 0:
            continue
        key = (-overlap, m.name)
        if best is None or key < best[:2]:
            best = (*key, f"procedural:{m.mesh_type}:{m.resolution}")
    return best[2] if best else None


def _nearest_misses(catalog_assets: list[dict],
                    query_weights: dict[str, float], kind: str,
                    n: int = 3) -> list[str]:
    """Best-scoring filenames even below threshold — for actionable errors."""
    scored = sorted(
        ((_score(a, query_weights, kind), a.get("filename", "?"))
         for a in catalog_assets),
        key=lambda sf: (-sf[0], sf[1]))
    return [f for s, f in scored[:n] if s > 0]


def _register_authored(entry: dict,
                       used_models: dict[str, ResolvedModel],
                       is_2d: bool) -> Optional[str]:
    """Register a Scene-Compiler-authored model in `used_models` and return its
    Resources.Load path (None when its source file is gone).

    Deliberately NOT routed through `_register`: an authored pick has no
    catalog entry to score, no variety rotation to take part in, and — most
    importantly — no size correction. `fit_size` stays 0.0 so neither the
    Sketchfab uniform fit nor the shrink-only clamp can touch it, and
    ResolvedModel.scale stays 1.0 so the FBX importer keeps the file's own
    units; the author's layout scale rides on EntitySpec.scale instead (baked
    by src/unity_scenebind.py), which is the transform the spawner applies.
    """
    src = Path(str(entry.get("src") or ""))
    if not src.is_file():
        return None
    is_sprite = entry.get("type") == "sprite"
    if is_sprite and not is_2d:
        return None
    res_root = _AUTHORED_SPRITE_RES_ROOT if is_sprite else _AUTHORED_RES_ROOT
    name = src.name
    model_key = f"authored:{src.stem}"
    rm = used_models.get(model_key)
    if rm is not None and Path(rm.src) != src.resolve():
        # Same stem, different authored file — e.g. two placed groups each
        # named car.fbx from different vendor folders. Same collision class
        # as _register's asset: key; disambiguate the same way.
        tag = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:8]
        name = f"{src.stem}-{tag}{src.suffix}"
        model_key = f"authored:{Path(name).stem}"
        rm = used_models.get(model_key)
    dest_rel = f"{res_root}/Authored/{name}"
    if rm is None:
        rm = ResolvedModel(
            kind=model_key,
            src=str(src.resolve()),
            dest_rel=dest_rel,
            resource_path=dest_rel.rsplit(".", 1)[0],
            rig="sprite" if is_sprite else "generic",
            scale=1.0,
            source="scene_compiler",
            fit_size=0.0,
        )
        used_models[model_key] = rm
    return rm.resource_path


def resolve_entity_assets(schema,
                          used_models: dict[str, ResolvedModel],
                          all_models: dict[str, ResolvedModel],
                          lookups_dir: str = "lookups",
                          asset_cfg: Optional[dict] = None,
                          strict: bool = False,
                          sketchfab_cfg: Optional[dict] = None,
                          llm: Optional[Any] = None) -> dict[str, str]:
    """Assign a real model to every entity group.

    Mutates `used_models` (adds catalog picks so the existing copy/.meta/
    ModelSwap chain fires for them) and returns entity_model_paths
    {entity_name: Resources.Load path}.

    `sketchfab_cfg` (config `sketchfab:` section) is None/absent/disabled by
    default — every existing caller keeps today's Kenney-only behavior
    byte-for-byte. When `sketchfab_cfg["enabled"]` is true, a Tier S runs
    before the Kenney catalog tiers (see the loop below): it reads whatever
    Sketchfab picks are already frozen in lookups/unity/sketchfab_catalog.json
    (merged into `assets` by load_catalog), and — only if
    `sketchfab_cfg["auto_fetch"]` is also true — triggers a live search on a
    cache miss via src/sketchfab_freeze.py, freezing the result to disk before
    using it. That live fetch is the one deliberate, documented exception to
    this module otherwise being a pure, network-free lookup (see
    docs/model-pipeline-architecture.md's Sketchfab section).

    `llm` (the Pipeline LLM client, e.g. self.llm from UnityPipeline.run() —
    None from every caller that has none, including unity-validate's
    no-LLM replay) is passed straight through to a Tier S live fetch as an
    optional vision-based visual-QC step on top of that fetch's own keyword
    ranking — see unity_sketchfab_vision.select_best_candidate's docstring
    for what problem that closes and how it degrades. None (no LLM reachable,
    `sketchfab.vision_select` not enabled, a text-only model, any failure)
    leaves every existing Tier S behavior byte-for-byte unchanged; this
    parameter adds a capability, it never removes one.

    Raises ValueError in strict mode when an entity matches nothing real.
    """
    cfg = asset_cfg or {}
    min_score = float(cfg.get("min_match_score", _DEFAULT_MIN_SCORE))
    top_k = max(1, int(cfg.get("variety_top_k", _DEFAULT_TOP_K)))
    asset_root = cfg.get("asset_root")  # optional override for entry roots
    # Distribution sampling (see _DEFAULT_SAMPLE_POOL). 0.0 = disabled = the
    # historical best-match + round-robin path, byte-for-byte.
    sample_temp = max(0.0, float(cfg.get("sample_temperature", 0.0) or 0.0))
    sample_pool = max(1, int(cfg.get("sample_pool", _DEFAULT_SAMPLE_POOL)))
    sample_seed = int(cfg.get("sample_seed", 0) or 0)

    sk_cfg = sketchfab_cfg or {}
    sk_enabled = bool(sk_cfg.get("enabled"))
    sk_client = None
    if sk_enabled:
        from .sketchfab_client import SketchfabClient
        sk_client = SketchfabClient(sk_cfg)

    # Format gate by game type: 2D games may use sprites (png) and models;
    # everything else is models-only — a flat PNG must never be picked as a
    # 3D scene object.
    is_2d = getattr(schema, "game_type", "3D") == "2D"
    allowed_formats = {"fbx", "png"} if is_2d else {"fbx"}

    catalog = load_catalog(str(lookups_dir))
    _all_assets = [a for a in catalog.get("assets", []) if a.get("filename")
                  and a.get("format", "fbx") in allowed_formats]
    # Kenney's tiers (2a/2b/2c, below) must never see Sketchfab-sourced
    # entries — load_catalog() merges sketchfab_catalog.json into the SAME
    # "assets" list, so without this split a frozen Sketchfab pick could win
    # a Kenney-tier match even with sketchfab.enabled: false, silently
    # breaking the "disabled = today's behavior, untouched" guarantee.
    # Sketchfab entries live only in `sk_catalog_assets`, consumed solely by
    # Tier S below.
    assets = [a for a in _all_assets if a.get("source") != "sketchfab"]
    sk_catalog_assets = [a for a in _all_assets if a.get("source") == "sketchfab"]

    scene_words = {w for w in tokenize(schema.description or "")
                   if w not in _STOPWORDS}

    # A game that declared it has no avatar resolves its content to UI elements
    # rather than to models — see Tier U and the Tier 4 branch in the loop.
    embodied = is_embodied(schema)

    kind_sizes = load_kind_sizes(str(lookups_dir))
    clamp_sizes = load_clamp_sizes(str(lookups_dir))

    # Concept-layer taxonomy (optional). Precompute each asset's (broad, strong)
    # layer membership once so the per-entity pool is a cheap set intersection.
    # Absent file → `layers is None` and every layer branch below is skipped:
    # the resolver falls back to the original global literal matching verbatim.
    layers = load_concept_layers(str(lookups_dir))
    asset_membership: list[tuple[frozenset, frozenset]] = (
        [layers.membership(a) for a in assets] if layers else [])

    # Genre-aware layer bias. schema.meta.genre is stamped by
    # unity_archetypes.apply_intent ("universal" = no opinion → zero bias, so
    # every genre-less run scores byte-identically to before). The bias is an
    # additive term on an asset's STRONG layer memberships in the concept
    # tiers: a racing game boosts road_track/vehicle_ground and penalizes
    # aircraft/watercraft — the missing signal behind the DriftArcade run
    # resolving its kind="vehicle" opponents to a round-robin of airplanes.
    genre = str((getattr(schema, "meta", None) or {}).get("genre", "universal"))
    gbias: dict[str, float] = (
        load_genre_layer_bias(str(lookups_dir)).get(genre, {})
        if genre != "universal" else {})

    def _genre_bias(strong_m: frozenset) -> float:
        if not gbias or not strong_m:
            return 0.0
        return sum(gbias.get(layer, 0.0) for layer in sorted(strong_m))

    entity_model_paths: dict[str, str] = {}
    unmatched: list[tuple[str, str, list[str]]] = []   # (name, kind, near misses)
    defaulted: list[tuple[str, str]] = []
    rotation: dict[tuple[str, ...], int] = {}          # candidate-set → next index
    # Style-coherence: the FIRST successful Tier S pick's author (Sketchfab
    # `username`) becomes "the project's artist" for every later entity —
    # fetch_and_freeze() tries that author's own catalog before the open
    # marketplace, so independently-sourced models don't drift into a
    # "constructed chaos" of unrelated styles. Set once, never overwritten,
    # even if a later entity has to fall back to a different author.
    preferred_author: Optional[str] = None

    prop_fallback = all_models.get("prop") or next(iter(all_models.values()), None)

    # Set True the moment any entity resolves to racing-core content (a
    # vehicle_ground/road_track catalog pick, or the default-pack vehicle);
    # checked after the loop when genre == "racing".
    racing_core_seen = False

    def _register(scored: list[tuple[float, dict]]) -> Optional[str]:
        """Variety-pick from a sorted (score, asset) list, register the pick in
        used_models, and return its Resources.Load path (None if empty).

        Two selection modes, both deterministic and both rotating by a
        per-candidate-set counter so same-query groups get DISTINCT models:

        - sample_temperature == 0 (default): candidates within _VARIETY_BAND of
          the best score round-robin. Narrow and safe, but only ever surfaces
          the handful of top-scored models — the reason a 2941-asset catalog
          kept showing the same ~20 files.
        - sample_temperature  > 0: the top `sample_pool` scores are normalized
          into a softmax distribution and SAMPLED with a seeded RNG. Reaches
          the whole field while staying reproducible (same schema + same
          assets.sample_seed → identical picks). The draw index feeds the seed,
          so consecutive groups sharing a candidate set draw DIFFERENT models
          rather than re-drawing the same one.

        Shared across the concept-layer and global match paths so every pick
        flows through one copy/.meta registration.
        """
        if not scored:
            return None
        if sample_temp > 0.0:
            pool = scored[:sample_pool]
            key = tuple(a["filename"] for _, a in pool)
            idx = rotation.get(key, 0)
            rotation[key] = idx + 1
            weights = _softmax_weights([s for s, _ in pool], sample_temp)
            rng = random.Random(_stable_seed(sample_seed, idx, key))
            pick = rng.choices([a for _, a in pool], weights=weights, k=1)[0]
        else:
            best = scored[0][0]
            band = [a for s, a in scored[:top_k] if s >= best * _VARIETY_BAND]
            key = tuple(a["filename"] for a in band)
            idx = rotation.get(key, 0)
            rotation[key] = idx + 1
            pick = band[idx % len(band)]

        nonlocal racing_core_seen
        if (not racing_core_seen and layers
                and layers.membership(pick)[1] & _RACING_CORE_LAYERS):
            racing_core_seen = True

        src = _entry_source(pick, asset_root)
        if src is None:
            # Catalog entry points at a file that no longer exists on disk —
            # treat as unresolved rather than crash on src.resolve() below.
            return None
        category = (pick.get("category") or "prop").capitalize()
        is_sprite = pick.get("format") == "png"
        if pick.get("source") == "sketchfab":
            res_root = _SKETCHFAB_RES_ROOT
        elif is_sprite:
            res_root = _SPRITE_RES_ROOT
        else:
            res_root = _RES_ROOT
        fname = pick["filename"]
        model_key = f"asset:{Path(fname).stem}"
        rm = used_models.get(model_key)
        if rm is not None and Path(rm.src) != src.resolve():
            # Same filename STEM, different source file — e.g. a standalone
            # banner-double-high.fbx and one bundled inside marbles-kit/ both
            # tokenize to "banner-double-high" (625 such collisions live in
            # the catalog, 125 of them spanning different categories/formats,
            # including FBX-vs-sprite pairs like corridor.fbx/corridor.png).
            # Reusing the bare-stem key/dest_rel here would silently hand this
            # entity the FIRST-registered file's ResolvedModel, and later a
            # disk copy of one file would overwrite the other at the same
            # Resources path — two different assets answering to one path.
            # Disambiguate with a short stable hash of the catalog relpath
            # (sha1, not hash(), for the same PYTHONHASHSEED-reproducibility
            # reason _stable_seed uses it above).
            tag = hashlib.sha1(
                (pick.get("relpath") or fname).encode("utf-8")).hexdigest()[:8]
            fname = f"{Path(fname).stem}-{tag}{Path(fname).suffix}"
            model_key = f"asset:{Path(fname).stem}"
            rm = used_models.get(model_key)
        dest_rel = f"{res_root}/{category}/{fname}"
        if rm is None:
            # Bounds-fit target: Sketchfab picks only. Kenney/default-pack
            # scale is already pack-internally consistent (modular pieces like
            # car-kit track segments would break their relative tiling if
            # individually normalized) — only the wild-card source (arbitrary
            # artist-authored real-world scale) gets a runtime fit.
            fit_size = (kind_sizes.get(kind, 0.0)
                        if pick.get("source") == "sketchfab" else 0.0)
            # No uniform fit → the shrink-only ceiling still applies, ANY
            # source (negative sentinel; spawner: min(1, clamp/measured)) —
            # the giant-vehicle guard for picks the Sketchfab-only fit
            # doesn't cover. See load_clamp_sizes.
            if fit_size == 0.0 and clamp_sizes.get(kind):
                fit_size = -clamp_sizes[kind]
            rm = ResolvedModel(
                kind=model_key,
                src=str(src.resolve()),
                dest_rel=dest_rel,
                resource_path=dest_rel.rsplit(".", 1)[0],
                # "sprite" rig = 2D texture: no ModelImporter .meta is authored
                # (Unity's default TextureImporter handles PNG), and the spawner
                # loads it as Texture2D → Sprite.Create.
                rig="sprite" if is_sprite else "generic",
                scale=float(pick.get("scale", 1.0) or 1.0),
                source=pick.get("source", "kenney"),
                license=pick.get("license"),
                attribution=pick.get("attribution"),
                fit_size=fit_size,
            )
            used_models[model_key] = rm
        return rm.resource_path

    # Tier A source table: {entity_name: {src, file, type, scale}} stamped on
    # schema.meta by src/unity_scenebind.py when the build was given a Scene
    # Compiler layout (`unity-create --scene`). Absent -> {} -> every tier
    # below behaves exactly as it did before authored scenes existed.
    authored_table = authored_models(schema)

    for e in schema.entities:
        kind = (e.kind or "prop").lower()
        hint = (getattr(e, "model_hint", "") or "").strip().lower()

        # ── Tier A: authored model (Scene Compiler) ──────────────────────────
        # The author opened the model, looked at it, and put it there. Nothing
        # downstream gets to overrule that: this runs ahead of the explicit
        # kind, Sketchfab and the Kenney catalog, and takes no runtime
        # bounds-fit or shrink clamp (fit_size stays 0.0) because the size
        # they arranged IS the intended size.
        _authored = authored_table.get(e.name)
        if _authored:
            _apath = _register_authored(_authored, used_models, is_2d)
            if _apath:
                entity_model_paths[e.name] = _apath
                if kind == "vehicle":
                    racing_core_seen = True
                continue
            # Source file vanished between binding and resolution — fall
            # through to the normal tiers rather than spawn nothing, but say
            # so: the scene will not match what the author arranged.
            _log.warning("[assets] authored model missing for %s: %s — "
                         "falling back to the catalog tiers",
                         e.name, _authored.get("src"))

        # ── Tier U: UI element (non-embodied games only) ──────────────────────
        # In a game that declared it has no avatar, a "CommitNode" or a
        # "TerminalWindow" is not a prop standing in a world — it is a control
        # on a board, and the library has no model of one. It holds 15 HUD
        # glyph sprites and no UI kit at all, so every tier below can only ever
        # answer this entity with the wrong object: live, CodeVault's file
        # icons and folder containers resolved to Kenney furniture.
        #
        # `ui:<element>` is a POSITIVE resolution, not a fallback — the
        # spawner builds a real card for it (body, border, label). It comes
        # after Tier A because an author who placed a model by hand outranks
        # every derived choice, and before the catalog because a catalog hit
        # here is the bug.
        #
        # Entities that name no UI element fall straight through: a teaching
        # game about an engine legitimately contains an engine, and there IS a
        # model of that.
        if not embodied:
            element = ui_element_of(e)
            if element is not None:
                entity_model_paths[e.name] = f"ui:{element}"
                _log.info("[assets] entity '%s' (kind=%s) → ui:%s — a control "
                          "on the board, built rather than resolved",
                          e.name, kind, element)
                continue

        # ── Tier 1: explicit kind (schema-default "prop" is not explicit) ────
        tier1: Optional[ResolvedModel] = None
        if kind in used_models and (kind != "prop" or hint == "default prop"):
            tier1 = used_models[kind]
        elif kind == "prop" and hint == "default prop" and prop_fallback is not None:
            used_models.setdefault("prop", prop_fallback)
            tier1 = prop_fallback

        # A real model_hint asks for something more specific than the default
        # pack — "corsair plane" on kind="vehicle" must reach the catalog, not
        # stop at Car.fbx. Hold the tier-1 model as the safety net and try the
        # catalog first; `model_hint: "default <kind>"` opts back into tier 1.
        if tier1 is not None and hint in ("", f"default {kind}", "default prop"):
            if kind == "vehicle":
                racing_core_seen = True   # default-pack car is real racing content
            entity_model_paths[e.name] = tier1.resource_path
            continue

        # ── Tier 2: catalog semantic match ────────────────────────────────────
        qw = _query_weights(e, scene_words)
        strong_q = {t for t, w in qw.items() if w >= _STRONG_HIT}
        # Strong tokens that say something MORE than the entity's own kind.
        # "Waiters" (kind="character", model_hint "waiter") carries
        # waiter/waiters here; a bare "Characters" or "NPCs" carries nothing
        # (see _GENERIC_PERSON_TOKENS). Drives both weak-match branches of
        # _passes_kind_guard below.
        _kind_variants = {kind, kind + "s",
                          kind[:-1] if kind.endswith("s") else kind}
        _generic_q = _kind_variants | (_GENERIC_PERSON_TOKENS
                                       if kind in _PERSON_KINDS else set())
        # Built from what the entity DECLARES about itself — its name and
        # model_hint — not from strong_q. sketchfab_query is deliberately
        # excluded even though _query_weights scores it at 2.0: it is a
        # marketplace SEARCH string carrying context and style ("realistic
        # dirt track debris" describes debris lying on a dirt track), which
        # is a fine ranking signal but not a statement of what the object IS.
        # Counting it here let a Debris entity claim "track" as its identity
        # and take track-straight.fbx away from its own obstacle default.
        specific_q = {t for t in ((_tokens_with_plurals(e.name)
                                   | _tokens_with_plurals(hint)) - _generic_q)
                      if len(t) > 1 and t not in _STYLE_TOKENS}

        # The entity's concept SCOPE — its kind-constrained concept-layer band
        # (see ConceptLayers.band). Computed HERE, before Tier S, rather than at
        # Tier 2a where it used to live, so every scoring tier shares one
        # definition of "which domain is this entity in" (_passes_kind_scope).
        layer_band: set[str] = set()
        layer_top_hits = 0
        if layers:
            layer_band, layer_top_hits = layers.band(qw, kind, _STRONG_HIT)

        def _kind_only_hit(a: dict) -> bool:
            """True if this entry matches nothing but the entity's own kind —
            the match carries no signal beyond the bare category word."""
            akw = _entry_tokens(a)
            strong = {t for t, w in qw.items() if w >= _STRONG_HIT and t in akw}
            return bool(strong) and strong <= _generic_q

        def _passes_kind_guard(a: dict) -> bool:
            """Block catalog picks whose only tie to the entity is a weak,
            coincidental keyword — the class of bug behind two hit-live
            regressions: "GroundEnemies" (kind=enemy) matched a wood floor
            plank via the shared word "ground", and "StratoPlatform"
            (kind=platform) matched Stairs/steps.fbx via the shared word
            "platform" (one of seven loosely-associated tags on an unrelated
            staircase asset). Applied to Tier S (Sketchfab) as well as the
            Kenney tiers below — without it, a Sketchfab pick with a short
            keyword list can clear min_score on a single coincidental token
            (e.g. a "traffic cone" model matching an unrelated "AntigravDevice"
            or "BirdFlock" entity) and win over a real, correctly-tagged
            default (hit live: 5 unrelated entity kinds all resolved to the
            same collected Sketchfab prop).
            """
            role_tokens = _STRUCTURAL_ROLE_TOKENS.get(kind)
            if role_tokens is not None and getattr(e, "path_points", None):
                # Structural path role (see _STRUCTURAL_ROLE_TOKENS): the
                # filename itself must carry role evidence, whatever tier the
                # candidate came from and whatever its kind_hints claim.
                fname = {t for t in tokenize(Path(a.get("filename", "")).stem)
                         if len(t) > 1}
                if not (fname & role_tokens):
                    return False
            if specific_q and _kind_only_hit(a):
                # The entity NAMED something specific and this asset answers
                # none of it — the bare category word is not evidence, however
                # the library categorizes the asset and whatever its filename
                # says. Checked BEFORE the kind_hints short-circuit below,
                # which would otherwise wave it through. Hit live: "Waiters"
                # (kind="character", model_hint "waiter") took
                # character-skate-boy.fbx, which matches "character" and
                # nothing else — and cleared the old filename-evidence escape
                # because Kenney names its whole character pack "character-*",
                # making the kind word a naming prefix rather than an identity.
                # A grey capsule reads as "placeholder, replace me"; a skate
                # boy reads as a decision the pipeline made. Confidently wrong
                # is worse than obviously unfinished.
                return False
            hints = {h.lower() for h in a.get("kind_hints", [])}
            if kind in hints:
                return True
            if tier1 is not None:
                # A real default model already covers this kind. Two things may
                # displace it: the library itself categorizing the asset as
                # this kind (above), or IDENTITY EVIDENCE — a specific token
                # the entity named appearing in the asset's own FILENAME, the
                # same standard _sk_contextual_guard and _passes_kind_scope
                # apply. The library tags people npc/player/crowd/enemy and
                # never "character", so without this second escape a neutral
                # character default would block every catalog pick for that
                # kind, including "Astronauts" → astronautA.fbx, a match nobody
                # would call wrong.
                #
                # Filename, NOT the keyword list: keywords are where
                # coincidental overlap lives, and trusting them here reopens
                # the two regressions this guard exists for — "GroundEnemies"
                # (model_hint "ground") would take floor-plank.fbx, tagged
                # "ground", and "Debris" would take survivors-tent, tagged
                # "debris". Neither name says what the object is. A stray tag
                # is not enough to gamble away a known-good default (the
                # tier-1 fallback below fires if this empties the field).
                fname_tokens = {t for t in tokenize(Path(a.get("filename", "")).stem)
                                if len(t) > 1}
                return bool(specific_q & fname_tokens)
            if _kind_only_hit(a):
                # Nothing specific was asked for and no default to fall back
                # on, so any member of the kind is a fair answer — but only
                # when the object is actually named after the kind, not merely
                # tagged with it alongside unrelated keywords.
                fname_tokens = {t for t in tokenize(Path(a.get("filename", "")).stem)
                                if len(t) > 1}
                return kind in fname_tokens
            return True

        def _passes_kind_scope(a: dict) -> bool:
            """Kind-scoped candidate filtering: an asset that DECLARES a domain
            may only fill an entity scoped to that same domain.

            The keyword tiers score on token overlap alone, so a single
            incidental shared word reaches across the entire library. Hit live
            (tools/out/resolver_tiers.json, ood_sort): "ScoreBoard"
            (kind="ui", model_hint "score board ui") tokenizes to score/board/
            ui, and skateboard.fbx carries the curated keyword "board" — one
            strong hit, enough to win Tier 2b outright and park a skateboard
            where a scoreboard belongs. Nothing upstream objected: a "ui"
            entity classifies into no concept layer, so Tier 2a never ran, and
            _passes_kind_guard only rejects a match whose ONLY strong token is
            the bare kind word ("board" is not "ui").

            But the asset's own stamps already say what it is — skateboard.fbx
            is category "vehicle", kind_hints ["vehicle"]: STRONG concept-layer
            membership in the vehicle family. Three outcomes:

              - no strong membership   → the asset claims no narrow domain, so
                                         there is nothing to contradict; stays
                                         eligible (today's behavior, and about
                                         a third of the catalog).
              - strong membership that meets the entity's own layer band
                                       → in scope, eligible.
              - strong membership elsewhere (or an entity that names no concept
                at all, band empty)    → only the identity-evidence escape
                                         hatch, the same rule
                                         _sk_contextual_guard applies to
                                         marketplace tag soup: a strong query
                                         token must appear in the pack's own
                                         FILENAME. An entity that literally
                                         says "skateboard" still gets one;
                                         "board" alone never does.

            Complements rather than replaces _passes_kind_guard: that one asks
            "is this match more than a coincidental keyword?", this one asks
            "is this asset even in the entity's domain?". Inert when
            concept_layers.json is absent (`layers is None`) — like every other
            layer branch, no taxonomy means no opinion.
            """
            if layers is None:
                return True
            strong_m = layers.membership(a)[1]
            if not strong_m or (strong_m & layer_band):
                return True
            fname = {t for t in tokenize(Path(a.get("filename", "")).stem)
                     if len(t) > 1}
            return bool(strong_q & fname)

        def _sk_contextual_guard(a: dict) -> bool:
            """Marketplace tags describe an upload's CONTEXT as often as its
            identity — a traffic cone is tagged "race"/"road"/"street" because
            it lives near roads; a survivors' tent is tagged "debris"/"crash"
            because it was modeled beside a crash site. Both stamps are honest
            (category prop/obstacle — not self-certification, not style
            adjectives), yet both won Tier S live: TrackSegments (kind="track")
            tiled the cone, "Debris" got the tent. Two checks, Sketchfab-only
            (Kenney tags are curated, and its tiers are concept-layer-
            constrained already):

            1. Surface gate — a _SURFACE_KINDS entity only takes an entry the
               taxonomy places STRONGLY (category/kind_hints) in a surface
               layer (_SURFACE_LAYERS); contextual keywords are broad
               membership at most. Skipped when concept_layers.json is absent.
            2. Identity evidence — a strong query token must appear in the
               uploader's own model NAME (filename stem — the one field that
               is neither tag soup nor a freeze-time stamp), or at least TWO
               strong tokens must hit the tag list. One stray tag in a 20-tag
               soup is context, not identity.
            """
            if (kind in _SURFACE_KINDS and layers is not None
                    and not (layers.membership(a)[1] & _SURFACE_LAYERS)):
                return False
            strong_q = {t for t, w in qw.items() if w >= _STRONG_HIT}
            fname_tokens = {t for t in tokenize(Path(a.get("filename", "")).stem)
                            if len(t) > 1}
            if strong_q & fname_tokens:
                return True
            return len(strong_q & _entry_tokens(a)) >= 2

        # ── Tier S: Sketchfab-first (config-gated; runs before Kenney tiers) ──
        # Gated the same as the FX/skip kinds below and 2D games — Sketchfab
        # only ever yields 3D FBX, which a 2D orthographic game has no use for.
        # Reusing _score/_query_weights/_entry_source/_register verbatim keeps
        # Kenney's own tiers (2a/2b/2c, below) completely unchanged — they just
        # run only when Tier S misses, which is what "leaving Kenney as an
        # offline option" means concretely. _passes_kind_guard applies here too
        # (see its docstring) — Tier S is not exempt from the same weak-match
        # protection Tier 2b already enforces.
        if sk_enabled and not is_2d and kind not in _FX_KINDS and kind not in _SKIP_KINDS:
            sk_min = float(sk_cfg.get("min_match_score", min_score))

            def _sk_score(a: dict) -> float:
                # Same genre bias the local concept tiers get — a cached
                # Sketchfab airplane must not win a racing game's vehicle
                # slot any more than a Kenney one.
                s = _score(a, qw, kind)
                if s > 0.0 and gbias and layers:
                    s += _genre_bias(layers.membership(a)[1])
                return s

            sk_scored = [(s, a) for a in sk_catalog_assets
                        if (s := _sk_score(a)) >= sk_min
                        and _entry_source(a, asset_root) is not None
                        and _passes_kind_guard(a)
                        and _passes_kind_scope(a)
                        and _sk_contextual_guard(a)]
            if not sk_scored and sk_cfg.get("auto_fetch") and sk_client is not None:
                from .sketchfab_freeze import fetch_and_freeze
                # sketchfab_query (src/unity_sketchfab_translator.py's concept +
                # shared-style-brief enrichment) wins when present; falls back
                # to today's mechanical model_hint / strong-token derivation,
                # genre-prefixed so a bare "vehicle" search in a racing
                # project asks the marketplace for "racing vehicle".
                query = (getattr(e, "sketchfab_query", "") or hint
                        or " ".join(sorted(t for t, w in qw.items() if w >= _STRONG_HIT)))
                if genre != "universal" and not getattr(e, "sketchfab_query", ""):
                    query = f"{genre.replace('_', ' ')} {query}".strip()
                # Visual QC on the keyword-ranked candidates below, closing
                # the class of bug keyword scoring structurally cannot catch
                # (a same-word coincidence like "helmet" resolving a
                # skydiving jumpsuit to a baseball asset) — see
                # unity_sketchfab_vision.py's own docstring. Opt-in
                # (`sketchfab.vision_select`) and only reachable when a
                # caller actually threaded an `llm` through (unity-validate's
                # no-LLM replay never does) — None either way leaves this
                # exact Tier S behavior untouched.
                vision_select_fn = None
                if llm is not None and sk_cfg.get("vision_select"):
                    from .unity_sketchfab_vision import select_best_candidate
                    vision_select_fn = lambda cands, _e=e, _kind=kind: select_best_candidate(
                        _e.name, _kind, hint or _e.name, cands,
                        client=sk_client, llm=llm)
                new_entry = fetch_and_freeze(
                    query, score_fn=lambda a: _score(a, qw, kind),
                    client=sk_client, blender_cfg=sk_cfg,
                    cache_dir=sk_cfg.get("cache_dir", "Models/Sketchfab"),
                    lookups_dir=str(lookups_dir), category=kind or "prop",
                    kind_hints=[kind], min_score=sk_min,
                    preferred_author=preferred_author,
                    vision_select_fn=vision_select_fn)
                if new_entry is not None:
                    sk_catalog_assets.append(new_entry)  # in-run cache: later
                                                          # entities this same
                                                          # call can reuse it
                    s = _sk_score(new_entry)
                    if (s >= sk_min and _passes_kind_guard(new_entry)
                            and _passes_kind_scope(new_entry)
                            and _sk_contextual_guard(new_entry)):
                        sk_scored = [(s, new_entry)]
            if sk_scored:
                sk_scored.sort(key=lambda sa: (-sa[0], sa[1]["filename"]))
                if preferred_author is None:
                    preferred_author = sk_scored[0][1].get("author_username") or None
                picked = _register(sk_scored)
                if picked is not None:
                    entity_model_paths[e.name] = picked
                    continue

        def _final_score(a: dict) -> float:
            s = _score(a, qw, kind)
            if s > 0.0 and is_2d and a.get("format") == "png":
                s += _SPRITE_2D_BONUS
            return s

        # ── Tier 2a: concept-layer match (find layer → best asset IN layer) ──
        # Classify the entity into concept layer(s) from its own strong tokens
        # (kind-constrained), then rank ONLY the assets that belong to those
        # layers. A strong (category/kind_hint) member gets the alignment bonus
        # so an on-concept asset beats an off-concept one that merely edges
        # ahead on generic_score. This is what stops cross-domain drift before
        # the literal global match can happen.
        if layer_band:
            layer_scored: list[tuple[float, dict]] = []
            for a, (broad, strong_m) in zip(assets, asset_membership):
                # _passes_kind_guard applies HERE too, not just at Tier 2b.
                # Without it this tier had no weak-match protection at all: an
                # entity that names a concept layer but whose specific hint
                # matches nothing got the best-scoring member of that layer,
                # with the alignment bonus on top. For character_humanoid that
                # is "the most character-ish humanoid in the library" — the
                # path a kind="character"/model_hint="waiter" entity took to
                # character-skate-boy.fbx while Tier 2b's guard sat unused
                # one tier below.
                if (not (broad & layer_band) or not _passes_kind_scope(a)
                        or not _passes_kind_guard(a)):
                    continue
                is_strong = bool(strong_m & layer_band)
                # When a real default already covers this kind, only a STRONG
                # (category/kind_hint) member of the layer may displace it — a
                # stray keyword must not swap the player's humanoid for a
                # parachute prop just because "wingsuit" is one of its tags.
                # With no default, a keyword-only member is still better than
                # nothing, so broad members stay in.
                if tier1 is not None and not is_strong:
                    continue
                base = _final_score(a)
                if base < min_score or _entry_source(a, asset_root) is None:
                    continue
                if is_strong:
                    base += _LAYER_ALIGN_BONUS
                base += _genre_bias(strong_m)
                if base < min_score:
                    continue   # genre penalty pushed an off-genre asset out
                layer_scored.append((base, a))
            layer_scored.sort(key=lambda sa: (-sa[0], sa[1]["filename"]))
            picked = _register(layer_scored)
            if picked is not None:
                entity_model_paths[e.name] = picked
                continue

        # ── Tier 2b: global literal match (fallback / concept-layers absent) ──
        # _kind_only_hit / _passes_kind_guard are defined above (before Tier S)
        # so both Tier S and Tier 2b share the exact same weak-match guard.
        # _passes_kind_scope is a HARD filter here, applied to `candidates`
        # rather than alongside _passes_kind_guard below: the guarded/raw
        # fallback deliberately relaxes the weak-match guard when it would
        # otherwise empty the field, and that relaxation must never hand back
        # an out-of-domain asset (ScoreBoard has no tier-1 default, so the raw
        # fallback is exactly the branch skateboard.fbx came back through).
        candidates = [(s, a) for a in assets
                      if (s := _final_score(a)) >= min_score
                      and _entry_source(a, asset_root) is not None
                      and _passes_kind_scope(a)]
        guarded = [sa for sa in candidates if _passes_kind_guard(sa[1])]
        # Guarding never regresses below today's behavior: with a tier-1
        # default it's a hard filter (empty just falls through to the
        # tier-1 fallback below); without one, prefer the guarded ranking
        # but fall back to the raw one rather than risk a strict-mode
        # hard-fail for a kind the catalog has no trustworthy tag for.
        scored = sorted(guarded if (guarded or tier1 is not None) else candidates,
                        key=lambda sa: (-sa[0], sa[1]["filename"]))
        picked = _register(scored)
        if picked is not None:
            entity_model_paths[e.name] = picked
            continue

        # Hinted entity with no catalog hit: the default-pack model for its
        # kind is still real, kind-correct content — use it rather than
        # degrading to a procedural mesh, the generic prop, or a strict fail.
        if tier1 is not None:
            if kind == "vehicle":
                racing_core_seen = True
            entity_model_paths[e.name] = tier1.resource_path
            continue

        # ── Tier 2c: best-in-layer fallback (kill the "same 15 defaults") ────
        # No literal token hit anywhere, and no default-pack model for this
        # kind — the old path went straight to the generic prop (or a strict
        # fail). But the entity DID name a concept layer (a real vocabulary
        # token, layer_top_hits ≥ 1), and that layer has real assets, so pick
        # the most on-concept one instead of the placeholder. Gated on a vocab
        # hit so a kind-only band never forces an arbitrary asset.
        if layer_band and layer_top_hits >= 1:
            soft: list[tuple[float, dict]] = []
            for a, (broad, strong_m) in zip(assets, asset_membership):
                if (not (broad & layer_band)
                        or _entry_source(a, asset_root) is None
                        or not _passes_kind_scope(a)):
                    continue
                # Same signage embargo the _score tiers enforce — this soft
                # path scores without _score, so it needs its own check.
                if _signage_blocked(_entry_tokens(a), qw, kind):
                    continue
                s = 0.05
                if strong_m & layer_band:
                    s += _LAYER_ALIGN_BONUS
                if kind in {h.lower() for h in a.get("kind_hints", [])}:
                    s += 0.5
                s += float(a.get("generic_score", 0)) / 50.0
                akw = {k.lower() for k in a.get("keywords", [])}
                s += 0.1 * len(scene_words & akw)
                if is_2d and a.get("format") == "png":
                    s += _SPRITE_2D_BONUS
                s += _genre_bias(strong_m)
                if s <= 0.0:
                    continue   # genre penalty: off-genre beats nothing, but
                               # never at a negative score
                soft.append((s, a))
            soft.sort(key=lambda sa: (-sa[0], sa[1]["filename"]))
            picked = _register(soft)
            if picked is not None:
                _log.info("[assets] entity '%s' (kind=%s) → best-in-layer %s "
                          "(concept layers %s)", e.name, kind, picked,
                          sorted(layer_band))
                entity_model_paths[e.name] = picked
                continue

        # ── Tier 3: schema procedural mesh ────────────────────────────────────
        # "AtmosphericClouds" with a CloudLayer1 procedural mesh in the same
        # schema: the mesh is the intended content — build it at spawn time.
        proc = _procedural_route(e, getattr(schema, "procedural_meshes", None))
        if proc is not None:
            entity_model_paths[e.name] = proc
            _log.info("[assets] entity '%s' routed to %s (schema procedural mesh)",
                      e.name, proc)
            continue

        # ── Non-mesh kinds: fx → particles, ui/background → skip ─────────────
        # Reached only when the catalog and procedural tiers missed — these
        # kinds never needed an FBX, so a miss is expected, not a failure.
        if kind in _FX_KINDS:
            entity_model_paths[e.name] = "fx:particles"
            _log.info("[assets] entity '%s' (kind=%s) → runtime ParticleSystem",
                      e.name, kind)
            continue
        if kind in _SKIP_KINDS:
            entity_model_paths[e.name] = f"skip:{kind}"
            _log.info("[assets] entity '%s' (kind=%s) → no 3D spawn (owned by "
                      "SceneSetup / sky pipeline)", e.name, kind)
            continue

        # ── Structural path roles never take a wrong-shape fallback ──────────
        # A road ribbon / barrier rail with no role-evidenced model (see
        # _STRUCTURAL_ROLE_TOKENS) spawns as the primitive tile ribbon —
        # colored cubes laid along the path READ as a road; a chain of generic
        # props (or a strict hard-fail over geometry the spawner can build
        # itself) does not.
        if (_STRUCTURAL_ROLE_TOKENS.get(kind) is not None
                and getattr(e, "path_points", None)):
            _log.info("[assets] entity '%s' (kind=%s) has no role-evidenced "
                      "model — spawning as primitive tile ribbon", e.name, kind)
            continue

        # ── Tier 4: fallback policy ───────────────────────────────────────────
        # On a board, "nothing in the library matches" is not a failure to
        # report or a prop to guess at — it is the answer. The entity is a
        # control the brief named in its own domain vocabulary ("Remote",
        # "Staging Area", "Conflict"), and the honest build for it is a labelled
        # card, not a Kenney crate and not a hard stop over a model that was
        # never going to exist. Deliberately ahead of BOTH the strict and the
        # prop-fallback branches, because both of them assume a world.
        if not embodied:
            entity_model_paths[e.name] = "ui:card"
            _log.info("[assets] entity '%s' (kind=%s) matched no model — "
                      "building it as a labelled card (ui:card). A "
                      "non-embodied game's unmatched content is UI.",
                      e.name, kind)
            continue
        if strict:
            unmatched.append((e.name, e.kind,
                              _nearest_misses(assets, qw, kind)))
        elif prop_fallback is not None:
            used_models.setdefault("prop", prop_fallback)
            entity_model_paths[e.name] = prop_fallback.resource_path
            defaulted.append((e.name, e.kind))

    if unmatched:
        detail = "; ".join(
            f"'{name}' (kind={kind!r}"
            + (f", closest: {', '.join(near)}" if near else "")
            + ")"
            for name, kind, near in unmatched)
        raise ValueError(
            f"[assets.strict] no real model matches entity {detail}. "
            "Give the entity a model_hint matching the asset library "
            "(run: python main.py unity-catalog --dry-run for the vocabulary), "
            "map its kind in lookups/unity/default_models.json, add a "
            "procedural_meshes entry sharing a name word with the entity, or "
            "set assets.strict: false."
        )
    if defaulted:
        _log.warning(
            "[assets] no catalog match for entity/kind(s) %s — using default "
            "prop '%s'. Add a model_hint or extend the asset library.",
            sorted({f"{n}({k})" for n, k in defaulted}),
            prop_fallback.resource_path if prop_fallback else "?")

    # FAIL-FAST genre coherence: a strict-mode 3D racing project that resolved
    # every entity yet holds zero racing-core content (no ground vehicle, no
    # track piece) matches the intended genre in name only — hard-fail with
    # the fix, exactly like the strict unmatched-entity path above. Gated on
    # `layers` (no taxonomy → membership can't be judged) and on 3D (2D racing
    # resolves sprites the layer vocabulary doesn't reliably cover).
    if (genre == "racing" and strict and not is_2d and layers
            and schema.entities and not racing_core_seen):
        raise ValueError(
            "[assets.strict] genre is 'racing' but no entity resolved to a "
            "ground vehicle or track piece (concept layers vehicle_ground/"
            "road_track). Add entities like a drivable car (kind='vehicle'), "
            "track segments (kind='track' or model_hint 'race track'), and "
            "barriers/checkpoints, or set assets.strict: false."
        )

    return entity_model_paths


def texture_dirs_for(used_models: dict[str, ResolvedModel]) -> list[Path]:
    """Texture folders that must ship with the catalog picks.

    Kenney FBX reference PNGs in a `Textures/` folder that lives beside the
    models on disk; Unity's importer searches Textures folders up the parent
    chain, so copying each pack's folder next to the imported models keeps
    materials from coming in blank. Only catalog picks (kind 'asset:*') need
    this — the default pack ships texture-free placeholders.
    """
    dirs: list[Path] = []
    seen: set[str] = set()
    for key, m in sorted(used_models.items()):
        if not key.startswith(("asset:", "authored:")) or m.rig == "sprite":
            continue  # sprites are self-contained PNGs — no FBX texture folder
        for parent in Path(m.src).parents:
            tex = parent / "Textures"
            if tex.is_dir():
                if str(tex) not in seen:
                    seen.add(str(tex))
                    dirs.append(tex)
                break
    return dirs


def texture_dirs_with_res_root_for(
        used_models: dict[str, ResolvedModel]) -> list[tuple[Path, str]]:
    """Like texture_dirs_for, but paired with the Resources-relative root each
    model was actually copied under ("Models/Kenney" or "Models/Sketchfab").

    Unity's FBX importer only finds a Textures/ folder by walking UP the
    parent chain from the model's OWN copied location — a texture folder
    dropped under the wrong subtree is never found, even though it exists
    on disk. Root-caused live: every Sketchfab-sourced model rendered black
    in Play mode because the caller previously wrote every catalog pick's
    textures to the single hardcoded Assets/Resources/Models/Kenney/Textures/
    path regardless of source, which is not an ancestor of a Sketchfab
    pick's own Assets/Resources/Models/Sketchfab/<Category>/ location.
    """
    pairs: list[tuple[Path, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, m in sorted(used_models.items()):
        if not key.startswith(("asset:", "authored:")) or m.rig == "sprite":
            continue
        res_root = _RES_ROOT_BY_SOURCE.get(m.source, _RES_ROOT)
        for parent in Path(m.src).parents:
            tex = parent / "Textures"
            if tex.is_dir():
                sig = (str(tex), res_root)
                if sig not in seen:
                    seen.add(sig)
                    pairs.append((tex, res_root))
                break
    return pairs
