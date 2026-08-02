"""Concept-named script rescue: brief "SCRIPTS" bullet parsing.

Regression cover for a live E2E failure (Grandma's Antique Shop): the brief
required 12 systems, each "as its own module", and 7 of them reached no schema
at all — ProvenanceSystem, InsuranceAudit, MuseumMetaProgression,
NightlyShopReshuffle, YelpNewGamePlus, PhotoMode, ShuffleDodgeControls. Same
cause as the entity leak this module's sibling closes: Step 3 sees only the
Step-2 concept, so a brief's explicit demands were never shown to the model
that had to satisfy them.
"""
from __future__ import annotations

from src.unity_concept_scripts import (
    extract_named_scripts, inject_missing_concept_scripts,
    render_script_checklist,
)
from src.unity_schema import UnityScript, UnitySchema

# The house SCRIPTS format, wrapped at ~80 cols with the type in an aligned
# column — exactly as prompts/grandmas_antique_shop.txt writes it.
_GRANDMA_SCRIPTS = (
    "SCRIPTS\n"
    "- ShuffleDodgeControls  (player)         one-button dodge on tap, menacing\n"
    "                                         shuffle on hold\n"
    "- MirrorSpawning        (custom)         raiders enter only through\n"
    "                                         AntiqueMirror entities\n"
    "- MuseumMetaProgression (saveload)       donates the best items on death\n"
    "- ShopCamera            (camera)         Cinemachine third-person rig\n"
    "\n"
    "NARRATIVE\n"
    "1. Closing Time — Grandma Edna locks up\n"
)


def _schema(user_prompt: str, scripts: list[UnityScript] | None = None) -> UnitySchema:
    return UnitySchema(
        project_name="ConceptScriptTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        scripts=scripts or [], meta={"user_prompt": user_prompt},
    )


# ── parsing ───────────────────────────────────────────────────────────────────

def test_wrapped_bullet_keeps_name_type_and_folded_description():
    scripts = extract_named_scripts(_GRANDMA_SCRIPTS)
    assert [s["name"] for s in scripts] == [
        "ShuffleDodgeControls", "MirrorSpawning",
        "MuseumMetaProgression", "ShopCamera"]
    assert scripts[0]["type"] == "player"
    # The wrapped continuation line is folded back into one description.
    assert scripts[0]["description"] == (
        "one-button dodge on tap, menacing shuffle on hold")


def test_unknown_type_normalises_to_custom():
    """"saveload" is not a ScriptType — UnityScript would coerce it, so the
    checklist must not ask the model for a value the field cannot hold."""
    scripts = extract_named_scripts(_GRANDMA_SCRIPTS)
    assert scripts[2]["type"] == "custom"


def test_parse_stops_at_the_next_section_header():
    """NARRATIVE ends the list — its numbered lines are not script bullets."""
    assert all("Closing Time" not in s["description"]
               for s in extract_named_scripts(_GRANDMA_SCRIPTS))


def test_section_header_with_a_colon_ends_the_list():
    """dropzone.txt writes "ENTITIES:" with a colon. The entity module's own
    header regex has no ":" in it, so without allowing one here the entity
    bullets below would be folded in and read as scripts."""
    brief = ("SCRIPTS\n"
             "- GameManager (game_manager)  run state machine\n"
             "ENTITIES:\n"
             "- Player: lead skydiver in jumpsuit\n")
    scripts = extract_named_scripts(brief)
    assert [s["name"] for s in scripts] == ["GameManager"]


def test_colon_bullet_form_is_accepted():
    brief = ("SCRIPTS\n"
             "- InsuranceAudit: totals destroyed stock (type: custom)\n")
    scripts = extract_named_scripts(brief)
    assert scripts[0]["name"] == "InsuranceAudit"
    assert scripts[0]["type"] == "custom"
    assert "type:" not in scripts[0]["description"]


def test_no_scripts_section_is_not_an_error():
    assert extract_named_scripts("a one-line vibe prompt about grandmas") == []
    assert render_script_checklist("a one-line vibe prompt") == ""


