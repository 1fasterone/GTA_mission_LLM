"""Concept-named title rescue: brief TITLE parsing + override-if-wrong.

Solution #3 (docs/COMPILER_AUDIT.md, Gap B). Regression cover for the
measured live A/B result on coldwater_circuit.txt
(docs/LLM_ENRICHMENT_STRATEGY.md §1.6): the brief's title survived 0/1 — the
LLM arm shipped "FrozenLakeRally" in place of the brief's own
"ColdwaterCircuit".

Title is the one asymmetric rescue in this family: a scalar override-if-wrong
rather than a list append, gated by two confidence tiers so an unstructured
vibe prompt is never forced into a mechanically-bad title (see
src/unity_concept_title.py's own module docstring).
"""
from __future__ import annotations

from src.unity_concept_title import (
    extract_title, inject_missing_concept_title, render_title_checklist,
)
from src.unity_schema import UnitySchema

_STRUCTURED_NO_TITLE_LINE = (
    "ColdwaterCircuit — an ice-racing arcade game.\n"
    "\n"
    "ENTITIES\n"
    "- PlayerCar: the driver's rally car (kind: vehicle)\n"
    "\n"
    "SCENES\n"
    "1. MainMenu — the title screen\n"
)

_EXPLICIT_TITLE = "TITLE: ColdwaterCircuit\n\nA one-line vibe prompt otherwise.\n"

_VIBE_PROMPT = "a cozy game about a lighthouse keeper who talks to gulls"


def _schema(user_prompt: str, project_name: str = "SchemaFillInvented") -> UnitySchema:
    return UnitySchema(
        project_name=project_name, company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        meta={"user_prompt": user_prompt},
    )


# ── parsing ───────────────────────────────────────────────────────────────────

def test_explicit_title_line_wins():
    name, _desc = extract_title(_EXPLICIT_TITLE)
    assert name == "ColdwaterCircuit"


def test_house_first_line_form_is_read_when_no_explicit_title():
    name, _desc = extract_title(_STRUCTURED_NO_TITLE_LINE)
    assert name == "ColdwaterCircuit"


def test_extract_title_always_returns_something():
    """The --no-llm control arm has no LLM to fall back on, so extract_title
    itself never abstains — only the rescue's confidence gates do."""
    name, _desc = extract_title(_VIBE_PROMPT)
    assert name


# ── confidence gates ────────────────────────────────────────────────────────

def test_checklist_fires_only_on_the_explicit_tier():
    assert render_title_checklist(_EXPLICIT_TITLE) != ""
    assert render_title_checklist(_STRUCTURED_NO_TITLE_LINE) == ""


def test_checklist_states_the_required_name():
    out = render_title_checklist(_EXPLICIT_TITLE)
    assert '"ColdwaterCircuit"' in out


# ── rescue ────────────────────────────────────────────────────────────────────

def test_rescue_overrides_a_wrong_explicit_title():
    schema = _schema(_EXPLICIT_TITLE, project_name="FrozenLakeRally")
    msgs = inject_missing_concept_title(schema)
    assert schema.project_name == "ColdwaterCircuit"
    assert msgs and "FrozenLakeRally" in msgs[0] and "ColdwaterCircuit" in msgs[0]


def test_rescue_overrides_a_wrong_title_via_structural_fallback():
    """No explicit TITLE line, but the brief is otherwise structured
    (ENTITIES + SCENES) — the first-line convention is trusted."""
    schema = _schema(_STRUCTURED_NO_TITLE_LINE, project_name="FrozenLakeRally")
    msgs = inject_missing_concept_title(schema)
    assert schema.project_name == "ColdwaterCircuit"
    assert msgs


def test_rescue_is_a_noop_when_the_title_already_matches():
    """Never touches a schema whose title already agrees with the brief —
    same 'append-only unless wrong' spirit as every sibling rescue."""
    schema = _schema(_EXPLICIT_TITLE, project_name="ColdwaterCircuit")
    assert inject_missing_concept_title(schema) == []
    assert schema.project_name == "ColdwaterCircuit"


def test_rescue_abstains_on_an_unstructured_vibe_prompt():
    """No explicit TITLE and no other structured section — forcing a
    mechanical first-line title over the LLM's own invention would very
    plausibly make the result worse, so the rescue must do nothing."""
    schema = _schema(_VIBE_PROMPT, project_name="LighthouseWhispers")
    assert inject_missing_concept_title(schema) == []
    assert schema.project_name == "LighthouseWhispers"


def test_rescue_ignores_an_empty_prompt():
    assert inject_missing_concept_title(_schema("")) == []
