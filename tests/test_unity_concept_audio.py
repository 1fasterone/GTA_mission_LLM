"""Concept-named audio-cue rescue: brief "AUDIO" cue-table parsing.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B). Regression cover for the
measured live A/B result on coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief named 10 audio cues
exactly, the LLM arm kept 3 and invented 3 replacements.
"""
from __future__ import annotations

from src.unity_concept_audio import (
    extract_named_audio, inject_missing_concept_audio, render_audio_checklist,
)
from src.unity_schema import AudioTrack, UnitySchema

_AUDIO = (
    "AUDIO\n"
    "- engine_rally_loop      music    looping   engine drone under the whole race\n"
    "- studs_on_black_ice     sfx                metallic bite crossing black ice\n"
    "- finish_flag_crowd      sfx                cheer and flag snap at the line\n"
)


def _schema(user_prompt: str, audio_tracks: list[AudioTrack] | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="ConceptAudioTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        audio_tracks=audio_tracks or [], meta={"user_prompt": user_prompt},
    )


# ── parsing ───────────────────────────────────────────────────────────────────

def test_cue_table_rows_are_parsed():
    tracks = extract_named_audio(_AUDIO)
    assert [t.name for t in tracks] == [
        "engine_rally_loop", "studs_on_black_ice", "finish_flag_crowd"]
    assert tracks[0].category == "music"
    assert tracks[0].loop is True
    assert tracks[1].category == "sfx"
    assert tracks[1].loop is False


def test_description_is_read_after_the_loop_word():
    tracks = extract_named_audio(_AUDIO)
    assert tracks[0].description == "engine drone under the whole race"
    assert tracks[1].description == "metallic bite crossing black ice"


def test_missing_description_falls_back_to_the_name():
    tracks = extract_named_audio("AUDIO\n- coin_pickup sfx\n")
    assert tracks[0].description == "coin pickup"


def test_no_audio_section_returns_empty():
    assert extract_named_audio("a one-line vibe prompt") == []


def test_unrecognised_category_row_is_skipped_not_guessed():
    """A row whose category word isn't in the closed vocabulary doesn't parse
    — the module never invents a category."""
    tracks = extract_named_audio("AUDIO\n- mystery_cue whatever a stray row\n")
    assert tracks == []


# ── checklist ─────────────────────────────────────────────────────────────────

def test_checklist_names_every_cue_verbatim():
    out = render_audio_checklist(_AUDIO)
    for name in ("engine_rally_loop", "studs_on_black_ice", "finish_flag_crowd"):
        assert f'name="{name}"' in out
    assert 'category="music"' in out and "loop=true" in out


def test_checklist_empty_when_no_audio_section():
    assert render_audio_checklist("just a vibe") == ""


# ── rescue ────────────────────────────────────────────────────────────────────

def test_rescue_restores_only_the_dropped_cues():
    schema = _schema(_AUDIO, audio_tracks=[
        AudioTrack(name="engine_rally_loop", description="kept", category="music"),
    ])
    msgs = inject_missing_concept_audio(schema)
    names = [t.name for t in schema.audio_tracks]
    assert names == ["engine_rally_loop", "studs_on_black_ice", "finish_flag_crowd"]
    # Append-only: the LLM's own cue is untouched, not rewritten.
    assert schema.audio_tracks[0].description == "kept"
    assert msgs and "studs_on_black_ice" in msgs[0] and "finish_flag_crowd" in msgs[0]


def test_rescue_is_a_noop_when_the_llm_kept_everything():
    schema = _schema(_AUDIO, audio_tracks=[
        AudioTrack(name=n, description="d", category="sfx")
        for n in ("engine_rally_loop", "studs_on_black_ice", "finish_flag_crowd")
    ])
    assert inject_missing_concept_audio(schema) == []


def test_name_match_is_case_insensitive():
    schema = _schema(_AUDIO, audio_tracks=[
        AudioTrack(name="ENGINE_RALLY_LOOP", description="d", category="music"),
    ])
    inject_missing_concept_audio(schema)
    assert sum(1 for t in schema.audio_tracks
               if t.name.lower() == "engine_rally_loop") == 1


def test_rescue_ignores_a_brief_with_no_audio_section():
    assert inject_missing_concept_audio(_schema("just a vibe")) == []
