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

# Resources.Load root for catalog picks. Distinct from the default pack's
# "Models/Default/" so the two never collide; 3 path parts keeps write()'s
# secondary-copy prefix strip (Assets/Models/<Category>/<file>) working.
_RES_ROOT = "Models/Kenney"

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


def _score(entry: dict, query_weights: dict[str, float], kind: str) -> float:
    """Weighted keyword overlap + kind-hint bonus + generic tiebreak.

    Returns 0.0 unless at least one keyword actually matches — a kind hint or
    the generic bonus alone must never clear the match threshold, or bare
    default-kind entities with meaningless names would grab arbitrary assets.
    """
    kw = set(k.lower() for k in entry.get("keywords", []))
    kw.update(tokenize(Path(entry.get("filename", "")).stem))
    hit_weight = sum(w for t, w in query_weights.items() if t in kw)
    if hit_weight == 0.0:
        return 0.0
    score = hit_weight / max(1.0, len(kw)) ** 0.5
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
        if t not in _STOPWORDS:
            weights[t] = 1.0
    kind = (entity.kind or "").lower()
    if kind and kind not in _STOPWORDS:
        weights[kind] = max(weights.get(kind, 0.0), 1.0)
    for t in tokenize(getattr(entity, "model_hint", "") or ""):
        if t not in _STOPWORDS:
            weights[t] = 2.0
    return weights


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

    catalog = load_catalog(str(lookups_dir))
    assets = [a for a in catalog.get("assets", []) if a.get("filename")]

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
        scored = sorted(
            ((s, a) for a in assets
             if (s := _score(a, qw, kind)) >= min_score
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
            dest_rel = f"{_RES_ROOT}/{category}/{pick['filename']}"
            model_key = f"asset:{Path(pick['filename']).stem}"
            rm = used_models.get(model_key)
            if rm is None:
                rm = ResolvedModel(
                    kind=model_key,
                    src=str(src.resolve()),
                    dest_rel=dest_rel,
                    resource_path=dest_rel.rsplit(".", 1)[0],
                    rig="generic",
                    scale=float(pick.get("scale", 1.0) or 1.0),
                )
                used_models[model_key] = rm
            entity_model_paths[e.name] = rm.resource_path
            continue

        # ── Tier 3: fallback policy ───────────────────────────────────────────
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
            "map its kind in lookups/unity/default_models.json, or set "
            "assets.strict: false."
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
        if not key.startswith("asset:"):
            continue
        for parent in Path(m.src).parents:
            tex = parent / "Textures"
            if tex.is_dir():
                if str(tex) not in seen:
                    seen.add(str(tex))
                    dirs.append(tex)
                break
    return dirs
