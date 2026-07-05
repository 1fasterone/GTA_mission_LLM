"""
Kenney asset resolver — keyword-overlap matching (no embeddings needed).
Reads lookups/unity/kenney_catalog.json and scores assets against the schema.
Same architecture as unity_resolver.py: local JSON lookup, word-overlap scoring.

The project-wide asset shortlist (feeding the KenneyImporter editor grab-bag)
is themed by the same CONCEPT LAYERS the per-entity resolver uses: the schema
is classified into the concept domains it spans, on-domain assets get a bonus,
pure off-theme generic filler is dropped, and the shortlist is spread across
the project's concepts so it mirrors the game instead of surfacing whatever
scores highest on commonness (the "steps.fbx for a skydiving game" symptom).
Concept layers are optional — with no concept_layers.json the scoring reverts
to the original keyword+generic behavior verbatim.
"""

import json
from collections import defaultdict
from math import ceil
from pathlib import Path
from typing import Optional

from rich.console import Console

from .unity_asset_catalog import tokenize
from .unity_concept_layers import load_concept_layers
from .unity_schema import UnitySchema

console = Console()

# Concept-layer scoring, on the same 0.0–~1.5 scale as keyword+generic score.
_LAYER_STRONG_BONUS = 0.6   # asset's category/kind_hint places it in a project concept
_LAYER_BROAD_BONUS = 0.2    # asset only keyword-touches a project concept

# ── Game type → relevant contexts ─────────────────────────────────────────────

_GAME_TYPE_CONTEXTS: dict[str, set[str]] = {
    "2D":        set(),                                     # skip 3D assets
    "2.5D":      {"platformer", "action", "any"},
    "3D":        {"action", "platformer", "racing", "any"},
    "VR":        {"action", "platformer", "any"},
    "top_down":  {"action", "platformer", "racing", "any"},
    "isometric": {"action", "platformer", "puzzle", "any"},
}

# Visual style → boost for kenney assets (all kenney packs are low-poly/colorful)
_STYLE_BOOST: dict[str, float] = {
    "cartoon":   1.3,
    "low_poly":  1.5,   # kenney assets ARE low-poly — perfect match
    "stylized":  1.2,
    "pixel_art": 0.8,   # kenney 3D less relevant for pixel art
    "realistic": 0.6,   # kenney low-poly conflicts with realistic style
    "retro":     1.1,
}


def _load_catalog(lookups_dir: str) -> dict:
    # Prefer the generated full-library catalog (unity-catalog CLI); fall back
    # to the hand-curated kenney catalog so old checkouts keep working.
    for name in ("asset_catalog.json", "kenney_catalog.json"):
        path = Path(lookups_dir) / "unity" / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    return {}


def _score_asset(asset: dict, query_words: set[str],
                 valid_contexts: set[str]) -> tuple[float, float, bool]:
    """Relevance components for a single asset against the query.

    Returns (keyword_score, generic_bonus, context_ok). Split out so callers
    can tell a real keyword match from a pure generic-score tiebreak — the
    concept-layer pass needs that distinction to drop off-theme filler.
    """
    if not valid_contexts:
        return 0.0, 0.0, False

    # Context filter: skip assets with no matching game context
    asset_contexts = set(asset.get("game_contexts", []))
    if not (asset_contexts & valid_contexts):
        return 0.0, 0.0, False

    # Keyword overlap
    asset_words = set(kw.lower() for kw in asset.get("keywords", []))
    overlap     = len(query_words & asset_words)
    kw_score    = overlap / max(len(asset_words), 1)

    # Generic score (0–10) as a tiebreaker (normalised to 0.0–0.2)
    generic_bonus = asset.get("generic_score", 0) / 50.0

    return kw_score, generic_bonus, True


def _project_tokens_and_kinds(schema: UnitySchema) -> tuple[set[str], set[str]]:
    """Strong project tokens (entity names + model_hints + salient description
    words) and the set of entity kinds — the raw material for classifying the
    project into concept layers."""
    tokens: set[str] = set()
    kinds: set[str] = set()
    for e in schema.entities:
        tokens.update(tokenize(e.name))
        tokens.update(tokenize(getattr(e, "model_hint", "") or ""))
        if e.kind:
            kinds.add(e.kind.lower())
    tokens.update(tokenize(schema.description or ""))
    return tokens, kinds


