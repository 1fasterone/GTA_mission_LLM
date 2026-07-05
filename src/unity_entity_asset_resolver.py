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
  3. fallback       — strict mode hard-fails with the query tokens and near
                      misses (FAIL-FAST doctrine); non-strict keeps the old
                      prop fallback + warning.

Deterministic: pure lookup + arithmetic, stable tie-breaks, no RNG, no LLM.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from .unity_asset_catalog import load_catalog, tokenize
from .unity_concept_layers import load_concept_layers
from .unity_model_resolver import ResolvedModel

_log = logging.getLogger(__name__)

# Resources.Load roots for catalog picks. Distinct from the default pack's
# "Models/Default/" so the two never collide; 3 path parts keeps write()'s
# secondary-copy prefix strip (Assets/<root>/<Category>/<file>) working.
_RES_ROOT = "Models/Kenney"
_SPRITE_RES_ROOT = "Sprites/Kenney"

# 2D games prefer sprites over 3D models when both match an entity — a fish
# PNG beats a marble-track FBX in a side-scroller. Applied only to entries
# that already cleared the strong-hit gate.
_SPRITE_2D_BONUS = 0.75

_DEFAULT_MIN_SCORE = 0.15
_DEFAULT_TOP_K = 5
_VARIETY_BAND = 0.85          # candidates within 85% of the best score rotate

# Bonus added to a candidate that is a STRONG member of the entity's concept
# layer (its category / kind_hint places it there, not a stray keyword). Large
# enough to lift an on-concept asset over an off-concept one that only edges
# ahead on generic_score — the exact flip behind "StratoPlatform → steps.fbx",
# where a staircase (higher generic_score, category=stairs) out-pointed a real
# platform (category=structure) purely on commonness.
_LAYER_ALIGN_BONUS = 1.0

# Generic query words that appear in almost any scene description — they carry
# no signal about which MODEL fits, so they never count as keyword hits.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "with", "for", "to",
    "game", "player", "level", "scene", "world", "entity", "entities",
    "group", "object", "objects", "item", "items", "prop", "props",
})


def _slug(name: str) -> str:
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", name or "").split()
    return "".join(p.capitalize() for p in parts) or "Entity"


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


def _score(entry: dict, query_weights: dict[str, float], kind: str) -> float:
    """Weighted keyword overlap + kind-hint bonus + generic tiebreak.

    Returns 0.0 unless a STRONG token (entity name / kind / model_hint, weight
    ≥ 1.0) matches. Scene-description words (weight 0.1) are tiebreakers among
    strong matches only — hit live: "AtmosphericClouds" grabbed steps.fbx
    because the description word "vertical" (0.1) qualified the asset and then
    the +0.5 prop kind bonus carried it over the threshold.
    """
    kw = set(k.lower() for k in entry.get("keywords", []))
    kw.update(t for t in tokenize(Path(entry.get("filename", "")).stem)
              if len(t) > 1)   # "fence-1x2" leaves a bare "x" — never a keyword
    hits = {t: w for t, w in query_weights.items() if t in kw}
    if not hits or max(hits.values()) < _STRONG_HIT:
        return 0.0
    score = sum(hits.values()) / max(1.0, len(kw)) ** 0.5
    if kind in entry.get("kind_hints", []):
        score += 0.5
    return score + entry.get("generic_score", 0) / 50.0


def _query_weights(entity, scene_words: set[str]) -> dict[str, float]:
    """Token → weight for one entity. model_hint counts double, the scene
    description barely at all (it only breaks ties toward on-theme assets)."""
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
    # Singular/plural bridging for strong tokens: entity names are usually
    # plural ("PineTrees") while catalog keywords are singular ("tree").
    for t, w in list(weights.items()):
        if w >= _STRONG_HIT:
            if t.endswith("s") and len(t) > 3:
                weights.setdefault(t[:-1], w)
            else:
                weights.setdefault(t + "s", w)
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


