"""Deterministic brief -> UnitySchema fill — the `--no-llm` replacement for Steps 2-3.

WHY THIS EXISTS
---------------
Steps 4-7 were always LLM-free. Steps 2-3 were not, and a structured brief
already states, in parseable form, almost everything Step 3 is asked to invent:
TITLE, SCENES, NARRATIVE, SCRIPTS, ENTITIES, AUDIO, FEATURES, VISUAL STYLE,
COLOR PALETTE, RENDER PIPELINE, PLATFORM. The rescue layers
(unity_concept_entities / unity_concept_scripts / unity_concept_scenes /
unity_concept_narrative / unity_concept_audio / unity_concept_title) already
parse the brief's own sections out of `meta['user_prompt']` and restore
whatever the schema-fill model dropped — which means the deterministic reader
of those sections is not a fallback, it is a second, independent
implementation of the same job.

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
from .unity_brief_sections import _lines, _scalar, _section
from .unity_concept_audio import extract_named_audio, inject_missing_concept_audio
from .unity_concept_entities import (inject_missing_concept_entities,
                                     extract_named_entities)
from .unity_concept_narrative import (extract_named_story_beats,
                                      inject_missing_concept_narrative)
from .unity_concept_scenes import extract_named_scenes, inject_missing_concept_scenes
from .unity_concept_scripts import inject_missing_concept_scripts
from .unity_concept_title import extract_title, inject_missing_concept_title
from .unity_schema import (AudioTrack, ColorPalette, EnvironmentSpec, SceneSpec,
                           StoryBeat, UnitySchema)


class BriefParseError(ValueError):
    """The brief is too thin to fill deterministically. Carries the remedy."""


_VALID_PLATFORM = ("pc", "mobile", "console", "webgl", "all")
_VALID_PIPELINE = {"urp": "URP", "hdrp": "HDRP", "builtin": "builtin",
                   "built-in": "builtin", "built in": "builtin"}
_VALID_UNITY = ("6000.0", "6000.1", "6000.4", "2022.3", "2021.3")
_VALID_GAME_TYPE = ("2D", "2.5D", "3D", "VR", "top_down", "2d_topdown",
                    "isometric")
# "none" declares the game has no avatar (unity_schema.is_embodied). Without it
# an author who wrote `LOCOMOTION: none` had the line rejected as unparseable
# and silently replaced with "walk" — the exact declaration the UI-first path
# exists to honour, dropped at the parser.
_VALID_LOCOMOTION = ("walk", "drive", "fly", "swim", "none")

# "  primary    #87CEEB  (open-sky daylight — clear horizon blue)"
_PALETTE_RE = re.compile(
    r"^(primary|secondary|accent|background|trim)\s+(#[0-9A-Fa-f]{6})\b")
_PALETTE_NAME_RE = re.compile(r'name\s+"([^"]+)"')


# ── Section readers not yet promoted to their own rescue-layer module ─────────
# COLOR PALETTE and FEATURES have no rescue-layer sibling (Solution #3 covers
# scenes/audio/narrative/title only — see docs/COMPILER_AUDIT.md), so they stay
# local to the offline fill rather than living in unity_brief_sections.py.

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
    scene_specs = extract_named_scenes(prompt)
    scenes = [s.name for s in scene_specs]

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
    project_name, description = extract_title(prompt)

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

    story_beats = extract_named_story_beats(prompt)
    audio_tracks = extract_named_audio(prompt)
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

    # Every section below comes from the SAME injectors the LLM path uses to
    # rescue dropped content. Against an empty/placeholder schema every brief
    # row is "missing", so the rescue IS the fill — one implementation, two
    # callers, no chance of the offline schema and the rescued schema
    # disagreeing. Their log lines say "the schema-fill dropped ..." —
    # accurate on the LLM path, misleading here, where no schema-fill ran to
    # drop anything. Flag the reframe rather than rewording another module's
    # messages. Also defensive: it keeps parse_brief's own return value
    # self-sufficiently correct even for a caller that does not also run the
    # schema through the pipeline's Step 5 (which calls the same injectors
    # again — a no-op the second time, since every brief row is already
    # present by then).
    msgs.append("Offline fill: the lines below are the rescue injectors "
                "reporting a 100% miss against an empty schema — that is the "
                "fill working, not content being lost")
    msgs.extend(inject_missing_concept_entities(schema))
    msgs.extend(inject_missing_concept_scripts(schema))
    msgs.extend(inject_missing_concept_scenes(schema))
    msgs.extend(inject_missing_concept_audio(schema))
    msgs.extend(inject_missing_concept_narrative(schema))
    msgs.extend(inject_missing_concept_title(schema))

    msgs.append(
        f"Deterministic fill: {len(schema.scenes)} scenes, {len(schema.entities)} "
        f"entities, {len(schema.scripts)} scripts, {len(schema.story_beats)} "
        f"beats, {len(schema.audio_tracks)} audio cues, {len(features)} features"
    )
    return schema, msgs
