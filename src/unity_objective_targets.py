"""
Objective targets — the content an objective is about, recovered from its own words.

WHY THIS EXISTS
---------------
`MissionObjective.target_ref` is the field that names the content an objective is
about, and four different consumers read it:

    micro/board.board_domain_for_missions -> commands
        the verbs the parser answers to, and `meta.objective_commands` with them
    micro/board.board_domain_for_missions -> requires
        the order the places come in
    unity_compose SEAM 8a
        WHICH board event advances WHICH objective
    unity_playability._check_objective_targets
        whether the goal is content that exists in the build

Step 3 is asked for it — `src/unity_prompts.py` spells the field out as
`"target_ref":"<entity group name or character id>"` — and routinely leaves it
blank. Blank is not a small loss. With no ref, SEAM 8a binds NOTHING, and the
generic sources underneath it bind exactly ONE objective per event source by
design (a trigger volume cannot tell which objective its event was about). A
six-objective board then ships with two advanceable steps and a mission that can
never complete.

Live incident (RepoQuest: Enterprise Edition, 2026-08-15): six objectives, six
blank refs, 69/100 NOT PLAYABLE — 3 of 6 required objectives with no event
source, the board composed with no verbs and no ordering rule, and every one of
the four consumers above reading an empty string.

The objective's own description names the content in the player's own words:
"Align the red conflict markers" is about `ConflictBlocks`. This resolves that
reference deterministically, and only ever to an entity the schema ALREADY
declares — nothing here invents a group, and nothing here is a second author.

HOW IT DECIDES
--------------
One score per candidate entity, and the unique best scorer wins:

    +1 per shared word    the description and the entity name agree on a noun.
                          Singularised and short words dropped, so "conflicts"
                          meets "ConflictBlocks" and a stray "of" pairs nothing
                          (micro/board._id_tokens, the same rule the board
                          itself matches names with).

    +2 grammatical role   English marks a destination with a locative
                          preposition — "place a build artifact ON THE
                          conveyor" — so a word after one is evidence for a
                          PLACE, and a word before one is evidence for the thing
                          carried. Awarded only when the role and the entity's
                          board side agree.

    +1 what the type needs   a `collect` objective advances on a piece being
                          placed and a `reach` objective on a place being
                          filled (unity_compose._board_objective_binding), so
                          the side the type will look for is worth a point.

The two side-aware terms are scored only for a project that actually has a board
(`locomotion == 'none'`); everywhere else the score is the shared-word count
alone. The split is read from `board_options_for_entities` — the same function
that will classify the cast at compose time, so this cannot prefer a side the
board then disagrees about.

A TIE ABSTAINS, naming both candidates. "Select at least 5 nodes" against a cast
holding both `GraphNodes` and `EmergencyNodes` is genuinely ambiguous, and a ref
guessed from an ambiguous sentence is the silent wrong answer this module exists
to stop: it would claim the objective away from SEAM 8's generic sources — which
can still honestly advance it — and bind it to the wrong group forever.

Objectives that already carry a ref are never touched. Self-driven types
(`survive`) are skipped: the tracker runs its own clock for them, and a ref would
let SEAM 8a bind a second driver on top of it.

DETERMINISM
-----------
No LLM, no network, no clock, no randomness — Step 5, same contract as the rest
of Steps 4-7. Same schema in, same refs out.
"""

from __future__ import annotations

import re

from .micro.board import (_DESTINATION_PREPS, _VERB_SKIP, _id_tokens,
                          board_options_for_entities)
from .unity_schema import UnitySchema

# What each objective TYPE will look for once SEAM 8a reads the ref. Both
# vocabularies are listed on purpose: the IR writes `collect`/`reach`
# (unity_schema.MissionObjType) and the composed micro writes `interact` for the
# same idea (micro/objectives.ObjectiveEntry._clamp_type).
_TYPE_WANTS: dict[str, str] = {
    "collect":  "piece",
    "interact": "piece",
    "reach":    "zone",
}

_ROLE_BONUS = 2
_TYPE_BONUS = 1


