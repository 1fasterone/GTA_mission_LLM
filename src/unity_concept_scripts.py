"""
Concept-named SCRIPT rescue — restores the brief's own "SCRIPTS" bullets when
the Step-3 schema-fill LLM silently drops them.

The exact sibling of src/unity_concept_entities.py, for the other half of the
brief. That module closed the entity leak; this one closes the systems leak,
and the two share a single cause (see [[step3-sees-only-the-concept]]): Step 3
is handed the Step-2 concept prose and NOTHING else, so anything the concept
paraphrased away is simply not in front of the schema-fill model. Entities were
already guaranteed by a checklist + rescue. Scripts had neither, so a brief
could state "each of these systems is required; implement as its own module"
and still get silence.

Hit live on the grandma brief (Grandma's Antique Shop): of 12 explicitly
required systems, 3 arrived as their own module, 2 arrived partial, and 7 —
ProvenanceSystem, InsuranceAudit, MuseumMetaProgression, NightlyShopReshuffle,
YelpNewGamePlus, PhotoMode, ShuffleDodgeControls — had no module at all. The
Museum Wing scene left with them. Nothing errored; the systems were never shown
to the model that was supposed to write them.

Bullet form is the house SCRIPTS format, wrapped continuation lines included:

    SCRIPTS
    - MirrorSpawning        (custom)   raiders enter only through AntiqueMirror
                                       entities; per-mirror spawn cadence
    - ShopCamera            (camera)   Cinemachine third-person rig

Deterministic regex parse of the ORIGINAL user_prompt
(schema.meta['user_prompt']) — never the concept prose, which has already
renamed things. Append-only: a script the LLM DID write is never touched, only
the missing ones are added.
"""

from __future__ import annotations

import re

from .unity_concept_entities import _fold_bullets
from .unity_schema import _VALID_SCRIPT_TYPES, UnityScript

# An ALL-CAPS line (optional trailing colon) is the next section header. The
# colon matters: the entity module's own header regex has no ":" in it, and
# briefs in this repo write both "ENTITIES" and "ENTITIES:" — without allowing
# it, a SCRIPTS list followed by "ENTITIES:" would keep folding and read the
# entity bullets as scripts (dropzone.txt is exactly that shape).
_SECTION_END_RE = re.compile(r"^[A-Z][A-Z0-9 /&'-]{1,40}:?$")

# "- Name  (type)  description" — the documented house form. The type
# parenthetical is what distinguishes a script bullet from an entity bullet.
_SCRIPT_BULLET_RE = re.compile(
    r"^([A-Za-z][\w'-]*)\s*\(\s*([A-Za-z_][\w ]*?)\s*\)\s*(.*)$")

# Fallback form: "- Name: description (type: custom)" — same information, the
# entity module's punctuation. Accepted so a brief that mixes the two styles
# does not silently lose half its systems.
_SCRIPT_COLON_RE = re.compile(r"^([A-Za-z][\w'-]*)\s*:\s*(.+)$")
_TYPE_CLAUSE_RE = re.compile(r"\(\s*type:\s*([A-Za-z_][\w ]*?)\s*\)", re.IGNORECASE)

# The schema caps `scripts` at 24 (unity_schema.UnitySchema). The rescue must
# respect it or the whole schema fails validation on append.
_MAX_SCRIPTS = 24
_CHECKLIST_MAX_SCRIPTS = _MAX_SCRIPTS
# A script description is the SPECIFICATION of a mechanic, not a label, and
# this is the only form of it Step 3 ever sees. At 160 the Gridfall brief's
# GridLogic entry (517 chars: grid, spawn, gravity, collision refusal, lock,
# row clear, score, level, top-out) was cut mid-word at "on every tick of a
# grav" — 69% of the mechanic discarded, and the built game had none of it.
# Scripts are few (capped at _CHECKLIST_MAX_SCRIPTS), so the prompt cost of a
# real budget here is trivial; the entity checklist keeps its tight cap
# because a brief may name 40 entities.
_CHECKLIST_MAX_DESC = 900

# Script types that own a singleton role in the generated project: the
# generator selects a template by `type` and attaches by `attach_to`, so two
# scripts of the same non-custom type land on the same GameObject with the same
# job (two Update loops, two "Instance" singletons). A briefed script whose type
# is already taken is therefore added as "custom" — the MODULE is what the brief
# asked for, and a demoted type still carries its description and its name.
# "custom", "enemy" and "collectible" are legitimately many-per-project.
_MULTI_INSTANCE_TYPES = frozenset({"custom", "enemy", "collectible"})

# type -> attach_to. Anything else attaches to the systems object, which is the
# UnityScript default and what a gameplay system wants.
_ATTACH_FOR_TYPE = {"player": "player", "camera": "camera",
                    "game_manager": "game_manager"}

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _normalise_type(raw: str) -> str:
    """Mirror UnityScript.normalise_type so the checklist states the type the
    schema would actually store — telling the model to write type="saveload"
    when the field would coerce it to "custom" invites a mismatch the rescue
    then has to reconcile."""
    t = (raw or "").strip().lower().replace(" ", "_")
    return t if t in _VALID_SCRIPT_TYPES else "custom"


def _describe(name: str) -> str:
    """Last-resort description from the script's own name — UnityScript
    requires one, and a bullet may state only a name and a type."""
    words = _CAMEL_SPLIT_RE.sub(" ", name).strip()
    return f"{words} system named by the brief."


