"""Concept-named scene rescue: brief "SCENES" entry parsing.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B). Regression cover for the
measured live A/B result on coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief named 5 scenes exactly, the
LLM arm kept 1 and invented 4 replacements.
"""
from __future__ import annotations

from src.unity_concept_scenes import (
    extract_named_scenes, inject_missing_concept_scenes,
    render_scene_checklist,
)
from src.unity_schema import SceneSpec, UnitySchema

# The numbered house form, wrapped continuation included — as prompts/dropzone.txt writes it.
_DROPZONE_SCENES = (
    "SCENES\n"
    "1. MainMenu         — animated title: skydiver silhouette against\n"
    "                       sunset, tap to jump\n"
    "2. AircraftInterior — the plane's open door, wind roar builds\n"
    "3. Freefall         — terminal velocity descent through cloud layers\n"
)

# The bulleted house form — as prompts/turbo_toys.txt / grandmas_antique_shop.txt write it.
_BULLETED_SCENES = (
    "SCENES\n"
    "- Sunny Speedway: the first circuit — a looping oval through a cartoon\n"
    "  toy town\n"
    "- Night Track: raced after dusk under string lights\n"
)


def _schema(user_prompt: str, scenes: list[str] | None = None,
           scene_specs: list[SceneSpec] | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="ConceptSceneTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=scenes or ["Main"], packages=[],
        scene_specs=scene_specs or [], meta={"user_prompt": user_prompt},
    )


# ── parsing ───────────────────────────────────────────────────────────────────

def test_numbered_house_form_keeps_wrapped_description():
    scenes = extract_named_scenes(_DROPZONE_SCENES)
    assert [s.name for s in scenes] == ["MainMenu", "AircraftInterior", "Freefall"]
    assert scenes[0].description == "animated title: skydiver silhouette against sunset, tap to jump"


def test_bulleted_house_form_is_also_accepted():
    scenes = extract_named_scenes(_BULLETED_SCENES)
    assert [s.name for s in scenes] == ["SunnySpeedway", "NightTrack"]


def test_dash_form_description_keeps_its_own_colon():
    """Numbered entries split name/body on the em dash FIRST, so a dash-form
    description containing a colon keeps its whole body (regression: trying
    the colon split first truncated this at "animated title")."""
    scenes = extract_named_scenes(_DROPZONE_SCENES)
    assert "skydiver silhouette" in scenes[0].description


def test_time_of_day_is_read_from_the_description():
    """_time_of_day() itself reads "dusk"; SceneSpec's own field validator
    then canonicalizes that to its stored vocabulary ("sunset")."""
    scenes = extract_named_scenes(_BULLETED_SCENES)
    night = next(s for s in scenes if s.name == "NightTrack")
    assert night.time_of_day == "sunset"


def test_no_scenes_section_returns_empty():
    assert extract_named_scenes("a one-line vibe prompt") == []
    assert extract_named_scenes("") == []


def test_section_header_ends_the_list():
    brief = "SCENES\n1. MainMenu — the title screen\n\nAUDIO\n- coin sfx a pickup\n"
    assert [s.name for s in extract_named_scenes(brief)] == ["MainMenu"]


# ── checklist ─────────────────────────────────────────────────────────────────

def test_checklist_names_every_scene_verbatim():
    out = render_scene_checklist(_DROPZONE_SCENES)
    for name in ("MainMenu", "AircraftInterior", "Freefall"):
        assert f'name="{name}"' in out
    assert "MUST contain one entry" in out


def test_checklist_empty_when_no_scenes_section():
    assert render_scene_checklist("just a vibe") == ""


# ── rescue ────────────────────────────────────────────────────────────────────

def test_rescue_restores_only_the_dropped_scenes():
    schema = _schema(_DROPZONE_SCENES, scenes=["Main", "MainMenu"],
                     scene_specs=[SceneSpec(name="MainMenu", description="kept")])
    msgs = inject_missing_concept_scenes(schema)
    assert set(schema.scenes) == {"Main", "MainMenu", "AircraftInterior", "Freefall"}
    names = {s.name for s in schema.scene_specs}
    assert names == {"MainMenu", "AircraftInterior", "Freefall"}
    # Append-only: the LLM's own spec is untouched, not rewritten.
    kept = next(s for s in schema.scene_specs if s.name == "MainMenu")
    assert kept.description == "kept"
    assert msgs and "AircraftInterior" in msgs[0] and "Freefall" in msgs[0]


def test_rescue_is_a_noop_when_the_llm_kept_everything():
    schema = _schema(_DROPZONE_SCENES,
                     scenes=["Main", "MainMenu", "AircraftInterior", "Freefall"],
                     scene_specs=[SceneSpec(name=n) for n in
                                  ("MainMenu", "AircraftInterior", "Freefall")])
    assert inject_missing_concept_scenes(schema) == []


def test_name_match_is_case_insensitive():
    schema = _schema(_DROPZONE_SCENES, scenes=["Main", "mainmenu"],
                     scene_specs=[SceneSpec(name="mainmenu")])
    inject_missing_concept_scenes(schema)
    assert sum(1 for n in schema.scenes if n.lower() == "mainmenu") == 1


def test_restored_scene_hosts_every_entity_group():
    """The brief's SCENES section never states a per-scene entity subset —
    a restored spec must default to entities=None (host everything), not an
    empty scene."""
    schema = _schema(_DROPZONE_SCENES)
    inject_missing_concept_scenes(schema)
    restored = next(s for s in schema.scene_specs if s.name == "MainMenu")
    assert restored.entities is None


def test_schema_cap_is_respected_and_named_loudly():
    """schema.scenes is capped at 8 total. The budget already accounts for
    the "Main" scene occupying one slot, so a 10-scene brief against a
    schema that starts with only "Main" gets 7 restored (7 + Main = 8) and
    3 named as overflow, not silently truncated."""
    brief = "SCENES\n" + "".join(
        f"{i}. Scene{i:02d} — an area\n" for i in range(1, 11))
    schema = _schema(brief, scenes=["Main"], scene_specs=[])
    msgs = inject_missing_concept_scenes(schema)
    assert len(schema.scenes) == 8
    assert len(schema.scene_specs) == 7
    overflow = [m for m in msgs if "capped at 8" in m]
    assert overflow and "Scene08" in overflow[0]


def test_rescue_ignores_a_brief_with_no_scenes_section():
    assert inject_missing_concept_scenes(_schema("just a vibe")) == []
