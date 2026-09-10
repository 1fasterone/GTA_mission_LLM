"""Objectives micro — step order (`after`) coverage.

Before this, ObjectivesSchema was a flat list: `ActiveObjective` (Objectives.cs.j2)
already displayed "the first incomplete required objective" as the current step,
but `ReportProgress` accepted progress toward ANY objective at ANY time — a
source wired earlier (SEAM 8: an eliminate objective's spawner, say) could bank
progress on a later step before the player had reached it, disagreeing with what
the HUD showed. `after` (mirroring board.py's zone `requires`) closes that gap:
deterministically filled from list order among non-optional objectives unless
the brief already named an order, never overriding an explicit value.

Offline and deterministic — no LLM, no Unity, no network.
"""
from __future__ import annotations

from pathlib import Path

from src.csharp_syntax_gate import check_files
from src.micro.code_fields import CodeMethod
from src.micro.objectives import ObjectiveEntry, ObjectivesPipeline, ObjectivesSchema

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    """Mirrors tests/test_controller_multi_mode.py's _cfg(): config.yaml is
    gitignored and absent in a fresh CI checkout, so the pipeline ctor gets a
    literal dict with just the keys it reads instead of a real file read."""
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":  str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


def _schema(objectives) -> ObjectivesSchema:
    return ObjectivesSchema(
        name="TestObjectives", title="Test Mission",
        objectives=[ObjectiveEntry(**o) for o in objectives])


def _fixed(schema: ObjectivesSchema) -> ObjectivesSchema:
    return ObjectivesPipeline(_cfg()).default_fix(schema, "tracker")


def _harden(schema: ObjectivesSchema) -> list[str]:
    return ObjectivesPipeline(_cfg()).harden(schema)


def _render(schema: ObjectivesSchema) -> str:
    pipe = ObjectivesPipeline(_cfg())
    fixed = pipe.default_fix(schema, "tracker")
    env = pipe.jinja()
    return env.get_template("Objectives.cs.j2").render(s=fixed, variant="tracker")


# ── default_fix: deterministic chain ────────────────────────────────────────

def test_blank_after_chains_onto_the_previous_required_objective():
    s = _fixed(_schema([
        {"id": "a", "type": "reach"},
        {"id": "b", "type": "collect"},
        {"id": "c", "type": "eliminate"},
    ]))
    by_id = {o.id: o for o in s.objectives}
    assert by_id["a"].after == ""
    assert by_id["b"].after == "a"
    assert by_id["c"].after == "b"


def test_explicit_after_is_never_overridden():
    """Fill-only, same contract as locomotion_modes / flying_abilities: an
    author's own choice — including a non-adjacent id — always wins."""
    s = _fixed(_schema([
        {"id": "a", "type": "reach"},
        {"id": "b", "type": "collect"},
        {"id": "c", "type": "eliminate", "after": "a"},   # skips b deliberately
    ]))
    by_id = {o.id: o for o in s.objectives}
    assert by_id["c"].after == "a"


def test_optional_objectives_are_not_auto_chained_and_do_not_break_the_chain():
    s = _fixed(_schema([
        {"id": "a", "type": "reach"},
        {"id": "side", "type": "collect", "optional": True},
        {"id": "b", "type": "eliminate"},
    ]))
    by_id = {o.id: o for o in s.objectives}
    assert by_id["side"].after == ""          # untouched — never part of the chain
    assert by_id["b"].after == "a"            # chains past the optional entry


def test_optional_objective_keeps_its_own_explicit_after():
    """An optional objective may still declare its own prerequisite by hand —
    only the auto-CHAIN skips optionals, not the fill-only respect for an
    explicit value."""
    s = _fixed(_schema([
        {"id": "a", "type": "reach"},
        {"id": "side", "type": "collect", "optional": True, "after": "a"},
    ]))
    by_id = {o.id: o for o in s.objectives}
    assert by_id["side"].after == "a"


# ── harden(): loud abstention, never a silent drop ──────────────────────────

def test_harden_flags_a_dangling_after():
    s = _schema([{"id": "a", "type": "reach", "after": "nope"}])
    msgs = _harden(s)
    assert any("nope" in m and "not an objective" in m for m in msgs)


def test_harden_flags_self_reference():
    s = _schema([{"id": "a", "type": "reach", "after": "a"}])
    msgs = _harden(s)
    assert any("after itself" in m for m in msgs)