def _section_lines(user_prompt: str) -> list[str]:
    """The SCRIPTS section's own lines, header excluded, ending at the next
    ALL-CAPS section header."""
    lines = (user_prompt or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper().rstrip(":") == "SCRIPTS":
            start = i + 1
            break
    if start is None:
        return []
    out: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped and not line[:1].isspace() and _SECTION_END_RE.match(stripped):
            break
        out.append(line)
    return out


def extract_named_scripts(user_prompt: str) -> list[dict]:
    """Parse the brief's own "SCRIPTS" section into
    [{"name", "type", "description"}, ...] in source order. [] when the brief
    has no recognizable SCRIPTS section (e.g. a one-line vibe prompt).
    """
    section = _section_lines(user_prompt)
    if not section:
        return []

    out: list[dict] = []
    taken: set[str] = set()
    for bullet in _fold_bullets(section):
        name = kind = desc = ""
        m = _SCRIPT_BULLET_RE.match(bullet)
        if m:
            name, kind, desc = m.group(1), m.group(2), m.group(3)
        else:
            m = _SCRIPT_COLON_RE.match(bullet)
            if not m:
                continue
            name, desc = m.group(1), m.group(2)
            type_m = _TYPE_CLAUSE_RE.search(desc)
            if type_m:
                kind = type_m.group(1)
                desc = _TYPE_CLAUSE_RE.sub("", desc)

        key = name.strip().lower()
        if not key or key in taken:
            continue
        taken.add(key)
        desc = " ".join(desc.split()).strip().rstrip(".").strip()
        out.append({"name": name.strip(), "type": _normalise_type(kind),
                    "description": desc or _describe(name)})
    return out


def render_script_checklist(user_prompt: str) -> str:
    """Render the brief's own SCRIPTS bullets as a mandatory checklist for the
    Step-3 schema-fill prompt. "" when the brief has no SCRIPTS section.

    Deterministic string assembly over the same parse the rescue uses, so the
    names asked for here are byte-identical to the names the rescue would
    restore — the model honouring the checklist and the fixer repairing it
    converge on one script list instead of two overlapping ones.
    """
    named = extract_named_scripts(user_prompt)
    if not named:
        return ""
    lines = [
        "REQUIRED SCRIPTS — the brief names these systems explicitly. Your",
        '"scripts" array MUST contain one entry for EACH line below, using',
        "EXACTLY the name and type given. These ARE the game's systems: a line",
        "you leave out is a mechanic the game does not have. Do not merge two",
        "of them into one script, and do not rename them.",
    ]
    for i, s in enumerate(named[:_CHECKLIST_MAX_SCRIPTS], start=1):
        lines.append(f'  {i}. name="{s["name"]}"  type="{s["type"]}"')
        if s["description"]:
            lines.append(f"     {s['description'][:_CHECKLIST_MAX_DESC]}")
    return "\n".join(lines) + "\n"


def inject_missing_concept_scripts(schema) -> list[str]:
    """Append a UnityScript for every brief-named script the schema-fill step
    dropped. Mutates schema.scripts in place (append-only) and returns fixer
    log messages in the same style as other schema fixers.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    named = extract_named_scripts(meta.get("user_prompt", ""))
    if not named:
        return []

    existing_names = {s.name.strip().lower() for s in schema.scripts}
    taken_types = {s.type for s in schema.scripts if s.type not in _MULTI_INSTANCE_TYPES}

    added: list[UnityScript] = []
    demoted: list[str] = []
    dropped: list[str] = []
    budget = _MAX_SCRIPTS - len(schema.scripts)

    for s in named:
        key = s["name"].strip().lower()
        if not key or key in existing_names:
            continue
        if len(added) >= budget:
            dropped.append(s["name"])
            continue
        stype = s["type"]
        if stype not in _MULTI_INSTANCE_TYPES and stype in taken_types:
            demoted.append(f"{s['name']} ({stype}->custom)")
            stype = "custom"
        elif stype not in _MULTI_INSTANCE_TYPES:
            taken_types.add(stype)
        added.append(UnityScript(
            name=s["name"], type=stype, description=s["description"],
            attach_to=_ATTACH_FOR_TYPE.get(stype, "systems"),
        ))
        existing_names.add(key)

    msgs: list[str] = []
    if added:
        schema.scripts = list(schema.scripts) + added
        msgs.append(
            f"Concept named {len(added)} script"
            f"{'' if len(added) == 1 else 's'} the schema-fill dropped "
            f"({', '.join(s.name for s in added)}) — restored from the brief's "
            "own SCRIPTS section instead of shipping a game missing those systems."
        )
    if demoted:
        msgs.append(
            f"Restored script(s) re-typed to 'custom' because the type was "
            f"already filled: {', '.join(demoted)} — two scripts of one "
            "non-custom type attach twice to the same object."
        )
    if dropped:
        # Abstain loudly: the schema's own cap is ground truth, so say exactly
        # which systems did not fit rather than silently shipping 24 of 27.
        msgs.append(
            f"schema.scripts is capped at {_MAX_SCRIPTS} — could NOT restore "
            f"{len(dropped)} brief-named script(s): {', '.join(dropped)}. "
            "Trim the brief's SCRIPTS list or merge systems."
        )
    return msgs
