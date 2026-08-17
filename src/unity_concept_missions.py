"""
Brief-declared mission rescue — restores the brief's own "MISSIONS" section
when the Step-3 schema-fill LLM collapses it.

WHY THIS EXISTS
---------------
`inject_missing_concept_entities` exists because schema-fill drops the cast.
This is its sibling, for the same failure one level up: schema-fill collapses
the PROGRESSION. There was no way for a brief to insist on it, because the
structured-brief format had no MISSIONS section at all — SCENES, ENTITIES,
SCRIPTS, NARRATIVE and AUDIO were all declarable and the thing the player
actually does was not.

Measured, three consecutive live runs of one lost-property brief that lays out
three ordered phases in prose and in a four-beat NARRATIVE section:

    run 1 (natural language)  1 mission, 2 objectives
    run 2 (natural language)  1 mission, 1 objective
    run 3 (structured)        1 mission, 3 objectives

Every one of them a single mission. The composer reads missions plural
everywhere it matters — `steps_from_missions` states every phase as a step,
`board_domain_for_missions` derives verbs and ordering across all of them, and
SEAM 9 sums one readout over every tracker — and none of it could ever be
reached from a brief, because Step 3 never emitted more than one mission to
span.

This is a RESTORE, not an invention: every mission, title, objective, type,
target and count comes from the brief's own bytes. Append-only, exactly like
the entity rescue — a mission the LLM already wrote is never touched, and an
id it already used is never overwritten.

THE FORMAT
----------
A numbered mission line, then its objectives as bullets underneath:

    MISSIONS
    1. Clear the Counter — every lost thing goes on the shelf that matches it.
       - Shelve the umbrellas on the umbrella shelf (collect: LostUmbrellas x5)
       - Shelve the suitcases on the suitcase shelf (collect: LostSuitcases x3)
    2. Book the Night In — write down what you shelved.
       - Record each kind in the claim log (interact: ClaimLog)

The `(...)` annotation is OPTIONAL and every part of it is optional in turn:
`(collect)`, `(collect: LostUmbrellas)` and `(collect: LostUmbrellas x5)` are
all valid, and a bullet with no annotation at all is valid too. A bare bullet
gets `reach` (MissionObjType's own default) and an empty `target_ref`, which
`unity_objective_targets` then resolves from the sentence — the brief says what
to do, the compiler works out what it is about.

DETERMINISM
-----------
Regex over `schema.meta['user_prompt']` — the ORIGINAL brief, not the Step-2
concept, which paraphrases the specific names away. No LLM, no network, no
clock. Same brief in, same missions out.
"""

from __future__ import annotations

import re

from .unity_schema import MissionObjective, MissionSpec

# Where the MISSIONS section ends: the next ALL-CAPS header line, same rule the
# entity rescue slices ENTITIES with (unity_concept_entities._SECTION_HEADER_RE).
_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 /&'-]{1,40}$")
_MISSIONS_HEADER_RE = re.compile(r"^MISSIONS\b.*$", re.IGNORECASE)

# "1. Clear the Counter — every lost thing goes on the shelf that matches it."
_MISSION_RE = re.compile(r"^(\d{1,2})[.)]\s+(.+)$")
# The em/en dash (or " - ") splitting a mission's title from its description —
# the same split `unity_briefparse._DASH_SPLIT_RE` uses on a SCENES entry.
_DASH_SPLIT_RE = re.compile(r"\s+[—–]\s+|\s+-\s+")
# "- Shelve the umbrellas on the umbrella shelf (collect: LostUmbrellas x5)"
_OBJECTIVE_RE = re.compile(r"^[-*•]\s+(.+)$")
# The optional trailing annotation, and only when it TRAILS: a parenthetical
# mid-sentence is prose the author wrote, not a declaration.
_ANNOTATION_RE = re.compile(
    r"\(\s*([A-Za-z]+)\s*(?::\s*([A-Za-z][\w'-]*)\s*)?(?:[xX×]\s*(\d{1,4})\s*)?\)\s*$")

# MissionObjType (unity_schema._VALID_MISSION_OBJ). Listed here rather than
# imported so an annotation naming something outside the IR's vocabulary is
# REPORTED, not silently clamped to "reach" by the field validator — the author
# wrote a word the compiler does not have, and should be told which.
_VALID_TYPES: frozenset[str] = frozenset({
    "reach", "collect", "defeat", "survive", "escort", "timer",
})
_MAX_MISSIONS = 8
_MAX_OBJECTIVES = 5          # matches the Step-3 prompt's own per-mission cap
_MAX_COUNT = 999


def _slug(text: str, prefix: str) -> str:
    """A stable lower_snake id from the objective's own words."""
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return (f"{prefix}_{s}" if s else prefix)[:48]