def _prose_tokens(text: str) -> set[str]:
    """The word set of a description, in the same shape `_id_tokens` gives a
    name — so "Resolve all conflicts" and "ConflictBlocks" meet on `conflict`.

    Grammar words are dropped as well as short ones: without that, "all" and
    "the" pair an objective with any entity unlucky enough to be named after
    them."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", str(text or "").lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            for w in words if len(w) >= 3 and w not in _VERB_SKIP}


def _halves(description: str) -> tuple[str, str]:
    """`(carried, destination)` — the sentence split at its locative
    preposition. "Place a build artifact on the conveyor" -> ("place a build
    artifact", "conveyor"). No preposition means nothing arrived anywhere, so
    the whole sentence is the carried half."""
    text = " " + " ".join(str(description or "").lower().split()) + " "
    for prep in _DESTINATION_PREPS:
        if prep in text:
            head, tail = text.split(prep, 1)
            return head, tail
    return text, ""


def _board_sides(schema: UnitySchema, missions: list) -> dict[str, str]:
    """`{entity name: 'piece'|'zone'}` for a board game, empty otherwise.

    Advisory only — it weights a tie-break, it never decides the ref by itself.
    Read from the function compose will use, with the same mission list, so the
    side this prefers and the side the board builds are one answer."""
    if str(getattr(schema, "locomotion", "") or "").strip().lower() != "none":
        return {}
    opts = board_options_for_entities(schema.entities or [], mission=missions)
    if not opts:
        return {}
    sides = {p["id"]: "piece" for p in (opts.get("pieces") or []) if p.get("id")}
    sides.update({z["id"]: "zone" for z in (opts.get("zones") or []) if z.get("id")})
    return sides


def resolve_objective_targets(schema: UnitySchema) -> list[str]:
    """Fill every blank `target_ref` the objective's own sentence answers.

    Mutates `schema.missions` in place. Returns the messages for the caller to
    print — no rich markup, so this stays importable from a test."""
    # Lazy, and for the same reason unity_pipeline imports it lazily: the
    # composer pulls in the generator, and Step 5 should not pay for that just
    # to read one frozenset. One source of truth beats a copy that drifts.
    from .unity_compose import _SELF_DRIVEN_OBJECTIVE_TYPES

    out: list[str] = []
    missions = [m for m in (schema.missions or []) if m.objectives]
    if not missions:
        return out

    # Entities only. `_check_objective_targets` grades a ref against
    # schema.entities, so a character id — which the Step 3 prompt does offer —
    # would resolve here and then be graded as content missing from the build.
    names = [n for n in (str(getattr(e, "name", "") or "").strip()
                         for e in (schema.entities or [])) if n]
    if not names:
        return out

    # Every mission, the same list compose classifies the cast from.
    sides = _board_sides(schema, missions)
    unmatched: list[str] = []

    for mission in missions:
        for obj in mission.objectives:
            if str(obj.target_ref or "").strip():
                continue
            if obj.type in _SELF_DRIVEN_OBJECTIVE_TYPES:
                continue

            head, tail = _halves(obj.description)
            head_toks, tail_toks = _prose_tokens(head), _prose_tokens(tail)
            said = head_toks | tail_toks
            wants = _TYPE_WANTS.get(str(obj.type or "").lower(), "")

            scored: list[tuple[int, str]] = []
            for name in names:
                toks = _id_tokens(name)
                shared = toks & said
                if not shared:
                    continue
                side = sides.get(name, "")
                score = len(shared)
                if side == "zone" and (toks & tail_toks):
                    score += _ROLE_BONUS
                elif side == "piece" and (toks & head_toks):
                    score += _ROLE_BONUS
                if side and side == wants:
                    score += _TYPE_BONUS
                scored.append((score, name))

            if not scored:
                unmatched.append(f"{obj.id} ({obj.type})")
                continue

            best = max(score for score, _n in scored)
            top = sorted(n for score, n in scored if score == best)
            if len(top) > 1:
                out.append(
                    f"objective targets: {obj.id} names nothing one entity "
                    f"answers to — {' and '.join(top)} score the same on "
                    f"{obj.description.strip()!r}; left blank rather than bound "
                    "to the wrong one")
                continue

            obj.target_ref = top[0]
            out.append(f"objective targets: {obj.id} ({obj.type}) -> {top[0]} "
                       f"— from its own description {obj.description.strip()!r}")

    if unmatched:
        out.append("objective targets: " + ", ".join(unmatched)
                   + " name no entity in the build, so no ref was derived — "
                     "these can only advance through a generic SEAM 8 source")
    return out
