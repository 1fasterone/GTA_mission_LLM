"""Concept-named AUDIO CUE rescue — restores the brief's own "AUDIO" cue
table entries when the Step-3 schema-fill LLM silently drops them.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B) — the sibling of
unity_concept_entities.py / unity_concept_scripts.py for the game's sound.
Same cause as both (see [[step3-sees-only-the-concept]]).

Measured live on prompts/coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief named 10 audio cues
exactly; the LLM arm kept 3 and invented 3 replacements. Entities and
scripts, which already had rescue layers, survived at 100% on the same run —
this closes the same gap for audio cue IDENTITY.

Read the strategy doc's §1.6 "audio result" carefully before treating this
as a pure win, though: the SAME study found the arm that kept all 10 brief
cue ids verbatim (--no-llm) shipped generically-themed audio, because
diegetic cue *synthesis* (src/audio/synth.py) is concept-driven — carrying a
cue's NAME through is not the same as carrying its MEANING. This rescue only
restores identity (the ids the brief demands must exist in
schema.audio_tracks), exactly like the entities/scripts siblings — it is not
a substitute for keeping the LLM concept in the loop for what those cues
should actually sound like.

Deterministic regex parse of the ORIGINAL user_prompt
(schema.meta['user_prompt']), reusing the same section-slicing primitive
src/unity_briefparse.py's --no-llm control arm uses (see
src/unity_brief_sections.py); the cue table's own bullet-fold stays local
here since its "-"-only format has no numbered form for
unity_brief_sections._fold_numbered to share. Append-only: a cue the LLM DID
write is never touched, only the missing ones are added.
"""

from __future__ import annotations

import re

from .unity_brief_sections import _section
from .unity_schema import AudioTrack

# "- freefall_wind_rush     ambient  looping  full terminal-velocity wind roar"
_AUDIO_RE = re.compile(
    r"^([A-Za-z][\w-]*)\s+(ambient|sfx|music|ui|dialogue|voice)\b\s*"
    r"(looping|loop|oneshot)?\s*(.*)$", re.IGNORECASE)

_CHECKLIST_MAX_DESC = 200


def extract_named_audio(user_prompt: str) -> list[AudioTrack]:
    """Parse the brief's own "AUDIO" cue table into AudioTracks, in source
    order. [] when the brief has no recognizable cue table (a prose AUDIO
    section, or no AUDIO section at all).
    """
    section = _section(user_prompt, "AUDIO")
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

    tracks: list[AudioTrack] = []
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


def render_audio_checklist(user_prompt: str) -> str:
    """Render the brief's own AUDIO cue table as a mandatory checklist for
    the Step-3 schema-fill prompt. "" when the brief has no parseable cue
    table.
    """
    named = extract_named_audio(user_prompt)
    if not named:
        return ""
    lines = [
        "REQUIRED AUDIO CUES — the brief names these cues explicitly. Your",
        '"audio_tracks" array MUST contain one entry for EACH line below,',
        "using EXACTLY the name and category given. These ARE the game's",
        "sound: a line you leave out is a cue the player never hears.",
    ]
    for i, t in enumerate(named, start=1):
        head = f'  {i}. name="{t.name}"  category="{t.category}"'
        if t.loop:
            head += "  loop=true"
        lines.append(head)
        if t.description and t.description != t.name.replace("_", " "):
            lines.append(f"     {t.description[:_CHECKLIST_MAX_DESC]}")
    return "\n".join(lines) + "\n"


def inject_missing_concept_audio(schema) -> list[str]:
    """Append an AudioTrack for every brief-named cue the schema-fill step
    dropped. Mutates schema.audio_tracks in place (append-only) and returns
    fixer log messages in the same style as other schema fixers.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    named = extract_named_audio(meta.get("user_prompt", ""))
    if not named:
        return []

    existing = {t.name.strip().lower() for t in schema.audio_tracks}
    added: list[AudioTrack] = []
    for track in named:
        key = track.name.strip().lower()
        if not key or key in existing:
            continue
        added.append(track)
        existing.add(key)

    if not added:
        return []
    schema.audio_tracks = list(schema.audio_tracks) + added
    return [
        f"Concept named {len(added)} audio cue"
        f"{'' if len(added) == 1 else 's'} the schema-fill dropped "
        f"({', '.join(t.name for t in added)}) — restored from the brief's "
        "own AUDIO section instead of shipping a game missing those sounds."
    ]
