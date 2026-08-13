"""
statemachine — the emitted FSM is built from the AUTHORED graph.

The same defect the `ai` micro carried, in the micro the audit deferred: state
names reach a generated enum that a switch dispatches on (CS0117), transition
conditions reach emitted C# verbatim (CS0103), and SEAM 4's animator bridge
hardcoded `State.Idle/Moving/Jumping/Dead` — correct only for player_fsm's
default state set, and wrong the moment the FSM is an enemy_fsm or the model
names its own states.

These tests pin that the model's vocabulary survives into the emitted code while
never being able to break the build, and that an undeclared identifier in an
authored condition is DECLARED rather than dropped — the model's intent is the
part worth keeping.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.micro.statemachine import (StateDefinition, StateMachinePipeline,
                                    StateMachineSchema, StateTransition)
from src.unity_compose import MicroComposer, _anim_clip_for_state, subsystems_for
from src.unity_schema import EntitySpec

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dummy_config():
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":   str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


def _fix(schema, variant="enemy_fsm"):
    return StateMachinePipeline.default_fix(
        StateMachinePipeline.__new__(StateMachinePipeline), schema, variant)


def _render(cfg, schema, variant, tmp_path) -> str:
    p = StateMachinePipeline(cfg)
    p.write_files(schema, variant, tmp_path)
    return (tmp_path / "Scripts" / f"{schema.name}.cs").read_text(encoding="utf-8")


# ── State names: sanitised, never rejected ────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("prowl the deck", "ProwlTheDeck"),
    ("spooked",        "Spooked"),
    ("switch",         "SwitchState"),      # C# keyword
    ("2nd sweep",      "State2ndSweep"),    # cannot start with a digit
])
def test_state_names_are_sanitised(raw, expected):
    s = _fix(StateMachineSchema(name="S", variant="enemy_fsm",
                                states=[StateDefinition(name=raw),
                                        StateDefinition(name="Lunge")]))
    assert expected in {st.name for st in s.states}


def test_every_switch_label_is_a_declared_enum_member(dummy_config, tmp_path):
    """The CS0117 invariant: enum and switch render from ONE sanitised list."""
    s = _fix(StateMachineSchema(name="Sentry", variant="enemy_fsm", states=[
        StateDefinition(name="prowl the deck"), StateDefinition(name="Spooked"),
        StateDefinition(name="Lunge")]))
    cs = _render(dummy_config, s, "enemy_fsm", tmp_path)
    declared = {st.name for st in s.states}
    code = "\n".join(ln for ln in cs.splitlines()
                     if not ln.lstrip().startswith(("//", "///")))
    for ref in set(re.findall(r"State\.(\w+)", code)):
        assert ref in declared, f"{ref} referenced but not declared"


def test_states_colliding_after_sanitising_are_dropped_once():
    s = _fix(StateMachineSchema(name="S", variant="enemy_fsm", states=[
        StateDefinition(name="prowl deck"), StateDefinition(name="Prowl Deck"),
        StateDefinition(name="Lunge")]))
    assert [st.name for st in s.states].count("ProwlDeck") == 1


# ── The template's own contract ───────────────────────────────────────────────

def test_player_fsm_keeps_the_states_its_template_hardcodes():
    """StateMachine.cs.j2 gives player_fsm a hardcoded CheckTransitions() and a
    Kill() that transitions to State.Dead — and SEAM 2 calls Kill(). Renaming
    those four out of the enum is CS0117 in template-owned code."""
    s = _fix(StateMachineSchema(name="P", variant="player_fsm", states=[
        StateDefinition(name="Drifting"), StateDefinition(name="Spinning")]),
        "player_fsm")
    names = {st.name for st in s.states}
    assert {"Idle", "Moving", "Jumping", "Dead"} <= names
    assert {"Drifting", "Spinning"} <= names      # the model's own states remain


def test_player_fsm_dead_is_not_reported_unreachable(capsys):
    """Kill() reaches Dead without a transition, so flagging it would be a false
    alarm on every single build."""
    _fix(StateMachineSchema(name="P", variant="player_fsm"), "player_fsm")
    assert "no transition reaches" not in capsys.readouterr().out


def test_enemy_fsm_can_actually_die():
    """enemy_fsm has no Kill(), so without a -> Dead edge the terminal state it
    ships is one no enemy could ever enter."""
    s = _fix(StateMachineSchema(name="E", variant="enemy_fsm"))
    assert any(t.to_state == "Dead" for t in s.transitions)
    assert "_hp" in {f.name for f in s.extra_fields}


# ── Conditions: an undeclared identifier is DECLARED, not dropped ─────────────

def test_undeclared_identifier_is_declared_with_an_inferred_type():
    s = _fix(StateMachineSchema(name="S", variant="enemy_fsm",
        states=[StateDefinition(name="Prowl"), StateDefinition(name="Lunge")],
        transitions=[StateTransition(from_state="Prowl", to_state="Lunge",
                                     condition="_heardNoise && _alertTimer > 2f")]))
    fields = {f.name: f.type for f in s.extra_fields}
    assert fields["_alertTimer"] == "float"   # compared to a number
    assert fields["_heardNoise"] == "bool"    # used bare
    # And the model's condition survives verbatim rather than being dropped.
    assert any(t.condition == "_heardNoise && _alertTimer > 2f"
               for t in s.transitions)


def test_condition_calling_an_invented_method_drops_the_transition():
    """A field can be declared; a method body cannot be invented."""
    s = _fix(StateMachineSchema(name="S", variant="enemy_fsm",
        states=[StateDefinition(name="Prowl"), StateDefinition(name="Lunge")],
        transitions=[StateTransition(from_state="Prowl", to_state="Lunge",
                                     condition="SeesPlayer()")]))
    assert not any(t.from_state == "Prowl" and t.to_state == "Lunge"
                   for t in s.transitions)


def test_dangling_and_self_loop_transitions_are_dropped():
    s = _fix(StateMachineSchema(name="S", variant="enemy_fsm",
        states=[StateDefinition(name="Prowl"), StateDefinition(name="Lunge")],
        transitions=[
            StateTransition(from_state="Prowl", to_state="Nowhere", condition="true"),
            StateTransition(from_state="Ghost", to_state="Lunge", condition="true"),
            StateTransition(from_state="Prowl", to_state="Prowl", condition="true"),
            StateTransition(from_state="Prowl", to_state="Lunge", condition="true")]))
    assert [(t.from_state, t.to_state) for t in s.transitions] == [("Prowl", "Lunge")]


def test_initial_state_must_name_a_real_state():
    s = _fix(StateMachineSchema(name="S", variant="enemy_fsm",
        states=[StateDefinition(name="Prowl"), StateDefinition(name="Lunge")],
        initial_state="Sleeping"))
    assert s.initial_state in {st.name for st in s.states}


def test_emitted_conditions_reference_only_declared_members(dummy_config, tmp_path):
    s = _fix(StateMachineSchema(name="Sentry", variant="enemy_fsm",
        states=[StateDefinition(name="Prowl"), StateDefinition(name="Lunge")],
        transitions=[StateTransition(from_state="Prowl", to_state="Lunge",
                                     condition="_spotted && _range < 4f")]))
    cs = _render(dummy_config, s, "enemy_fsm", tmp_path)
    for name in ("_spotted", "_range"):
        assert f"private bool {name}" in cs or f"private float {name}" in cs
        assert f"({name}" in cs or f" {name}" in cs


# ── SEAM 4 renders from the FSM's real states ─────────────────────────────────

@pytest.mark.parametrize("state,clip", [
    ("Idle", "Idle"), ("Moving", "Run"), ("Jumping", "Jump"), ("Dead", "Death"),
    ("Patrol", "Run"), ("Lunge", "Attack"), ("Prowl", "Run"),
    ("Xyzzy", "Xyzzy"),   # unmatched keeps the model's own word
])
def test_animation_clip_mapping(state, clip):
    assert _anim_clip_for_state(state) == clip


def _gamewiring(tmp_path, **kw) -> str:
    subs = subsystems_for("3D", kw.pop("features", []), **kw)
    assets = Path(tmp_path) / "Assets"
    MicroComposer("FsmProof", project_config={"audio": {"backend": "dsp"}}).compose(
        subs, assets)
    return (assets / "Micros" / "wiring" / "Scripts"
            / "GameWiring.cs").read_text(encoding="utf-8")


def test_seam4_emits_a_case_per_declared_state(tmp_path):
    wiring = _gamewiring(tmp_path, entities=[EntitySpec(name="Foe", kind="enemy")])
    assert "SEAM 4" in wiring
    # player_fsm's four, unchanged from the hardcoded version it replaces.
    for state, clip in (("Idle", "Idle"), ("Moving", "Run"),
                        ("Jumping", "Jump"), ("Dead", "Death")):
        assert f"State.{state}: _playeranimator.CrossFade(\"{clip}\"" in wiring


def test_seam4_never_names_a_state_the_fsm_does_not_declare(tmp_path):
    """The CS0117 this replaces: the hardcoded switch referenced members that do
    not exist on any FSM but player_fsm's default set."""
    wiring = _gamewiring(tmp_path, entities=[EntitySpec(name="Foe", kind="enemy")])
    block = wiring[wiring.index("OnFSMStateChanged("):]
    block = block[:block.index("}\n")]
    referenced = set(re.findall(r"State\.(\w+)", block))
    assert referenced == {"Idle", "Moving", "Jumping", "Dead"}