def test_bullet_with_no_description_still_gets_one():
    """UnityScript.description is required — a name-and-type-only bullet must
    not produce an invalid spec."""
    scripts = extract_named_scripts("SCRIPTS\n- PhotoMode (custom)\n")
    assert scripts[0]["description"]
    inject_missing_concept_scripts(_schema("SCRIPTS\n- PhotoMode (custom)\n"))


# ── checklist ─────────────────────────────────────────────────────────────────

def test_checklist_names_every_script_verbatim():
    out = render_script_checklist(_GRANDMA_SCRIPTS)
    for name in ("ShuffleDodgeControls", "MirrorSpawning",
                 "MuseumMetaProgression", "ShopCamera"):
        assert f'name="{name}"' in out
    assert "MUST contain one entry" in out


def test_checklist_states_the_type_the_schema_would_store():
    out = render_script_checklist(_GRANDMA_SCRIPTS)
    assert 'name="MuseumMetaProgression"  type="custom"' in out
    assert "saveload" not in out


# ── rescue ────────────────────────────────────────────────────────────────────

def test_rescue_restores_only_the_dropped_scripts():
    schema = _schema(_GRANDMA_SCRIPTS, scripts=[
        UnityScript(name="MirrorSpawning", type="custom", description="kept"),
    ])
    msgs = inject_missing_concept_scripts(schema)
    names = [s.name for s in schema.scripts]
    assert names == ["MirrorSpawning", "ShuffleDodgeControls",
                     "MuseumMetaProgression", "ShopCamera"]
    # Append-only: the LLM's own script is untouched, not rewritten.
    assert schema.scripts[0].description == "kept"
    assert msgs and "ShuffleDodgeControls" in msgs[0]


def test_rescue_is_a_noop_when_the_llm_kept_everything():
    schema = _schema(_GRANDMA_SCRIPTS, scripts=[
        UnityScript(name=n, type="custom", description="d")
        for n in ("ShuffleDodgeControls", "MirrorSpawning",
                  "MuseumMetaProgression", "ShopCamera")
    ])
    assert inject_missing_concept_scripts(schema) == []
    assert len(schema.scripts) == 4


def test_name_match_is_case_insensitive():
    schema = _schema(_GRANDMA_SCRIPTS, scripts=[
        UnityScript(name="shopcamera", type="camera", description="d"),
    ])
    inject_missing_concept_scripts(schema)
    assert sum(1 for s in schema.scripts if s.name.lower() == "shopcamera") == 1


def test_taken_singleton_type_demotes_to_custom():
    """Two scripts of one non-custom type attach twice to the same object. The
    module still gets restored — with its name and description — as custom."""
    schema = _schema(_GRANDMA_SCRIPTS, scripts=[
        UnityScript(name="MainCameraRig", type="camera", description="d"),
    ])
    msgs = inject_missing_concept_scripts(schema)
    restored = next(s for s in schema.scripts if s.name == "ShopCamera")
    assert restored.type == "custom"
    assert any("camera->custom" in m for m in msgs)
    # A type NOT already taken keeps the briefed value.
    assert next(s for s in schema.scripts if s.name == "ShuffleDodgeControls").type == "player"


def test_attach_target_follows_the_type():
    schema = _schema(_GRANDMA_SCRIPTS)
    inject_missing_concept_scripts(schema)
    by_name = {s.name: s for s in schema.scripts}
    assert by_name["ShuffleDodgeControls"].attach_to == "player"
    assert by_name["ShopCamera"].attach_to == "camera"
    assert by_name["MirrorSpawning"].attach_to == "systems"


def test_schema_cap_is_respected_and_named_loudly():
    """schema.scripts is capped at 24. Overflow must be reported by name, not
    silently truncated — abstain loudly rather than ship 24 of 27."""
    brief = "SCRIPTS\n" + "".join(
        f"- Sys{i:02d} (custom)  system {i}\n" for i in range(30))
    schema = _schema(brief)
    msgs = inject_missing_concept_scripts(schema)
    assert len(schema.scripts) == 24
    overflow = [m for m in msgs if "capped at 24" in m]
    assert overflow and "Sys24" in overflow[0]


def test_rescue_ignores_a_brief_with_no_scripts_section():
    assert inject_missing_concept_scripts(_schema("just a vibe")) == []
