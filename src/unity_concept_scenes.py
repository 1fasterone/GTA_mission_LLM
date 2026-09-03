"""Concept-named SCENE rescue — restores the brief's own "SCENES" entries
when the Step-3 schema-fill LLM silently drops or renames them.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B) — the sibling of
unity_concept_entities.py / unity_concept_scripts.py for the world's own
places. Same cause as both (see [[step3-sees-only-the-concept]]): Step 3 is
handed the Step-2 concept prose and nothing else, so a brief's explicit scene
list has already been paraphrased away by the time schema-fill runs.

Measured live on prompts/coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief named 5 scenes exactly; the
LLM arm kept 1 and invented 4 replacements (RaceStart, MidLap, FinishLine,
WarmingHut/CrewChiefHub, LevelSelect) in their place. Entities and scripts,
which already had rescue layers, survived at 100% on the same run — this
closes the same gap for scenes.

Deterministic regex parse of the ORIGINAL user_prompt
(schema.meta['user_prompt']), reusing the exact section/list-folding
primitives src/unity_briefparse.py's --no-llm control arm already uses (see
src/unity_brief_sections.py) — one parser, two callers, so the offline fill
and this rescue can never disagree about what the brief's SCENES section
says. Append-only: a scene the LLM DID write is never touched or renamed,
only the missing ones are added.
"""

from __future__ import annotations

from .unity_brief_sections import (_DASH_SPLIT_RE, _NUMBERED_RE, _fold_numbered,
                                   _section, _to_pascal)
from .unity_schema import SceneSpec

# Deterministic time-of-day read from the scene's own words. Not a guess: the
# brief either says one of these or the scene keeps the "day" default, and the
# default is logged by the caller.
_TIME_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("night",   ("night", "midnight", "darkness", "moonlit", "after dark")),
    ("dusk",    ("dusk", "sunset", "evening", "twilight")),
    ("dawn",    ("dawn", "sunrise", "first light", "daybreak")),
    ("day",     ("day", "daylight", "noon", "midday", "morning", "afternoon")),
)


def _time_of_day(text: str) -> str:
    low = (text or "").lower()
    for label, words in _TIME_WORDS:
        if any(w in low for w in words):
            return label
    return "day"


def _split_name_body(entry: str) -> list[str]:
    """Split "Name<sep>description" on whichever separator comes FIRST.

    Both separators are live and they nest, so a fixed precedence gets one form
    wrong every time. "- Sunny Speedway: the first circuit — a looping oval
    through a cartoon toy town" splits on the colon; "1. MainMenu — animated
    title: skydiver silhouette against sunset" splits on the dash. Trying the
    dash first named the turbo_toys scene
    "SunnySpeedwayTheFirstCircuit"; trying the colon first would do the same to
    dropzone's. Earliest-wins is the only rule that reads both.
    """
    dash = _DASH_SPLIT_RE.search(entry)
    colon = entry.find(":")
    if dash is not None and (colon < 0 or dash.start() < colon):
        return [entry[:dash.start()], entry[dash.end():]]
    if colon >= 0:
        return [entry[:colon], entry[colon + 1:]]
    return [entry]


def extract_named_scenes(user_prompt: str) -> list[SceneSpec]:
    """Parse the brief's own "SCENES" section into SceneSpecs, in source
    order. [] when the brief has no recognizable SCENES section (e.g. a
    one-line vibe prompt). Accepts both the numbered house form
    ("1. MainMenu — ...") and the bulleted one ("- Main Menu: ...").
    """
    specs: list[SceneSpec] = []
    seen: set[str] = set()
    for entry in _fold_numbered(_section(user_prompt, "SCENES"), bullets=True):
        m = _NUMBERED_RE.match(entry)
        body = m.group(2).strip() if m else entry.strip()
        # Numbered entries split name from body on an em dash; bulleted ones
        # ("- Main Menu: play, best times, settings, quit.") split on the first
        # colon. Try the dash first so a dash-form description containing a
        # colon ("animated title: skydiver silhouette...") keeps its whole body.
        parts = _split_name_body(body)
        name = _to_pascal(parts[0], "")
        if not name or name in seen:
            continue
        seen.add(name)
        desc = parts[1].strip() if len(parts) > 1 else ""
        specs.append(SceneSpec(name=name, description=desc,
                               time_of_day=_time_of_day(desc)))
    return specs


# UnitySchema.scene_specs caps at max_length=8, and unity_pipeline._programmatic_fix
# independently hard-truncates schema.scenes to [:8] before this rescue ever runs
# (Step 5 fixer runs first) — the rescue must respect the same budget itself or its
# own append would be silently chopped downstream with no log line explaining why.
_MAX_SCENES = 8
_CHECKLIST_MAX_DESC = 200  # SceneSpec.description itself clamps to 200 chars


def render_scene_checklist(user_prompt: str) -> str:
    """Render the brief's own SCENES entries as a mandatory checklist for the
    Step-3 schema-fill prompt. "" when the brief has no SCENES section.

    Deterministic string assembly over the same parse the rescue uses, so the
    names asked for here are byte-identical to the names the rescue would
    restore — the model honouring the checklist and the fixer repairing it
    converge on one scene list instead of two overlapping ones.
    """
    named = extract_named_scenes(user_prompt)
    if not named:
        return ""
    lines = [
        "REQUIRED SCENES — the brief names these scenes explicitly. Your",
        '"scenes" and "scene_specs" arrays MUST contain one entry for EACH',
        "line below, using EXACTLY the name given. These ARE the game's",
        "levels and menus: a line you leave out is a place the player can",
        "never go. Add further scenes only for structure the brief also implies.",
    ]
    for i, s in enumerate(named[:_MAX_SCENES], start=1):
        lines.append(f'  {i}. name="{s.name}"')
        if s.description:
            lines.append(f"     {s.description[:_CHECKLIST_MAX_DESC]}")
    return "\n".join(lines) + "\n"


def inject_missing_concept_scenes(schema) -> list[str]:
    """Append a SceneSpec (and its bare name) for every brief-named scene the
    schema-fill step dropped. Mutates schema.scenes and schema.scene_specs
    together, in place (append-only), and returns fixer log messages in the
    same style as other schema fixers.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    named = extract_named_scenes(meta.get("user_prompt", ""))
    if not named:
        return []

    existing = {n.strip().lower() for n in schema.scenes}
    budget = _MAX_SCENES - len(schema.scenes)

    added: list[SceneSpec] = []
    dropped: list[str] = []
    for spec in named:
        key = spec.name.strip().lower()
        if not key or key in existing:
            continue
        if len(added) >= budget:
            dropped.append(spec.name)
            continue
        added.append(spec)
        existing.add(key)

    msgs: list[str] = []
    if added:
        schema.scenes = list(schema.scenes) + [s.name for s in added]
        schema.scene_specs = list(schema.scene_specs) + added
        msgs.append(
            f"Concept named {len(added)} scene"
            f"{'' if len(added) == 1 else 's'} the schema-fill dropped "
            f"({', '.join(s.name for s in added)}) — restored from the "
            "brief's own SCENES section instead of shipping a game missing "
            "those places."
        )
    if dropped:
        # Abstain loudly: the schema's own cap is ground truth, so say exactly
        # which scenes did not fit rather than silently shipping fewer than
        # the brief named.
        msgs.append(
            f"schema.scene_specs is capped at {_MAX_SCENES} — could NOT "
            f"restore {len(dropped)} brief-named scene(s): "
            f"{', '.join(dropped)}. Trim the brief's SCENES list."
        )
    return msgs