def resolve_assets(schema: UnitySchema,
                   lookups_dir: str = "lookups",
                   source_dir: Optional[str] = None,
                   max_assets: int = 15) -> list[dict]:
    """
    Score all catalog assets against the schema and return the top N.

    Each returned dict has:
      filename, pack, category, description, score, source_path
    """
    catalog = _load_catalog(lookups_dir)
    if not catalog:
        console.print("  [yellow]WARN[/] kenney_catalog.json not found — skipping asset resolution")
        return []

    # Skip 2D projects — all kenney packs are 3D
    if schema.game_type == "2D":
        return []

    cfg            = catalog.get("_resolver_config", {})
    max_assets     = min(max_assets, cfg.get("max_assets_per_project", 15))
    prefer_fmt     = cfg.get("prefer_format", "FBX format")
    src_hint       = source_dir or cfg.get("source_path_hint", "Models")

    valid_contexts = _GAME_TYPE_CONTEXTS.get(schema.game_type, {"any"})
    style_boost    = _STYLE_BOOST.get(schema.visual_style, 1.0)
    generic_thresh = cfg.get("generic_score_threshold", 7)

    # Build query words from schema description + script descriptions + features
    raw_text = " ".join([
        schema.description,
        schema.game_type,
        schema.visual_style,
        *[s.description for s in schema.scripts],
        *schema.features,
    ])
    query_words = set(raw_text.lower().split())

    # Concept-layer profile for the whole project. `project_layers` is the set
    # of concept domains this game spans; assets are then themed toward it.
    # Absent concept_layers.json → `layers is None`, every branch below is a
    # no-op, and scoring is byte-identical to the original keyword+generic path.
    layers = load_concept_layers(lookups_dir)
    project_layers: frozenset[str] = frozenset()
    if layers:
        p_tokens, p_kinds = _project_tokens_and_kinds(schema)
        project_layers = layers.project_layers(p_tokens, p_kinds)

    # Score every asset (3D models only — 2D sprites are the entity resolver's
    # business and would produce bogus KenneyImporter source paths here).
    scored: list[tuple[float, Optional[str], dict]] = []
    for asset in catalog.get("assets", []):
        if asset.get("format", "fbx") != "fbx":
            continue
        kw_score, generic_bonus, ctx_ok = _score_asset(
            asset, query_words, valid_contexts)
        if not ctx_ok:
            continue
        score = (kw_score + generic_bonus) * style_boost

        primary = None
        on_theme = False
        if layers and project_layers:
            # Precise (category + keyword) membership — NOT coarse kind_hints,
            # which would file a boat under aircraft in a project-wide sweep.
            broad, strong = layers.membership(asset, include_kind_hints=False)
            if strong & project_layers:
                score += _LAYER_STRONG_BONUS
                on_theme = True
            elif broad & project_layers:
                score += _LAYER_BROAD_BONUS
                on_theme = True
            primary = layers.primary_layer(asset, project_layers,
                                           include_kind_hints=False)

        # Universal high-generic assets (floor/box/platform) get a baseline
        # score even with no keyword hit — but only when the game actually has
        # a matching concept (or when concept layers are off). This is what
        # keeps a skydiving game's grab-bag from filling up with staircases.
        if score == 0.0 and asset.get("generic_score", 0) >= generic_thresh:
            if not project_layers or on_theme:
                score = asset["generic_score"] / 100.0

        # With a concept profile, the shortlist IS the project's concept
        # layers: an asset that belongs to no project concept is dropped even
        # if the noisy description bag gave it a stray keyword hit. That noise
        # is exactly what surfaced steps.fbx for a skydiving game; concept
        # membership, not incidental word overlap, decides inclusion here.
        if project_layers and not on_theme:
            continue
        if score <= 0.0:
            continue

        asset_with_meta = {**asset,
                           "score":       round(score, 4),
                           "source_path": str(Path(src_hint) / prefer_fmt / asset["filename"])}
        scored.append((score, primary, asset_with_meta))

    scored.sort(key=lambda x: (-x[0], x[2]["filename"]))

    # Dedupe by filename — the same model can appear under several packs/roots
    # (bend.fbx in marble_tracks and skate_kit); keep the highest-scoring one.
    seen: set[str] = set()
    deduped: list[tuple[float, Optional[str], dict]] = []
    for row in scored:
        fname = row[2]["filename"]
        if fname in seen:
            continue
        seen.add(fname)
        deduped.append(row)

    if project_layers:
        # Spread the shortlist across the project's concepts: cap picks per
        # concept layer so one dominant domain can't crowd out the rest, then
        # backfill from the deferred remainder to still reach max_assets.
        per_layer_cap = max(2, ceil(max_assets / max(1, len(project_layers))))
        counts: dict[str, int] = defaultdict(int)
        results: list[dict] = []
        deferred: list[dict] = []
        for _score, primary, meta in deduped:
            if len(results) >= max_assets:
                break
            if primary is not None and counts[primary] >= per_layer_cap:
                deferred.append(meta)
                continue
            results.append(meta)
            if primary is not None:
                counts[primary] += 1
        for meta in deferred:
            if len(results) >= max_assets:
                break
            results.append(meta)
    else:
        results = [meta for _s, _p, meta in deduped[:max_assets]]

    if results:
        console.print(f"  [green]Assets[/] resolved {len(results)} Kenney assets "
                      f"(top: {results[0]['filename']})")
    return results
