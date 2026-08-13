"""
Deterministic brief -> UnitySchema fill — the `--no-llm` replacement for Steps 2-3.

WHY THIS EXISTS
---------------
Steps 4-7 were always LLM-free. Steps 2-3 were not, and a structured brief
already states, in parseable form, almost everything Step 3 is asked to invent:
TITLE, SCENES, NARRATIVE, SCRIPTS, ENTITIES, AUDIO, FEATURES, VISUAL STYLE,
COLOR PALETTE, RENDER PIPELINE, PLATFORM. The rescue layers
(unity_concept_entities / unity_concept_scripts) already parse the brief's own
ENTITIES and SCRIPTS bullets out of `meta['user_prompt']` and restore whatever
the schema-fill model dropped — which means the deterministic reader of those
two sections is not a fallback, it is a second, independent implementation of
the same job.

This module finishes that thought: it reads the WHOLE brief and produces a
valid UnitySchema without a model in the loop, so the compiler can be run as a
pure compiler. That makes the LLM's marginal contribution measurable — run the
same brief with and without a model and diff the two schemas — instead of
merely arguable.

EVAL DISCIPLINE (read before using this in any judgement)
---------------------------------------------------------
A `--no-llm` run is NOT a test of the Pipeline LLM, and must never be reported
as one. It is the opposite: the control arm. Step 3 does not run, so nothing
about the run says anything about Step 3's quality.

It is also NOT the Compiler Engineer hand-filling a schema. The engineer writes
the PARSER; the parser reads a brief the user wrote. No schema content is
authored in this file — every string in the emitted schema comes from the
brief's own bytes or from a named, logged default. `meta['fill']` records which
of the two ran, so any downstream reader can tell whose hand wrote the schema.

ABSTAIN LOUDLY
--------------
A brief that is not structured enough to fill cannot be quietly half-filled —
that would ship a generic game wearing the brief's name. parse_brief() raises
BriefParseError naming the missing sections and the remedy. Prose briefs
("a game about a lighthouse keeper") have nothing to parse and are exactly the
case where the LLM is load-bearing; they are told so.
"""

from __future__ import annotations

import re
from typing import Any, cast

from .unity_archetypes import classify
from .unity_concept_entities import (inject_missing_concept_entities,
                                     extract_named_entities)
from .unity_concept_scripts import inject_missing_concept_scripts
from .unity_schema import (AudioTrack, ColorPalette, EnvironmentSpec, SceneSpec,
                           StoryBeat, UnitySchema)


class BriefParseError(ValueError):
    """The brief is too thin to fill deterministically. Carries the remedy."""


# ── Section slicing ───────────────────────────────────────────────────────────
# Same header vocabulary unity_pipeline._BRIEF_MARKER_RE gates on, plus the
# trailing scalar lines. A header line may carry a parenthetical count, an em
# dash subtitle or a colon — "SCENES (8 total):" and "AUDIO — twelve cues that
# should sound unmistakably like a skydive:" are both real headers in
# prompts/dropzone.txt.
_HEADERS = (
    "TITLE", "GENRE", "STYLE", "CORE PREMISE", "SCENES", "NARRATIVE",
    "SCRIPTS", "ENTITIES", "AUDIO", "FEATURES", "VISUAL STYLE",
    "COLOR PALETTE", "PROCEDURAL GEOMETRY", "PROCEDURAL", "RENDER PIPELINE",
    "UNITY VERSION", "PLATFORM", "GAME TYPE", "LOCOMOTION",
)
_ANY_HEADER_RE = re.compile(
    r"^(" + "|".join(re.escape(h) for h in _HEADERS) + r")\b", re.IGNORECASE)

# A scalar declaration: "PLATFORM: PC", "RENDER PIPELINE: URP  (volumetric...)".
_SCALAR_RE_TMPL = r"^{key}\s*[:\-—]\s*(.+)$"

# "1. MainMenu         — animated title: skydiver silhouette against sunset"
_NUMBERED_RE = re.compile(r"^(\d{1,2})[.)]\s+(.+)$")
# The em/en dash (or " - ") that splits a numbered entry's name from its body.
_DASH_SPLIT_RE = re.compile(r"\s+[—–]\s+|\s+-\s+")
# "(on_enter_zone)" trailing a narrative beat
_TRIGGER_RE = re.compile(r"\((on_start|on_enter_zone|on_timer|on_kill|"
                         r"on_collect|on_death|manual)\)\s*$", re.IGNORECASE)
