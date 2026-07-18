"""
Concept-named entity rescue — restores the brief's own "ENTITIES" bullets
when the Step-3 schema-fill LLM silently drops them.

Hit live on ToyRacerArcade: the concept explicitly named 6 gameplay entities
(PlayerCar, 3 rival cars, Coin, BoostPad), but the schema-fill LLM wrote 5
*different* generic scene-dressing groups (TrackSegments, BarrierWall,
CheckpointBanner, SpectatorCrowd, PitBuilding) and kept only PlayerCar —
losing every AI opponent and pickup the design actually needed. Playability
was graded 0/10 as a direct result: no rivals to race, no coins, no boost
mechanic.

Mirrors unity_racetrack.compose_racetrack's "trust non-degenerate LLM output,
rescue degenerate output" contract, but keyed on named-entity coverage
instead of kind coverage: it never touches a schema that already kept every
concept-named entity, and only APPENDS the ones that went missing — it never
mutates or removes what the LLM wrote. Deterministic regex parse of the
ORIGINAL user_prompt (schema.meta['user_prompt']), not the Step-2 LLM concept
prose, which already paraphrases specific names away.
"""

from __future__ import annotations

import re
from typing import TypedDict

from .unity_asset_catalog import tokenize
from .unity_entity_asset_resolver import _STOPWORDS
from .unity_schema import EntitySpec


class _KindDefaults(TypedDict):
    shape: str
    placement: str
    count: int
    scale: float
    tag: str
    color_role: str

# A line that is ALL-CAPS (allowing digits/spaces/&/'/- ) marks the next
# section header in the structured-brief format (see unity_pipeline.py's
# _BRIEF_MARKER_RE) — used to know where the ENTITIES bullet list ends.
_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 /&'-]{1,40}$")
_BULLET_RE = re.compile(r"^([A-Za-z][\w'-]*)\s*:\s*(.+)$")
_KIND_PAREN_RE = re.compile(r"\(kind:\s*([^)]+)\)", re.IGNORECASE)

# Deterministic per-kind defaults for a rescued entity. Anything not listed
# falls back to _DEFAULT_KIND — a single centered placeholder, the same
# conservative default EntitySpec itself uses.
_KIND_DEFAULTS: dict[str, _KindDefaults] = {
    "collectible": dict(shape="sphere", placement="scatter", count=12,
                        scale=0.5, tag="Collectible", color_role="accent"),
    "prop":        dict(shape="cube",   placement="scatter", count=6,
                        scale=1.0, tag="Untagged", color_role="accent"),
    "vehicle":     dict(shape="cube",   placement="single",  count=1,
                        scale=1.0, tag="Untagged", color_role="secondary"),
    "obstacle":    dict(shape="cylinder", placement="scatter", count=8,
                        scale=0.6, tag="Untagged", color_role="accent"),
}
_DEFAULT_KIND: _KindDefaults = dict(shape="cube", placement="single", count=1,
                     scale=1.0, tag="Untagged", color_role="secondary")

_RIVAL_WORDS_RE = re.compile(r"\b(rival|enemy|opponent|ai)\b", re.IGNORECASE)


def extract_named_entities(user_prompt: str) -> list[dict]:
    """Parse the brief's own "ENTITIES" section: bullets of the form
    "- Name: description (kind: x / y)." Returns
    [{"name", "description", "kind"}, ...] in source order; [] if the brief
    has no recognizable ENTITIES section (e.g. a one-line vibe prompt).
    """
    lines = (user_prompt or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper().rstrip(":") == "ENTITIES":
            start = i + 1
            break
    if start is None:
        return []

    out: list[dict] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("-"):
            m = _BULLET_RE.match(stripped.lstrip("-").strip())
            if not m:
                continue
            name, desc = m.group(1), m.group(2)
            kind = ""
            kind_m = _KIND_PAREN_RE.search(desc)
            if kind_m:
                kind = kind_m.group(1).split("/")[0].strip().lower()
                desc = _KIND_PAREN_RE.sub("", desc).strip().rstrip(".").strip()
            out.append({"name": name, "description": desc, "kind": kind})
        elif _SECTION_HEADER_RE.match(stripped):
            break  # next section header — the ENTITIES list is over
        # else: a wrapped continuation sentence under the last bullet — skip.
    return out


def _model_hint(name: str, kind: str, description: str) -> str:
    words = [t for t in tokenize(name) if t not in _STOPWORDS]
    if not words:
        words = [t for t in tokenize(description) if t not in _STOPWORDS][:3]
    if kind and kind != "prop" and kind not in words:
        words.append(kind)
    return " ".join(words[:4]) or (kind or "prop")


def inject_missing_concept_entities(schema) -> list[str]:
    """Append an EntitySpec for every brief-named entity the schema-fill
    step dropped. Mutates schema.entities in place (append-only) and returns
    fixer log messages in the same style as other schema fixers.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    named = extract_named_entities(meta.get("user_prompt", ""))
    if not named:
        return []

    existing = {e.name.strip().lower() for e in schema.entities}
    added: list[EntitySpec] = []
    for ent in named:
        key = ent["name"].strip().lower()
        if not key or key in existing:
            continue
        kind = ent["kind"] or "prop"
        defaults = _KIND_DEFAULTS.get(kind, _DEFAULT_KIND)
        tag = defaults["tag"]
        if kind == "vehicle" and _RIVAL_WORDS_RE.search(
                f"{ent['name']} {ent['description']}"):
            tag = "Enemy"
        added.append(EntitySpec(
            name=ent["name"], kind=kind, count=defaults["count"],
            shape=defaults["shape"], placement=defaults["placement"],
            color_role=defaults["color_role"], scale=defaults["scale"],
            tag=tag, model_hint=_model_hint(ent["name"], kind, ent["description"]),
        ))
        existing.add(key)

    if not added:
        return []
    schema.entities = list(schema.entities) + added
    return [
        f"Concept named {len(added)} entit"
        f"{'y' if len(added) == 1 else 'ies'} the schema-fill dropped "
        f"({', '.join(e.name for e in added)}) — restored from the brief's "
        "own ENTITIES section instead of leaving them out of the scene."
    ]
