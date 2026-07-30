"""
Spatial facts — the numeric quantities the BRIEF states, carried intact past
schema-fill (pipeline Step 3) as a deterministic sidecar.

The bug this closes
-------------------
The climbing brief (prompts/the_wall_that_wakes.txt) says "The summit is a
nerve cluster 900m up". Schema-fill compressed the number away: the schema it
produced carries no height figure anywhere, so `surface_height` found no
evidence, warned correctly, and the layout built a 60 m default wall for a
brief that asked for 900 m. Layout behaved correctly with what reached it —
the number never got there.

That is the second time brief compression became a silent downstream default
(the first: the brief's ENTITIES bullets arriving as rescue placeholders,
closed by src/unity_concept_entities.py). Both have the same shape — the LLM
is the only transport between the brief and the compiler, so anything it
paraphrases away is simply gone. The fix is not to ask the model to try
harder. This pass reads the brief itself, deterministically, BEFORE schema-fill
runs, and the facts ride alongside the schema as provenance: untouched by
schema-fill, by the entity rescue, and by every other LLM step.

What counts as a fact
---------------------
A number carrying a LENGTH UNIT, plus the few tokens of noun phrase around it:

    "The summit is a nerve cluster 900m up"   ->  900.0 m
    "exit at 4000 ft over a 900 m hard deck"  ->  1219.2 m, 900.0 m
    "3 lives", "5 enemies", "resolution 64"   ->  nothing

The unit is the whole gate, and it is not negotiable: without it a brief's
counts, seeds, resolutions and version numbers all read as distances, and the
sidecar becomes noise every consumer has to second-guess. `referent_text` is
the window the number sits in — what it modifies or is modified by — and
`evidence_sentence` is the raw sentence it came from. A consumer matches on
the window; a human reads the sentence.

This module extracts and transports. It does not decide what any number MEANS
for a given primitive — that is one consumption rule, in
src/unity_layout_archetype.py's `_schema_height`, shared by every dimension.

Transport
---------
Facts ride in `schema.meta` (`attach_spatial_facts` / `spatial_facts_of`),
which is the schema's provenance bag rather than its content: nothing in the
schema-fill contract asks the model to write it, every value in it is stamped
by deterministic Python after the model has answered, and it is the only
channel that survives into the generator's model-aware re-layout
(unity_generator._relayout re-runs compute_layout and must resolve the same
height Step 5 did, or the layout stops being idempotent).

`attach_spatial_facts` always writes the key, even for an empty list, so a
model that invents its own `meta.spatial_facts` cannot smuggle one past the
extraction pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# ── Units ────────────────────────────────────────────────────────────────────
# The canonical length-unit table for the whole compiler. Metres are the base;
# src/unity_layout_archetype.py's altitude parser builds its own regex from
# `UNIT_ALTERNATION` so unit spelling and unit VALUE can never drift apart.
_FT_TO_M = 0.3048
UNIT_TO_M: dict[str, float] = {
    "kft": 1000.0 * _FT_TO_M, "km": 1000.0,
    "metres": 1.0, "meters": 1.0, "metre": 1.0, "meter": 1.0, "m": 1.0,
    "feet": _FT_TO_M, "foot": _FT_TO_M, "ft": _FT_TO_M,
}
# Longest first, then alphabetical — deterministic, and it keeps "metres" from
# losing to a bare "m" (regex alternation is leftmost-first).
UNIT_ALTERNATION: str = "|".join(
    sorted(UNIT_TO_M, key=lambda u: (-len(u), u)))

# A number immediately followed by one of those units. The trailing guard
# rejects speeds and compounds — "200 km/h" is a unit followed by "/", and
# "900 mountains" is an "m" followed by a word character, neither of which is
# a distance.
#
# Thousands separators are matched deliberately: a brief writing "4,000 ft"
# would otherwise leave the regex to match the "000" and produce a zero, which
# is silently dropped — a MISSED fact, which is the exact failure this module
# exists to prevent. The grouped alternative has to come first, or the bare
# form matches "4" and stops.
_QUANTITY_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,6}(?:\.\d+)?)\s*"
    r"(" + UNIT_ALTERNATION + r")(?![/\w])")

# Sentence boundaries. A period BETWEEN digits is a decimal point, not the end
# of a sentence — without that guard "4.5 m" splits into "4" and "5 m" and the
# fact comes out ten times too small. Newlines split too: briefs are written as
# bullet lists, whose lines carry no terminator at all.
_SENTENCE_SPLIT_RE = re.compile(r"(?:(?<!\d)[.!?;](?!\d))|[\n\r]+")

# How much noun phrase to keep around the number. Wide enough to reach the
# referent across an intervening clause — "The summit is a nerve cluster 900m
# up" puts five tokens between the two — and narrow enough that a neighbouring
# sentence fragment never lends its vocabulary to an unrelated number.
_REFERENT_TOKENS_BEFORE = 6
_REFERENT_TOKENS_AFTER = 4

# Where the facts ride. Named so callers never spell the key themselves.
META_KEY = "spatial_facts"


@dataclass(frozen=True)
class SpatialFact:
    """One numeric distance the brief stated, normalized to metres.

    `referent_text` is what the consumer matches against — the noun phrase
    window the number sits in. `evidence_sentence` is never matched on; it
    exists so a log line can show a human the sentence the compiler read.
    """
    value_m:           float
    referent_text:     str
    evidence_sentence: str

    def to_dict(self) -> dict[str, Any]:
        return {"value_m": self.value_m, "referent_text": self.referent_text,
                "evidence_sentence": self.evidence_sentence}

    def describe(self) -> str:
        """One-line rendering for `[layout.facts]` logs and layout messages.

        The evidence is dropped when it adds nothing — a short sentence IS its
        own referent window, and printing it twice makes the log read as two
        pieces of evidence when there is one.
        """
        out = f"{self.value_m:g} m — referent {self.referent_text!r}"
        if self.evidence_sentence != self.referent_text:
            out += f" (evidence: {self.evidence_sentence!r})"
        return out


def _sentences(text: str) -> list[str]:
    """The brief split into evidence-sized pieces, whitespace collapsed.

    Collapsing matters because briefs are hard-wrapped: the climbing brief
    breaks "The\\nsummit is a nerve cluster 900m up" across two lines, and an
    evidence sentence with a newline in it is unreadable in a log.
    """
    out: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(text or ""):
        seg = " ".join(raw.split())
        if seg:
            out.append(seg)
    return out


def _referent_window(sentence: str, lo: int, hi: int) -> str:
    """The `[lo, hi)` quantity plus a few whole tokens either side of it."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", sentence)]
    before = [s for s in spans if s[1] <= lo][-_REFERENT_TOKENS_BEFORE:]
    after = [s for s in spans if s[0] >= hi][:_REFERENT_TOKENS_AFTER]
    start = before[0][0] if before else lo
    end = after[-1][1] if after else hi
    return sentence[start:end].strip()