# "  primary    #87CEEB  (open-sky daylight — clear horizon blue)"
_PALETTE_RE = re.compile(
    r"^(primary|secondary|accent|background|trim)\s+(#[0-9A-Fa-f]{6})\b")
_PALETTE_NAME_RE = re.compile(r'name\s+"([^"]+)"')
# "- freefall_wind_rush     ambient  looping  full terminal-velocity wind roar"
_AUDIO_RE = re.compile(
    r"^([A-Za-z][\w-]*)\s+(ambient|sfx|music|ui|dialogue|voice)\b\s*"
    r"(looping|loop|oneshot)?\s*(.*)$", re.IGNORECASE)

_VALID_PLATFORM = ("pc", "mobile", "console", "webgl", "all")
_VALID_PIPELINE = {"urp": "URP", "hdrp": "HDRP", "builtin": "builtin",
                   "built-in": "builtin", "built in": "builtin"}
_VALID_UNITY = ("6000.0", "6000.1", "6000.4", "2022.3", "2021.3")
_VALID_GAME_TYPE = ("2D", "2.5D", "3D", "VR", "top_down", "2d_topdown",
                    "isometric")
_VALID_LOCOMOTION = ("walk", "drive", "fly", "swim")


def _lines(prompt: str) -> list[str]:
    return (prompt or "").splitlines()


def _section(prompt: str, header: str) -> list[str]:
    """Every line under `header`, up to the next recognised section header."""
    out: list[str] = []
    grabbing = False
    head_re = re.compile(r"^" + re.escape(header) + r"\b", re.IGNORECASE)
    for line in _lines(prompt):
        stripped = line.strip()
        if not grabbing:
            if head_re.match(stripped):
                grabbing = True
            continue
        # A new header closes the section. Checked against the stripped line so
        # an indented continuation can never be mistaken for a header.
        if _ANY_HEADER_RE.match(stripped) and not head_re.match(stripped):
            break
        out.append(line)
    return out


def _scalar(prompt: str, key: str) -> str:
    """Read a 'KEY: value' declaration anywhere in the brief. '' when absent."""
    pat = re.compile(_SCALAR_RE_TMPL.format(key=re.escape(key)), re.IGNORECASE)
    for line in _lines(prompt):
        m = pat.match(line.strip())
        if m:
            # Strip a trailing parenthetical rationale: "URP  (volumetric ...)"
            return re.sub(r"\s*\(.*$", "", m.group(1)).strip()
    return ""


def _fold_numbered(lines: list[str], *, bullets: bool = False) -> list[str]:
    """Fold a wrapped list into one logical string per entry.

    The generalised sibling of unity_concept_entities._fold_bullets: SCENES and
    NARRATIVE wrap their descriptions across indented continuation lines, and
    reading them line-by-line truncates every entry at ~80 cols.

    `bullets=True` also opens an entry on a "- " marker. Both list styles are
    live in prompts/: dropzone.txt numbers its scenes ("1. MainMenu — ..."),
    turbo_toys.txt and grandmas_antique_shop.txt bullet theirs
    ("- Main Menu: ..."). A reader that knows only one silently sees zero
    scenes in half the corpus — which is an abstain, not a wrong answer, but
    an abstain on a brief that plainly stated its scenes.

    A line that is neither a marker nor an indented continuation (the trailing
    "Scene transitions fade between hub, shop, museum..." prose both briefs put
    under the list) closes the current entry instead of being absorbed.
    """
    entries: list[str] = []
    pending: list[str] = []

    def close() -> None:
        if pending:
            entries.append(" ".join(pending))
            pending.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close()
            continue
        if _NUMBERED_RE.match(stripped):
            close()
            pending.append(stripped)
        elif bullets and stripped.startswith("-"):
            close()
            pending.append(stripped.lstrip("-").strip())
        elif pending and line[:1].isspace():
            pending.append(stripped)
        else:
            close()
    close()
    return entries


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