def _fold(section: str) -> list[str]:
    """Rejoin the brief's ~80-column wrap so a bullet is one logical line.

    A continuation is any line that does not itself start a mission or a
    bullet — without folding, an objective whose annotation wrapped onto the
    next line loses it, which is exactly how a declared target_ref would go
    missing while looking fine in the file.
    """
    out: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _MISSION_RE.match(line) or _OBJECTIVE_RE.match(line) or not out:
            out.append(line)
        else:
            out[-1] = out[-1] + " " + line
    return out


def extract_declared_missions(prompt: str) -> list[dict]:
    """The brief's MISSIONS section as `[{title, description, objectives}]`.

    Empty when the brief declares no MISSIONS section, which is the ordinary
    case — a brief that does not state its progression is not broken, it just
    leaves the whole question to Step 3.
    """
    lines = (prompt or "").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if _MISSIONS_HEADER_RE.match(ln.strip())), None)
    if start is None:
        return []
    body: list[str] = []
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        # An ALL-CAPS line ends the section — but only when it is a header on
        # its own, not a shouted word inside a sentence.
        if stripped and _SECTION_HEADER_RE.match(stripped) and ln == ln.lstrip():
            break
        body.append(ln)

    missions: list[dict] = []
    for line in _fold("\n".join(body)):
        m = _MISSION_RE.match(line)
        if m:
            if len(missions) >= _MAX_MISSIONS:
                break
            head = m.group(2).strip()
            parts = _DASH_SPLIT_RE.split(head, 1)
            missions.append({
                "title": " ".join(parts[0].split())[:80],
                "description": (" ".join(parts[1].split())[:300]
                                if len(parts) > 1 else ""),
                "objectives": [],
            })
            continue
        o = _OBJECTIVE_RE.match(line)
        if not o or not missions:
            continue
        if len(missions[-1]["objectives"]) >= _MAX_OBJECTIVES:
            continue
        text = o.group(1).strip()
        otype, target, count = "reach", "", 1
        ann = _ANNOTATION_RE.search(text)
        if ann:
            declared = (ann.group(1) or "").lower()
            if declared in _VALID_TYPES:
                otype = declared
                target = (ann.group(2) or "").strip()
                if ann.group(3):
                    count = max(1, min(_MAX_COUNT, int(ann.group(3))))
                # Only strip an annotation the compiler understood. A trailing
                # parenthetical that turned out to be prose stays in the
                # description, where the author put it.
                text = text[:ann.start()].strip()
        text = " ".join(text.split()).rstrip(".") or "Complete this objective."
        missions[-1]["objectives"].append({
            "description": text + ".", "type": otype,
            "target_ref": target, "count": count,
        })

    # A mission with no objectives is dropped by the schema anyway ("the
    # objectives are the whole point" — unity_prompts.py), so it is not
    # restored either.
    return [m for m in missions if m["objectives"]]


def inject_missing_concept_missions(schema) -> list[str]:
    """Append a MissionSpec for every brief-declared mission the schema-fill
    step did not write. Mutates `schema.missions` in place (append-only) and
    returns fixer log messages in the same style as the other Step 5 passes.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    declared = extract_declared_missions(meta.get("user_prompt", ""))
    if not declared:
        return []

    def _key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

    have_titles = {_key(m.title) for m in (schema.missions or [])}
    taken_ids = {o.id for m in (schema.missions or []) for o in m.objectives}
    out: list[str] = []
    added: list[MissionSpec] = []

    for i, spec in enumerate(declared):
        if _key(spec["title"]) in have_titles:
            continue
        objectives: list[MissionObjective] = []
        for j, o in enumerate(spec["objectives"]):
            oid = _slug(o["description"], f"obj_{len(taken_ids) + j + 1}")
            while oid in taken_ids:
                oid += "_b"
            taken_ids.add(oid)
            objectives.append(MissionObjective(
                id=oid, description=o["description"], type=o["type"],
                target_ref=o["target_ref"], count=o["count"]))
        mission = MissionSpec(
            id=_slug(spec["title"], f"m{len(schema.missions or []) + i + 1}"),
            title=spec["title"], description=spec["description"],
            objectives=objectives)
        added.append(mission)
        have_titles.add(_key(spec["title"]))

    if not added:
        return out
    schema.missions = list(schema.missions or []) + added
    out.append(
        f"Brief declared {len(added)} mission"
        f"{'' if len(added) == 1 else 's'} the schema-fill did not write "
        f"({', '.join(repr(m.title) for m in added)}) — restored from the "
        f"brief's own MISSIONS section, "
        f"{sum(len(m.objectives) for m in added)} objective(s)")
    # Named, because a restored mission changes what the player is SHOWN: the
    # rules micro states every mission as steps and SEAM 9 sums every tracker.
    unresolved = [o.id for m in added for o in m.objectives
                  if not o.target_ref.strip()]
    if unresolved:
        out.append(
            "Brief-declared objective(s) " + ", ".join(unresolved)
            + " name no target — left for unity_objective_targets to resolve "
              "from their own descriptions")
    return out