def extract_spatial_facts(text: str) -> list[SpatialFact]:
    """Every numeric distance `text` states, in reading order, deduplicated.

    Pure and deterministic — regex and arithmetic, no model call, no I/O, no
    RNG. Matching is done on a lowercased copy so "900M" reads the same as
    "900m"; the evidence and referent are sliced from the ORIGINAL, which
    lowercasing leaves character-aligned.
    """
    out: list[SpatialFact] = []
    seen: set[tuple[float, str, str]] = set()
    for sentence in _sentences(text):
        lowered = sentence.lower()
        for m in _QUANTITY_RE.finditer(lowered):
            value = float(m.group(1).replace(",", "")) * UNIT_TO_M[m.group(2)]
            if value <= 0.0:
                continue
            fact = SpatialFact(
                value_m=round(value, 3),
                referent_text=_referent_window(sentence, m.start(), m.end()),
                evidence_sentence=sentence,
            )
            key = (fact.value_m, fact.referent_text, fact.evidence_sentence)
            if key not in seen:
                seen.add(key)
                out.append(fact)
    return out


# ── Transport ────────────────────────────────────────────────────────────────

def attach_spatial_facts(schema, facts: Iterable[SpatialFact]) -> int:
    """Stamp `facts` onto the schema's provenance bag; returns how many.

    Always writes the key — an empty list is a statement ("the brief named no
    distances"), and it also overwrites anything an LLM decided to put there.
    Stored as plain dicts so the schema stays JSON-serializable.
    """
    materialized = list(facts)
    if isinstance(getattr(schema, "meta", None), dict):
        schema.meta[META_KEY] = [f.to_dict() for f in materialized]
    return len(materialized)


def spatial_facts_of(schema) -> list[SpatialFact]:
    """The facts riding with this schema, or [] when there are none.

    Tolerant by design: the schema round-trips through JSON on its way to the
    generator's re-layout, and anything malformed is dropped rather than
    raised — a bad sidecar entry must degrade to "no fact", which falls
    through to the named default, not break a build.
    """
    meta = getattr(schema, "meta", None)
    if not isinstance(meta, dict):
        return []
    out: list[SpatialFact] = []
    for raw in meta.get(META_KEY) or []:
        if isinstance(raw, SpatialFact):
            out.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        try:
            value = float(raw["value_m"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(SpatialFact(
            value_m=value,
            referent_text=str(raw.get("referent_text", "")),
            evidence_sentence=str(raw.get("evidence_sentence", "")),
        ))
    return out


# ── Matching ─────────────────────────────────────────────────────────────────

def _hits(text: str, needles: Sequence[str]) -> list[str]:
    """Needles present as whole words. Word-bounded for the same reason
    src/unity_layout_archetype.py's `_hits` is: a bare `in` test makes "top"
    match "topology" and "peak" match "speaking"."""
    lowered = text.lower()
    return [n for n in needles
            if re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])",
                         lowered)]


def match_facts(facts: Sequence[SpatialFact], referents: Sequence[str],
                lo: float, hi: float) -> list[SpatialFact]:
    """Facts whose referent window names one of `referents`, largest first.

    `lo`/`hi` are the consumer's own sanity range — the brief's "2 m ledge"
    and "1 km approach road" are both real distances and neither is a wall
    height. Ties break on the referent text so the order never depends on
    extraction accidents.
    """
    hit = [f for f in facts
           if lo <= f.value_m <= hi and _hits(f.referent_text, referents)]
    return sorted(hit, key=lambda f: (-f.value_m, f.referent_text))


__all__ = [
    "META_KEY", "UNIT_ALTERNATION", "UNIT_TO_M", "SpatialFact",
    "attach_spatial_facts", "extract_spatial_facts", "match_facts",
    "spatial_facts_of",
]
