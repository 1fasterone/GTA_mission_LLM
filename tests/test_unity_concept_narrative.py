"""Concept-named narrative-beat rescue: brief "NARRATIVE" entry parsing.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B). Regression cover for the
measured live A/B result on coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief numbered 6 story beats
exactly, the LLM arm kept 3.
"""
from __future__ import annotations

from src.unity_concept_narrative import (
    extract_named_story_beats, inject_missing_concept_narrative,
    render_narrative_checklist,
)
from src.unity_schema import StoryBeat, UnitySchema

_NARRATIVE = (
    "NARRATIVE\n"
    "1. Practice Laps — the driver warms up on packed snow (on_start)\n"
    "2. Black Ice Warning — the crew radios a hazard ahead\n"
    "3. Crossing the Bay — the car slides across open black ice (on_enter_zone)\n"
)


def _schema(user_prompt: str, story_beats: list[StoryBeat] | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="ConceptNarrativeTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        story_beats=story_beats or [], meta={"user_prompt": user_prompt},
    )


# ── parsing ───────────────────────────────────────────────────────────────────

def test_beats_are_numbered_and_labeled():
    beats = extract_named_story_beats(_NARRATIVE)
    assert [b.label for b in beats] == [
        "Practice Laps", "Black Ice Warning", "Crossing the Bay"]
    assert [b.id for b in beats] == ["beat_01", "beat_02", "beat_03"]


def test_trigger_is_read_from_the_trailing_parenthetical():
    beats = extract_named_story_beats(_NARRATIVE)
    assert beats[0].trigger == "on_start"
    assert beats[2].trigger == "on_enter_zone"


def test_missing_trigger_defaults_to_manual():
    beats = extract_named_story_beats(_NARRATIVE)
    assert beats[1].trigger == "manual"


def test_description_falls_back_to_the_label():
    beats = extract_named_story_beats("NARRATIVE\n1. Closing Time\n")
    assert beats[0].label == beats[0].description == "Closing Time"


def test_no_narrative_section_returns_empty():
    assert extract_named_story_beats("a one-line vibe prompt") == []


def test_parse_stops_at_the_next_section_header():
    brief = _NARRATIVE + "\nAUDIO\n- engine_loop music looping the engine sound\n"
    assert all("engine" not in b.description.lower()
               for b in extract_named_story_beats(brief))


# ── checklist ─────────────────────────────────────────────────────────────────

def test_checklist_names_every_beat_verbatim():
    out = render_narrative_checklist(_NARRATIVE)
    for label in ("Practice Laps", "Black Ice Warning", "Crossing the Bay"):
        assert f'label="{label}"' in out
    assert 'trigger="on_start"' in out


def test_checklist_empty_when_no_narrative_section():
    assert render_narrative_checklist("just a vibe") == ""


# ── rescue ────────────────────────────────────────────────────────────────────

def test_rescue_restores_only_the_dropped_beats():
    schema = _schema(_NARRATIVE, story_beats=[
        StoryBeat(id="beat_01", label="Practice Laps", description="kept"),
    ])
    msgs = inject_missing_concept_narrative(schema)
    labels = [b.label for b in schema.story_beats]
    assert labels == ["Practice Laps", "Black Ice Warning", "Crossing the Bay"]
    # Append-only: the LLM's own beat is untouched, not rewritten.
    assert schema.story_beats[0].description == "kept"
    assert msgs and "Black Ice Warning" in msgs[0] and "Crossing the Bay" in msgs[0]


def test_rescue_is_a_noop_when_the_llm_kept_everything():
    schema = _schema(_NARRATIVE, story_beats=[
        StoryBeat(id=f"beat_{i:02d}", label=label, description="d")
        for i, label in enumerate(
            ("Practice Laps", "Black Ice Warning", "Crossing the Bay"), start=1)
    ])
    assert inject_missing_concept_narrative(schema) == []


def test_restored_beat_ids_never_collide_with_existing_ones():
    """A brief's own beat_01/02/03 numbering must not collide with whatever
    the LLM already wrote under those same ids for DIFFERENT beats."""
    schema = _schema(_NARRATIVE, story_beats=[
        StoryBeat(id="beat_01", label="An LLM-invented opener", description="d"),
    ])
    inject_missing_concept_narrative(schema)
    ids = [b.id for b in schema.story_beats]
    assert len(ids) == len(set(ids)), f"duplicate beat ids: {ids}"
    assert ids[0] == "beat_01"  # the LLM's own beat is untouched


def test_label_match_is_case_insensitive():
    schema = _schema(_NARRATIVE, story_beats=[
        StoryBeat(id="beat_01", label="practice laps", description="d"),
    ])
    inject_missing_concept_narrative(schema)
    assert sum(1 for b in schema.story_beats
               if b.label.lower() == "practice laps") == 1


def test_rescue_ignores_a_brief_with_no_narrative_section():
    assert inject_missing_concept_narrative(_schema("just a vibe")) == []
