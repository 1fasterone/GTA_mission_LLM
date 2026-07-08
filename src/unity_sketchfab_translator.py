"""
Concept-to-Sketchfab-query translator.

Mirrors src/audio/concept_translator.py: after the LLM writes the Step 2
concept + Step 3 schema, this module reuses the same loaded model to give
every EntitySpec a `sketchfab_query` — a short search phrase for the live
Sketchfab Tier S (src/unity_entity_asset_resolver.py) — instead of the
resolver's mechanical "model_hint or tokenized entity name" fallback.

The key difference from the audio cue translator: audio cues can each be
independently "right" for their moment — a subjective match per cue is fine.
Physical models sourced from Sketchfab's open marketplace are each uploaded
by a DIFFERENT independent artist, so translating each entity in isolation
risks a stylistically incoherent scene — a realistic PBR knight next to a
cartoon sword next to a toy-store wall segment ("constructed chaos"). So this
translator derives ONE shared style brief for the whole project first, then
folds it into every entity's query. Paired with author-affinity biasing in
src/sketchfab_freeze.py (once one good model is found, later entities try
that SAME author's catalog first), this is the two-part answer to sourcing a
visually coherent set from independent marketplace uploads rather than a
single generated/curated pack.

Kept SEPARATE from `model_hint`: model_hint drives literal keyword-overlap
matching against Kenney's short curated tags (src/unity_asset_catalog.py) —
an evocative multi-word LLM phrase reused there would dilute that scoring,
not improve it.

Only entities with an empty sketchfab_query are filled. Degrades silently on
any failure — content.py's resolver already falls back to model_hint /
tokenized entity name when sketchfab_query is empty.
"""

from __future__ import annotations

import json
import re


_SYSTEM = """\
You are an art director for a game asset pipeline that sources real 3D models
from a marketplace of independent, unrelated freelance artists, searched by
LITERAL KEYWORD MATCH — not semantic/AI search. Every extra word narrows the
results, and an abstract or mood word ("weathered", "muted palette", "grim
atmosphere") that nobody actually tags their upload with returns ZERO results.
Your job is to prevent visual chaos (every model picked independently should
still read as one art direction) WITHOUT starving the search into emptiness.

Given a game concept and a list of entities (physical objects/characters that
each need a real 3D model), do TWO things:
  1. Write ONE shared "style" tag: 1-2 words, a genre/style LABEL real
     uploaders actually use — "low poly", "stylized", "realistic", "cartoon",
     "sci-fi", "cyberpunk", "medieval", "fantasy", "voxel", "steampunk". NOT a
     sentence, NOT a mood/color/material description, NO commas.
  2. For each entity, write a plain marketplace-search query: 1-3 concrete
     words naming ONLY the object itself — "knight", "castle wall", "wooden
     barrel", "longsword". No adjectives beyond what's essential to
     disambiguate the object. Do not repeat the style tag inside it.
Rules:
  - style tag + entity query together must total 4 words or fewer — that is
    the empirically-verified point past which real searches return nothing.
  - Never invent a hyper-specific franchise/IP style, and never name an artist.
  - One entity, one query. No lists, no explanation, no prose around the JSON.
Output ONLY a valid JSON object:
{"style": "...", "entities": {"<entity name>": "<query>", ...}}
"""

_USER_TMPL = """\
GAME CONCEPT:
{concept}

ENTITIES (name | kind | existing model_hint):
{entity_table}

Fill every entity. Output JSON only.
"""


def _build_table(entities: list) -> str:
    rows = []
    for e in entities:
        g = (lambda k, d: e.get(k, d)) if isinstance(e, dict) \
            else (lambda k, d: getattr(e, k, d))
        rows.append(
            f"  {g('name', '?')} | {g('kind', 'prop')} | "
            f"{g('model_hint', '') or '(none)'}"
        )
    return "\n".join(rows)


def translate_concept(concept_text: str, entities: list, llm, seed: int = 0) -> int:
    """Enrich each entity's sketchfab_query in-place using the project concept
    plus one shared style brief. Returns the number of entities updated.

    Silently degrades on any failure so the pipeline always continues —
    unity_entity_asset_resolver.py's mechanical fallback (model_hint /
    tokenized entity name) still applies when sketchfab_query is empty.
    """
    if not concept_text or not entities:
        return 0

    try:
        raw = llm.chat(
            system=_SYSTEM,
            user=_USER_TMPL.format(
                concept=concept_text.strip(),
                entity_table=_build_table(entities),
            ),
            temperature=0.7,
            thinking=False,
            seed=seed or None,
        )
    except Exception:  # noqa: BLE001
        return 0

    try:
        # Strip think-blocks and markdown fences before parsing.
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return 0
        payload = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001
        return 0

    if not isinstance(payload, dict):
        return 0
    style = str(payload.get("style") or "").strip()
    mapping = payload.get("entities")
    if not isinstance(mapping, dict):
        return 0

    updated = 0
    for e in entities:
        is_dict = isinstance(e, dict)
        name = e.get("name", "") if is_dict else getattr(e, "name", "")
        if name not in mapping:
            continue
        query = str(mapping[name]).strip()
        if not query:
            continue
        current = e.get("sketchfab_query", "") if is_dict else getattr(e, "sketchfab_query", "")
        if current:
            continue
        full_query = f"{style} {query}".strip() if style else query
        try:
            if is_dict:
                e["sketchfab_query"] = full_query
            else:
                e.sketchfab_query = full_query
            updated += 1
        except Exception:  # noqa: BLE001
            pass

    return updated