def _to_pascal(text: str, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    out = "".join(w[:1].upper() + w[1:] for w in words)
    if out and out[0].isdigit():
        out = "P" + out
    return out or fallback


# ── Section readers ───────────────────────────────────────────────────────────

def _read_title(prompt: str) -> tuple[str, str]:
    """(project_name, description) from the TITLE line + the premise prose.

    House form is a bare first line — "DropZone — a 3D skydiving simulation and
    stunt game for PC." — with the design paragraph under it. An explicit
    "TITLE: X" line wins when present.
    """
    # Three title forms are live in prompts/: an inline "TITLE: X", a bare
    # "TITLE" header with the name on the next line (turbo_toys,
    # grandmas_antique_shop), and no TITLE at all with the name opening the
    # first line (dropzone). Reading only the first line named two real briefs
    # "TITLE".
    explicit = _scalar(prompt, "TITLE")
    if not explicit:
        section = [ln.strip() for ln in _section(prompt, "TITLE") if ln.strip()]
        if section:
            explicit = section[0]
    lines = [ln.strip() for ln in _lines(prompt)]
    first = next((ln for ln in lines if ln), "")

    raw_title = explicit or first
    name_part = _DASH_SPLIT_RE.split(raw_title, maxsplit=1)[0].strip()
    project_name = _to_pascal(name_part, "UnityProject")

    # Description: CORE PREMISE, else GENRE (both are prose statements of what
    # the game IS), else the first paragraph under the title line.
    for header in ("CORE PREMISE", "GENRE"):
        body = [ln.strip() for ln in _section(prompt, header) if ln.strip()]
        if body:
            return project_name, " ".join(body)

    desc: list[str] = []
    started = False
    for ln in lines:
        if not started:
            if ln == first:
                started = True
            continue
        if not ln:
            if desc:
                break
            continue
        if _ANY_HEADER_RE.match(ln):
            break
        desc.append(ln)
    # The title's own subtitle is the one-line description when there is no
    # paragraph under it.
    tail = _DASH_SPLIT_RE.split(raw_title, maxsplit=1)
    fallback = tail[1].strip() if len(tail) > 1 else ""
    return project_name, (" ".join(desc) or fallback)


def _read_scenes(prompt: str) -> tuple[list[str], list[SceneSpec]]:
    specs: list[SceneSpec] = []
    seen: set[str] = set()
    for entry in _fold_numbered(_section(prompt, "SCENES"), bullets=True):
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
    return [s.name for s in specs], specs


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


def _read_story_beats(prompt: str) -> list[StoryBeat]:
    beats: list[StoryBeat] = []
    for entry in _fold_numbered(_section(prompt, "NARRATIVE")):
        m = _NUMBERED_RE.match(entry)
        if not m:
            continue
        idx, body = m.group(1), m.group(2).strip()
        trig_m = _TRIGGER_RE.search(body)
        trigger = trig_m.group(1).lower() if trig_m else "manual"
        if trig_m:
            body = body[:trig_m.start()].strip()
        parts = _DASH_SPLIT_RE.split(body, maxsplit=1)
        label = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else label
        beats.append(StoryBeat(id=f"beat_{int(idx):02d}", label=label,
                               description=desc, trigger=trigger))
    return beats


def _read_audio(prompt: str) -> list[AudioTrack]:
    tracks: list[AudioTrack] = []
    section = _section(prompt, "AUDIO")
    # AUDIO bullets wrap like every other list; fold on the "- " marker.
    folded: list[str] = []
    pending: list[str] = []
    for line in section:
        stripped = line.strip()
        if not stripped:
            if pending:
                folded.append(" ".join(pending)); pending = []
            continue
        if stripped.startswith("-"):
            if pending:
                folded.append(" ".join(pending))
            pending = [stripped.lstrip("-").strip()]
        elif pending and line[:1].isspace():
            pending.append(stripped)
    if pending:
        folded.append(" ".join(pending))

    for entry in folded:
        m = _AUDIO_RE.match(entry)
        if not m:
            continue
        name, category, loop_word, desc = m.groups()
        category = category.lower()
        tracks.append(AudioTrack(
            name=name,
            description=(desc or "").strip() or name.replace("_", " "),
            loop=bool(loop_word and loop_word.lower() in ("looping", "loop")),
            category="music" if category == "music" else category,
        ))
    return tracks


def _read_palette(prompt: str) -> ColorPalette | None:
    section = _section(prompt, "COLOR PALETTE")
    if not section:
        return None
    header = next((ln for ln in _lines(prompt)
                   if re.match(r"^COLOR PALETTE\b", ln.strip(), re.IGNORECASE)), "")
    name_m = _PALETTE_NAME_RE.search(header)
    slots: dict[str, str] = {}
    for line in section:
        m = _PALETTE_RE.match(line.strip())
        if m:
            slots[m.group(1).lower()] = m.group(2)
    if not slots:
        return None
    return ColorPalette(name=(name_m.group(1) if name_m else "Brief"), **slots)


def _read_features(prompt: str) -> list[str]:
    raw = _scalar(prompt, "FEATURES")
    if not raw:
        # Also accept a bulleted FEATURES section.
        raw = " ".join(ln.strip().lstrip("-").strip()
                       for ln in _section(prompt, "FEATURES") if ln.strip())
    return [f.strip().lower().replace(" ", "_")
            for f in re.split(r"[,;]", raw) if f.strip()]


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_brief(user_prompt: str) -> tuple[UnitySchema, list[str]]:
    """Fill a UnitySchema from a structured brief. No LLM, no I/O, no network.

    Returns (schema, log_messages). Raises BriefParseError, naming the missing
    sections and the remedy, when the brief cannot support a fill — never a
    partial schema wearing the brief's name.
    """
    msgs: list[str] = []
    prompt = user_prompt or ""

    # ── Axes: the same classifier the LLM path uses to OVERRIDE Step 3.
    # Here it is the primary source, so the confidence bar matters more: a
    # game_type with no signal cannot be silently defaulted to 3D.
    intent = classify(prompt)

    game_type = _scalar(prompt, "GAME TYPE")
    if game_type and game_type not in _VALID_GAME_TYPE:
        game_type = ""
    if game_type:
        msgs.append(f"game_type '{game_type}' — declared by the brief")
    elif intent.game_type and intent.game_type_confidence >= 3:
        game_type = intent.game_type
        hits = ", ".join(intent.matched.get("game_type", []))
        msgs.append(f"game_type '{game_type}' — classifier (brief said: {hits})")

    # ── The abstain gate. Everything above is evidence; this decides.
    # SCRIPTS is deliberately NOT required: a brief with scenes and entities but
    # no SCRIPTS section is still fillable, and inject_missing_concept_scripts
    # below adds whatever bullets it does carry.
    entity_rows = extract_named_entities(prompt)
    scenes, scene_specs = _read_scenes(prompt)

    missing: list[str] = []
    if not game_type:
        missing.append(
            "GAME TYPE — no dimension keyword scored. Add a 'GAME TYPE: 3D' "
            "line, or use a phrase the classifier knows ('third-person', "
            "'3d game', 'side-scroller', 'top-down', 'isometric', 'VR'). "
            "Note the bare token '3D' does NOT match on its own")
    if not scenes:
        missing.append(
            "SCENES — add a numbered 'SCENES:' list ('1. Name — description')")
    if not entity_rows:
        missing.append(
            "ENTITIES — add an 'ENTITIES:' bullet list "
            "('- Player: description' or '- Name: description (kind: player)')")
    if missing:
        raise BriefParseError(
            "[briefparse.strict] this brief cannot be filled without a model.\n"
            + "\n".join(f"  - {m}" for m in missing)
            + "\n\nA brief with no parseable structure is exactly the case the "
              "Pipeline LLM exists for — drop --no-llm and let Steps 2-3 run, "
              "or add the sections above and re-run offline."
        )

    # ── Scalars. Each falls back to a NAMED default that is logged, never a
    # silent one.
    project_name, description = _read_title(prompt)

    platform = (_scalar(prompt, "PLATFORM") or "").lower()
    platform = {"pc": "pc", "windows": "pc", "desktop": "pc"}.get(platform, platform)
    if platform not in _VALID_PLATFORM:
        platform = "pc"
        msgs.append("platform 'pc' — brief did not declare PLATFORM (default)")

    pipeline_raw = (_scalar(prompt, "RENDER PIPELINE") or "").lower()
    render_pipeline = _VALID_PIPELINE.get(pipeline_raw, "")
    if not render_pipeline:
        render_pipeline = "URP"
        msgs.append("render_pipeline 'URP' — brief did not declare RENDER PIPELINE (default)")

    unity_version = _scalar(prompt, "UNITY VERSION")
    if unity_version not in _VALID_UNITY:
        unity_version = "6000.0"
        msgs.append("unity_version '6000.0' — brief did not declare UNITY VERSION (default)")

    locomotion = (_scalar(prompt, "LOCOMOTION") or "").lower()
    if locomotion not in _VALID_LOCOMOTION:
        if intent.locomotion and intent.locomotion_confidence >= 3:
            locomotion = intent.locomotion
            hits = ", ".join(intent.matched.get("locomotion", []))
            msgs.append(f"locomotion '{locomotion}' — classifier (brief said: {hits})")
        else:
            locomotion = "walk"
            msgs.append("locomotion 'walk' — no locomotion keyword scored (default)")

    visual_style = (_scalar(prompt, "VISUAL STYLE") or _scalar(prompt, "STYLE")
                    or "stylized")

    story_beats = _read_story_beats(prompt)
    audio_tracks = _read_audio(prompt)
    if not audio_tracks and _section(prompt, "AUDIO"):
        # The brief HAS an AUDIO section but wrote it as prose (turbo_toys.txt:
        # "Revving cartoon toy engine loop, squealing cartoon tire skids...").
        # Splitting that into named cues means inventing the names — Step 2's
        # job, not a parser's. Abstain in place and say so.
        msgs.append(
            "AUDIO section is prose, not a cue table — 0 tracks parsed. "
            "Use '- cue_name  sfx|ambient|music  [looping]  description' rows "
            "to carry cues through the offline fill")
    palette = _read_palette(prompt)
    features = _read_features(prompt)

    # Environment: the first scene's own time-of-day is the project default —
    # derived, not invented.
    environment = EnvironmentSpec(
        time_of_day=(scene_specs[0].time_of_day if scene_specs else "day"))

    # cast, not ignore: every one of these five was clamped against its _VALID_*
    # set above (with a logged named default on miss), so the runtime value is
    # always a member of the Literal. mypy simply cannot narrow str -> Literal
    # through a membership test. Pydantic re-validates on construction, so a
    # clamp that ever regressed would raise here rather than ship a bad schema.
    schema = UnitySchema(
        project_name    = project_name,
        description     = description,
        game_type       = cast(Any, game_type),
        platform        = cast(Any, platform),
        render_pipeline = cast(Any, render_pipeline),
        unity_version   = cast(Any, unity_version),
        locomotion      = cast(Any, locomotion),
        visual_style    = visual_style,
        scenes          = scenes,
        scene_specs     = scene_specs,
        features        = features,
        story_beats     = story_beats,
        audio_tracks    = audio_tracks,
        color_palette   = palette,
        environment     = environment,
        entities        = [],
        scripts         = [],
        packages        = [],
    )
    schema.meta = {
        **(schema.meta or {}),
        "user_prompt": prompt,
        # No concept step ran. Downstream consumers that gate on a concept
        # (npc_dialog pre-extraction, audio cue extraction, sketchfab
        # translation) MUST see an empty concept and skip, not a synthesised
        # one — a fabricated concept would be the engineer writing Step 2's
        # output, which is exactly what Eval Discipline forbids.
        "concept": "",
        "fill": "deterministic",        # vs "llm" — whose hand wrote this schema
        "genre": intent.genre,
    }

    # Pydantic drops any FEATURES token outside the FeatureFlag vocabulary, and
    # drops it silently — a brief asking for "post_processing" (dropzone.txt and
    # this file both do) loses it with no trace on either fill. Name the
    # casualties: a feature the brief asked for and did not get is exactly the
    # kind of quiet subtraction this compiler is supposed to refuse.
    _dropped = [f for f in features if f not in set(schema.features)]
    if _dropped:
        msgs.append(
            "FEATURES not in the schema vocabulary, dropped: "
            + ", ".join(_dropped)
            + " — remove them from the brief or add them to FeatureFlag")

    # Entities and scripts come from the SAME injectors the LLM path uses to
    # rescue dropped content. Against an empty list every brief row is
    # "missing", so the rescue IS the fill — one implementation, two callers,
    # no chance of the offline schema and the rescued schema disagreeing.
    # Their log lines say "the schema-fill dropped ..." — accurate on the LLM
    # path, misleading here, where no schema-fill ran to drop anything. Flag the
    # reframe rather than rewording another module's messages.
    msgs.append("Offline fill: the two lines below are the rescue injectors "
                "reporting a 100% miss against an empty schema — that is the "
                "fill working, not content being lost")
    msgs.extend(inject_missing_concept_entities(schema))
    msgs.extend(inject_missing_concept_scripts(schema))

    msgs.append(
        f"Deterministic fill: {len(scenes)} scenes, {len(schema.entities)} entities, "
        f"{len(schema.scripts)} scripts, {len(story_beats)} beats, "
        f"{len(audio_tracks)} audio cues, {len(features)} features"
    )
    return schema, msgs
