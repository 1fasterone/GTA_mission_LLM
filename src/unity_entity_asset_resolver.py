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

    entity_model_paths: dict[str, str] = {}
    unmatched: list[tuple[str, str, list[str]]] = []   # (name, kind, near misses)
    defaulted: list[tuple[str, str]] = []
    rotation: dict[tuple[str, ...], int] = {}          # candidate-set → next index

    prop_fallback = all_models.get("prop") or next(iter(all_models.values()), None)

    for e in schema.entities:
        kind = (e.kind or "prop").lower()
        hint = (getattr(e, "model_hint", "") or "").strip().lower()

        # ── Tier 1: explicit kind (schema-default "prop" is not explicit) ────
        if kind in used_models and (kind != "prop" or hint == "default prop"):
            entity_model_paths[e.name] = used_models[kind].resource_path
            continue
        if kind == "prop" and hint == "default prop" and prop_fallback is not None:
            used_models.setdefault("prop", prop_fallback)
            entity_model_paths[e.name] = prop_fallback.resource_path
            continue

        # ── Tier 2: catalog semantic match ────────────────────────────────────
        qw = _query_weights(e, scene_words)

        def _final_score(a: dict) -> float:
            s = _score(a, qw, kind)
            if s > 0.0 and is_2d and a.get("format") == "png":
                s += _SPRITE_2D_BONUS
            return s

        scored = sorted(
            ((s, a) for a in assets
             if (s := _final_score(a)) >= min_score
             and _entry_source(a, asset_root) is not None),
            key=lambda sa: (-sa[0], sa[1]["filename"]))
        if scored:
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
                    # "sprite" rig = 2D texture: no ModelImporter .meta is
                    # authored (Unity's default TextureImporter handles PNG),
                    # and the spawner loads it as Texture2D → Sprite.Create.
                    rig="sprite" if is_sprite else "generic",
                    scale=float(pick.get("scale", 1.0) or 1.0),
                )
                used_models[model_key] = rm
            entity_model_paths[e.name] = rm.resource_path
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
