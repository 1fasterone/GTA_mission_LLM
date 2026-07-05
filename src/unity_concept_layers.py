"""
Concept layers — the semantic classification stage between an entity's intent
and the catalog's filenames.

The old resolver did literal token→filename matching: a strong entity token
that happened to appear in an unrelated asset's keyword list pulled that asset
across domain boundaries ("StratoPlatform" → Stairs/steps.fbx via a stray
"platform" tag; "GroundEnemies" → a wood floor plank via "ground"). Deep
prompts amplified the drift, and when nothing scored the resolver fell back to
the same ~15 default-pack models.

This module inserts a layer in between, exactly as the design asks:

    concept tokens  →  rank concept LAYERS  →  best asset INSIDE the layer

An entity is first classified into one or more concept layers (aircraft,
platform_walkway, creature_animal, …) from its own strong tokens, with its
structured `kind` constraining the choice. The catalog match then runs ONLY
against assets that belong to those layers, so a stray cross-domain keyword can
never win. And because the layer already establishes relevance, a layer with
real assets yields a best-in-layer pick instead of collapsing to the generic
prop — killing the "same 15 defaults" symptom.

Vocabulary is grounded in the generated catalog's tag set
(lookups/unity/concept_layers.json). The file is OPTIONAL: when absent
`load_concept_layers` returns None and the resolver behaves exactly as before
(global literal matching). Deterministic: pure set arithmetic, stable
tie-breaks, no RNG, no LLM.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .unity_asset_catalog import tokenize

# Defaults if concept_layers.json omits _config keys.
_DECISIVE_SCORE = 2.0
_KIND_PRIOR = 1.0           # entity.kind is a structured signal — as strong as
                            # a vocabulary hit when it maps to a layer
_LAYER_BAND = 0.6           # layers within 60% of the top score share the pool


class ConceptLayers:
    """Parsed concept-layer taxonomy with entity→layer and asset→layer logic."""

    __slots__ = ("layers", "decisive_score", "kind_prior", "layer_band")

    def __init__(self, raw: dict):
        cfg = raw.get("_config", {}) or {}
        self.decisive_score = float(cfg.get("decisive_score", _DECISIVE_SCORE))
        self.kind_prior = float(cfg.get("kind_prior", _KIND_PRIOR))
        self.layer_band = float(cfg.get("layer_band", _LAYER_BAND))
        # name -> (vocabulary:set, kinds:set, categories:set)
        self.layers: dict[str, tuple[frozenset, frozenset, frozenset]] = {}
        for name, spec in (raw.get("layers") or {}).items():
            self.layers[name] = (
                frozenset(w.lower() for w in spec.get("vocabulary", [])),
                frozenset(k.lower() for k in spec.get("kinds", [])),
                frozenset(c.lower() for c in spec.get("categories", [])),
            )

    # ── entity → layers ──────────────────────────────────────────────────────
    def classify(self, query_weights: dict[str, float], kind: str,
                 strong_hit: float) -> list[tuple[str, float, int]]:
        """Rank concept layers for one entity.

        Only STRONG query tokens (entity name / kind / model_hint, weight
        ≥ strong_hit) vote — scene-description words never choose a layer.
        Returns [(layer_name, score, vocab_hits)] best-first, empty if none.
        """
        strong = {t for t, w in query_weights.items() if w >= strong_hit}
        kind = (kind or "").lower()
        ranked: list[tuple[str, float, int]] = []
        for name, (vocab, kinds, _cats) in self.layers.items():
            vocab_hits = len(strong & vocab)
            score = float(vocab_hits)
            if kind in kinds:
                score += self.kind_prior
            if score > 0:
                ranked.append((name, score, vocab_hits))
        ranked.sort(key=lambda t: (-t[1], t[0]))
        return ranked

    def kind_layers(self, kind: str) -> frozenset[str]:
        kind = (kind or "").lower()
        return frozenset(n for n, (_v, k, _c) in self.layers.items() if kind in k)

    def project_layers(self, tokens, kinds) -> frozenset[str]:
        """The concept layers a whole PROJECT spans.

        Aggregates over every entity token (names / kinds / model_hints, plus
        salient description nouns): a layer is in the project profile when its
        vocabulary meets any token OR one of the project's entity kinds maps to
        it. Used to theme a project-wide asset shortlist (the KenneyImporter
        grab-bag) toward the domains the game actually contains, instead of
        letting high-generic-score filler dominate.
        """
        toks = {t.lower() for t in tokens}
        knds = {k.lower() for k in kinds}
        out: set[str] = set()
        for name, (vocab, klist, _cats) in self.layers.items():
            if (toks & vocab) or (knds & klist):
                out.add(name)
        return frozenset(out)

    def primary_layer(self, entry: dict, among: frozenset[str],
                      include_kind_hints: bool = True) -> Optional[str]:
        """The single project layer to bucket an asset under (stable pick), or
        None when the asset belongs to no layer in `among`."""
        broad, strong = self.membership(entry, include_kind_hints)
        hit = (strong & among) or (broad & among)
        return min(hit) if hit else None

    def band(self, query_weights: dict[str, float], kind: str,
             strong_hit: float) -> tuple[set[str], int]:
        """The concept-layer pool for one entity + the top layer's vocab hits.

        The entity's structured `kind` constrains the band: when the kind maps
        to any layer, the vocab-ranked band is intersected with those layers
        (so "GroundEnemies" — kind=enemy — is pinned to the enemy/creature
        layers and the adjective "ground" can't pull in terrain). If that
        intersection is empty (explicit naming pointed entirely outside the
        kind's domain) the vocab band wins — deliberate naming outranks kind.
        Returns (band_layer_names, top_vocab_hits).
        """
        ranked = self.classify(query_weights, kind, strong_hit)
        if not ranked:
            return set(), 0
        top = ranked[0][1]
        cutoff = top * self.layer_band
        vocab_band = {n for n, s, _ in ranked if s >= cutoff}
        klayers = self.kind_layers(kind)
        if klayers:
            constrained = vocab_band & klayers
            band = constrained or vocab_band or set(klayers)
        else:
            band = vocab_band
        top_hits = max((h for n, _s, h in ranked if n in band), default=0)
        return band, top_hits

    # ── asset → layers ───────────────────────────────────────────────────────
    def membership(self, entry: dict,
                   include_kind_hints: bool = True
                   ) -> tuple[frozenset[str], frozenset[str]]:
        """(broad, strong) layer membership for one catalog asset.

        strong  — the asset's PRIMARY domain: its category maps to the layer,
                  or (when include_kind_hints) one of its kind_hints does.
        broad   — strong ∪ layers reached only through a keyword overlap. A
                  stray keyword ("platform" on a staircase) makes an asset a
                  broad (weak) member, so it can enter a pool but loses the
                  in-pool alignment bonus to a strong member.

        include_kind_hints=True (per-entity resolver): a coarse kind_hint like
        "vehicle" lets a plane displace the default Car.fbx — the entity's own
        tokens disambiguate WHICH vehicle inside the band. Set it False for
        project-level theming, where there is no per-asset token signal to
        disambiguate and a coarse "vehicle"/"obstacle" hint would wrongly file
        a boat under aircraft or a track-bend under debris; then only category
        + keywords (precise) decide membership.
        """
        cat = str(entry.get("category", "")).lower()
        hints = {h.lower() for h in entry.get("kind_hints", [])}
        kw = {k.lower() for k in entry.get("keywords", [])}
        kw.update(t for t in tokenize(Path(entry.get("filename", "")).stem)
                  if len(t) > 1)
        strong: set[str] = set()
        broad: set[str] = set()
        for name, (vocab, kinds, cats) in self.layers.items():
            if (cat and cat in cats) or (include_kind_hints and (hints & kinds)):
                strong.add(name)
                broad.add(name)
            elif kw & vocab:
                broad.add(name)
        return frozenset(broad), frozenset(strong)


@lru_cache(maxsize=8)
def load_concept_layers(lookups_dir: str = "lookups") -> Optional[ConceptLayers]:
    """Load lookups/unity/concept_layers.json, or None when it is absent.

    Cached by directory. A missing or malformed file returns None so the
    resolver silently reverts to global literal matching (no hard dependency).
    """
    path = Path(lookups_dir) / "unity" / "concept_layers.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    layers = ConceptLayers(raw)
    return layers if layers.layers else None
