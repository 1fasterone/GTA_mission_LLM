"""Concept-named NARRATIVE rescue — restores the brief's own numbered story
beats when the Step-3 schema-fill LLM silently drops them.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B) — the sibling of
unity_concept_entities.py / unity_concept_scripts.py for the story's own
progression. Same cause as both (see [[step3-sees-only-the-concept]]).

Measured live on prompts/coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief numbered 6 story beats; the
LLM arm kept 3. Entities and scripts, which already had rescue layers,
survived at 100% on the same run — this closes the same gap for narrative.
Restoring the beats also feeds Step 5c's mission-loop derivation
(src/unity_playability_repair.py, §6 of the strategy doc), which reads
schema.story_beats to build a win condition when the brief describes its
progression as NARRATIVE rather than MISSIONS — a beat lost here was a beat
that derivation could never see either.

Deterministic regex parse of the ORIGINAL user_prompt
(schema.meta['user_prompt']), reusing the same section/list-folding
primitives src/unity_briefparse.py's --no-llm control arm uses (see
src/unity_brief_sections.py). Append-only: a beat the LLM DID write is never
touched, only the missing ones are added, with fresh ids so a restored beat
can never collide with one the LLM already numbered.
"""

from __future__ import annotations

import re

from .unity_brief_sections import _DASH_SPLIT_RE, _NUMBERED_RE, _fold_numbered, _section
from .unity_schema import StoryBeat

# "(on_enter_zone)" trailing a narrative beat
_TRIGGER_RE = re.compile(r"\((on_start|on_enter_zone|on_timer|on_kill|"
                         r"on_collect|on_death|manual)\)\s*$", re.IGNORECASE)

_BEAT_ID_RE = re.compile(r"^beat_(\d+)$")
_CHECKLIST_MAX_DESC = 300


def extract_named_story_beats(user_prompt: str) -> list[StoryBeat]:
    """Parse the brief's own "NARRATIVE" section into StoryBeats, in source
    order. [] when the brief has no recognizable NARRATIVE section.
    """
    beats: list[StoryBeat] = []
    for entry in _fold_numbered(_section(user_prompt, "NARRATIVE")):
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


def render_narrative_checklist(user_prompt: str) -> str:
    """Render the brief's own NARRATIVE beats as a mandatory checklist for
    the Step-3 schema-fill prompt. "" when the brief has no NARRATIVE
    section.
    """
    named = extract_named_story_beats(user_prompt)
    if not named:
        return ""
    lines = [
        "REQUIRED NARRATIVE BEATS — the brief numbers these story beats",
        'explicitly. Your "story_beats" array MUST contain one entry for',
        "EACH line below, using EXACTLY the label and trigger given. These",
        "ARE the game's progression: a beat you leave out is a moment the",
        "player never sees.",
    ]
    for i, b in enumerate(named, start=1):
        lines.append(f'  {i}. label="{b.label}"  trigger="{b.trigger}"')
        if b.description and b.description != b.label:
            lines.append(f"     {b.description[:_CHECKLIST_MAX_DESC]}")
    return "\n".join(lines) + "\n"


def inject_missing_concept_narrative(schema) -> list[str]:
    """Append a StoryBeat for every brief-numbered beat the schema-fill step
    dropped. Mutates schema.story_beats in place (append-only) and returns
    fixer log messages in the same style as other schema fixers.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    named = extract_named_story_beats(meta.get("user_prompt", ""))
    if not named:
        return []

    existing = {b.label.strip().lower() for b in schema.story_beats}
    # Fresh ids continue after whatever the LLM already numbered, so a
    # restored beat_02 can never collide with one the fill already wrote.
    next_idx = 1
    for b in schema.story_beats:
        m = _BEAT_ID_RE.match(b.id or "")
        if m:
            next_idx = max(next_idx, int(m.group(1)) + 1)

    added: list[StoryBeat] = []
    for beat in named:
        key = beat.label.strip().lower()
        if not key or key in existing:
            continue
        added.append(StoryBeat(id=f"beat_{next_idx:02d}", label=beat.label,
                               description=beat.description, trigger=beat.trigger))
        existing.add(key)
        next_idx += 1

    if not added:
        return []
    schema.story_beats = list(schema.story_beats) + added
    return [
        f"Concept named {len(added)} narrative beat"
        f"{'' if len(added) == 1 else 's'} the schema-fill dropped "
        f"({', '.join(b.label for b in added)}) — restored from the "
        "brief's own NARRATIVE section instead of shipping a story missing "
        "those moments."
    ]