def resolve_entity_assets(schema,
                          used_models: dict[str, ResolvedModel],
                          all_models: dict[str, ResolvedModel],
                          lookups_dir: str = "lookups",
                          asset_cfg: Optional[dict] = None,
                          strict: bool = False) -> dict[str, str]:
    """Assign a real model to every entity group.

    Mutates `used_models` (adds catalog picks so the existing copy/.meta/
    ModelSwap chain fires for them) and returns entity_model_paths
    {entity_name: Resources.Load path}.

    Raises ValueError in strict mode when an entity matches nothing real.
    """
    cfg = asset_cfg or {}
    min_score = float(cfg.get("min_match_score", _DEFAULT_MIN_SCORE))
    top_k = max(1, int(cfg.get("variety_top_k", _DEFAULT_TOP_K)))
    asset_root = cfg.get("asset_root")  # optional override for entry roots

    # Format gate by game type: 2D games may use sprites (png) and models;
    # everything else is models-only — a flat PNG must never be picked as a
    # 3D scene object.
    is_2d = getattr(schema, "game_type", "3D") == "2D"
    allowed_formats = {"fbx", "png"} if is_2d else {"fbx"}

    catalog = load_catalog(str(lookups_dir))
    assets = [a for a in catalog.get("assets", []) if a.get("filename")
              and a.get("format", "fbx") in allowed_formats]

    scene_words = {w for w in tokenize(schema.description or "")
                   if w not in _STOPWORDS}

    # Concept-layer taxonomy (optional). Precompute each asset's (broad, strong)
    # layer membership once so the per-entity pool is a cheap set intersection.
    # Absent file → `layers is None` and every layer branch below is skipped:
    # the resolver falls back to the original global literal matching verbatim.
    layers = load_concept_layers(str(lookups_dir))
    asset_membership: list[tuple[frozenset, frozenset]] = (
        [layers.membership(a) for a in assets] if layers else [])

    entity_model_paths: dict[str, str] = {}
    unmatched: list[tuple[str, str, list[str]]] = []   # (name, kind, near misses)
    defaulted: list[tuple[str, str]] = []
    rotation: dict[tuple[str, ...], int] = {}          # candidate-set → next index

    prop_fallback = all_models.get("prop") or next(iter(all_models.values()), None)

    def _register(scored: list[tuple[float, dict]]) -> Optional[str]:
        """Variety-pick from a sorted (score, asset) list, register the pick in
        used_models, and return its Resources.Load path (None if empty).

        Deterministic: candidates within _VARIETY_BAND of the best rotate by a
        per-candidate-set counter, so same-query groups get distinct models and
        repeat runs match exactly. Shared across the concept-layer and global
        match paths so every pick flows through one copy/.meta registration.
        """
        if not scored:
            return None
        best = scored[0][0]
        band = [a for s, a in scored[:top_k] if s >= best * _VARIETY_BAND]
        key = tuple(a["filename"] for a in band)
        idx = rotation.get(key, 0)
        rotation[key] = idx + 1
        pick = band[idx % len(band)]

        src = _entry_source(pick, asset_root)
        category = (pick.get("category") or "prop").capitalize()
        is_sprite = pick.get("format") == "png"
        res_root = _SPRITE_RES_ROOT if is_sprite else _RES_ROOT
        dest_rel = f"{res_root}/{category}/{pick['filename']}"
        model_key = f"asset:{Path(pick['filename']).stem}"
        rm = used_models.get(model_key)
        if rm is None:
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
            )
            used_models[model_key] = rm
        return rm.resource_path

    for e in schema.entities:
        kind = (e.kind or "prop").lower()
        hint = (getattr(e, "model_hint", "") or "").strip().lower()

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
            entity_model_paths[e.name] = tier1.resource_path
            continue

        # ── Tier 2: catalog semantic match ────────────────────────────────────
        qw = _query_weights(e, scene_words)

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
        layer_band: set[str] = set()
        layer_top_hits = 0
        if layers:
            layer_band, layer_top_hits = layers.band(qw, kind, _STRONG_HIT)
        if layer_band:
            layer_scored: list[tuple[float, dict]] = []
            for a, (broad, strong_m) in zip(assets, asset_membership):
                if not (broad & layer_band):
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
                layer_scored.append((base, a))
            layer_scored.sort(key=lambda sa: (-sa[0], sa[1]["filename"]))
            picked = _register(layer_scored)
            if picked is not None:
                entity_model_paths[e.name] = picked
                continue

        def _kind_only_hit(a: dict) -> bool:
            """True if `kind` is the ONLY strong token this entry matches —
            the match carries no signal beyond the bare kind word itself."""
            akw = set(k.lower() for k in a.get("keywords", []))
            akw.update(t for t in tokenize(Path(a.get("filename", "")).stem)
                       if len(t) > 1)
            strong = {t for t, w in qw.items() if w >= _STRONG_HIT and t in akw}
            kind_variants = {kind, kind + "s",
                             kind[:-1] if kind.endswith("s") else kind}
            return bool(strong) and strong <= kind_variants

        def _passes_kind_guard(a: dict) -> bool:
            """Block catalog picks whose only tie to the entity is a weak,
            coincidental keyword — the class of bug behind two hit-live
            regressions: "GroundEnemies" (kind=enemy) matched a wood floor
            plank via the shared word "ground", and "StratoPlatform"
            (kind=platform) matched Stairs/steps.fbx via the shared word
            "platform" (one of seven loosely-associated tags on an unrelated
            staircase asset).
            """
            hints = {h.lower() for h in a.get("kind_hints", [])}
            if kind in hints:
                return True
            if tier1 is not None:
                # A real default model already covers this kind — only a
                # catalog pick the library itself categorizes as this kind
                # may replace it; a stray overlap isn't enough to gamble away
                # a known-good fallback (the tier-1 fallback below still
                # fires if this empties out every candidate).
                return False
            if _kind_only_hit(a):
                # No default to fall back on, so only trust a bare kind-word
                # match when the object is actually named after the kind —
                # not merely tagged with it alongside unrelated keywords.
                fname_tokens = {t for t in tokenize(Path(a.get("filename", "")).stem)
                                if len(t) > 1}
                return kind in fname_tokens
            return True

        # ── Tier 2b: global literal match (fallback / concept-layers absent) ──
        candidates = [(s, a) for a in assets
                      if (s := _final_score(a)) >= min_score
                      and _entry_source(a, asset_root) is not None]
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
                if not (broad & layer_band) or _entry_source(a, asset_root) is None:
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

        # ── Tier 4: fallback policy ───────────────────────────────────────────
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
        if not key.startswith("asset:") or m.rig == "sprite":
            continue  # sprites are self-contained PNGs — no FBX texture folder
        for parent in Path(m.src).parents:
            tex = parent / "Textures"
            if tex.is_dir():
                if str(tex) not in seen:
                    seen.add(str(tex))
                    dirs.append(tex)
                break
    return dirs