def test_harden_flags_a_cycle():
    s = _schema([
        {"id": "a", "type": "reach", "after": "b"},
        {"id": "b", "type": "collect", "after": "a"},
    ])
    msgs = _harden(s)
    assert any("cycle" in m for m in msgs)


def test_harden_is_quiet_on_a_well_formed_chain():
    s = _fixed(_schema([
        {"id": "a", "type": "reach"},
        {"id": "b", "type": "collect"},
    ]))
    assert _harden(s) == []


# ── Rendered C# ──────────────────────────────────────────────────────────────

def test_rendered_script_carries_after_and_gates_report_progress(tmp_path):
    cs = _render(_schema([
        {"id": "a", "type": "reach"},
        {"id": "b", "type": "collect"},
    ]))
    assert 'after = "a"' in cs.split('id = "b"')[1].split("},")[0] \
        or 'after = "a"' in cs   # tolerate formatting drift, id="b" entry carries after="a"
    assert "public bool ReportProgress(string objectiveId" in cs
    assert "OnObjectiveBlocked" in cs
    assert "prereqDone" in cs

    out = tmp_path / "Objectives.cs"
    out.write_text(cs, encoding="utf-8")
    result = check_files([str(out)])
    assert result.ok, f"Roslyn syntax gate found: {result.diagnostics}"


def test_rendered_script_compiles_with_a_cycle_present(tmp_path):
    """A cycle is a build-time FIX warning (harden), never a codegen failure —
    the schema still renders valid, if permanently unwinnable, C#."""
    cs = _render(_schema([
        {"id": "a", "type": "reach", "after": "b"},
        {"id": "b", "type": "collect", "after": "a"},
    ]))
    out = tmp_path / "Objectives.cs"
    out.write_text(cs, encoding="utf-8")
    result = check_files([str(out)])
    assert result.ok, f"Roslyn syntax gate found: {result.diagnostics}"


# ── reserved private field neutralization ───────────────────────────────────
# default_fix's neutralize_unavailable_names(schema, {"_surviveTimer",
# "_missionComplete", "_byId"}) call (objectives.py, ~line 227). These are
# Objectives.cs.j2's own private bookkeeping — never a legal target for
# authored code, which already has a public surface for the same information
# (ReportProgress, MissionComplete, ActiveObjective). The generic helper
# itself is covered by tests/test_hud_e2e_stress.py; these confirm THIS call
# site is actually wired with THIS exact reserved-name set.

_AUDIT_MARKER = "AUDIT: removed reference to unavailable field"


def test_authored_update_extra_referencing_survive_timer_is_neutralized():
    schema = _schema([{"id": "a", "type": "reach"}])
    schema.update_extra = "_surviveTimer -= Time.deltaTime;"
    fixed = _fixed(schema)
    assert _AUDIT_MARKER in fixed.update_extra
    live_code = "\n".join(line for line in fixed.update_extra.splitlines()
                          if not line.lstrip().startswith("//"))
    assert "_surviveTimer" not in live_code


def test_authored_awake_extra_referencing_mission_complete_is_neutralized():
    schema = _schema([{"id": "a", "type": "reach"}])
    schema.awake_extra = "if (_missionComplete) return;"
    fixed = _fixed(schema)
    assert _AUDIT_MARKER in fixed.awake_extra


def test_authored_extra_method_referencing_by_id_is_neutralized():
    schema = _schema([{"id": "a", "type": "reach"}])
    schema.extra_methods = [CodeMethod(
        signature="private void CustomLookup()",
        body='var o = _byId["a"];')]
    fixed = _fixed(schema)
    body = fixed.extra_methods[0].body
    assert _AUDIT_MARKER in body
    live_code = "\n".join(line for line in body.splitlines()
                          if not line.lstrip().startswith("//"))
    assert "_byId" not in live_code


def test_legitimate_public_surface_references_are_left_untouched():
    """ReportProgress/MissionComplete/ActiveObjective are the intended public
    surface for the same information _surviveTimer/_missionComplete/_byId
    hold privately — authored code using them must not be touched."""
    code = 'if (ActiveObjective != null && !MissionComplete) ReportProgress("a", 1);'
    schema = _schema([{"id": "a", "type": "reach"}])
    schema.update_extra = code
    fixed = _fixed(schema)
    assert fixed.update_extra == code
    assert "AUDIT" not in fixed.update_extra
