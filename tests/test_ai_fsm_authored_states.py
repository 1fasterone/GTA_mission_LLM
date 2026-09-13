"""
ai FSM — the emitted state machine is built from the AUTHORED states.

The defect this pins: AIState.cs.j2 generated its enum from schema.states while
AI.cs.j2's switch hardcoded Idle/Chase/Attack. The schema prompt showed the model
those three names as its example, so it usually copied them and the run looked
fine — name the states anything else and the build broke with CS0117. The field
was decorative: it reached the enum and nothing else.

These tests pin that the model's own vocabulary survives into the emitted code
(that is the variety worth keeping) while never being able to break the build
(that is the part that must be deterministic): names are sanitised, roles are
what code is generated from, transition conditions come from a closed vocabulary
rather than authored C#, and dangling edges are dropped loudly.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.ai import (AIPipeline, AISchema, AIState, AITransition,
                          infer_role)
from src.unity_compose import MicroComposer, _ai_states_from_brief, subsystems_for
from src.unity_schema import EntitySpec, MissionObjective, MissionSpec

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


def _fix(schema, variant="fsm_guard"):
    return AIPipeline.default_fix(AIPipeline.__new__(AIPipeline), schema, variant)


def _render(cfg, schema, variant, tmp_path) -> tuple[str, str]:
    p = AIPipeline(cfg)
    p.write_files(schema, variant, tmp_path)
    return ((tmp_path / "Scripts" / f"{schema.name}.cs").read_text(encoding="utf-8"),
            (tmp_path / "Scripts" / f"{schema.name}State.cs").read_text(encoding="utf-8"))


# ── The CS0117 defect ─────────────────────────────────────────────────────────

def test_model_named_states_do_not_break_the_build(dummy_config, tmp_path):
    """The audit's exact reproduction: states named anything but the example three."""
    s = _fix(AISchema(name="SentryDrone", variant="fsm_guard", states=[
        AIState(name="Patrol"), AIState(name="Alert"),
        AIState(name="Pursue"), AIState(name="Retreat")]))
    cs, enum = _render(dummy_config, s, "fsm_guard", tmp_path)

    # Every switch label is a member the enum actually declares.
    declared = {ln.split(",")[0].strip() for ln in enum.splitlines()
                if ln.strip() and not ln.strip().startswith(("//", "public", "{", "}"))}
    assert declared == {"Patrol", "Alert", "Pursue", "Retreat"}
    for name in declared:
        assert f"case SentryDroneState.{name}:" in cs
    # And none of the old hardcoded three leak in.
    for ghost in ("SentryDroneState.Idle", "SentryDroneState.Chase",
                  "SentryDroneState.Attack"):
        assert ghost not in cs


def test_initial_state_is_a_state_that_exists(dummy_config, tmp_path):
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Prowl"), AIState(name="Lunge")]))
    cs, _ = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert f"_state = FoeState.{s.initial_state};" in cs
    assert s.initial_state in {st.name for st in s.states}


# ── Names: the model's vocabulary survives, sanitised ─────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("chase player",  "ChasePlayer"),
    ("pursue",        "Pursue"),
    ("hunt-the-diver", "HuntTheDiver"),
    ("2nd sweep",     "State2ndSweep"),   # an identifier cannot start with a digit
])
def test_state_names_are_sanitised_into_valid_identifiers(raw, expected):
    s = _fix(AISchema(name="Foe", variant="fsm_guard",
                      states=[AIState(name=raw), AIState(name="Strike")]))
    assert expected in {st.name for st in s.states}


def test_state_named_like_a_csharp_keyword_is_escaped():
    s = _fix(AISchema(name="Foe", variant="fsm_guard",
                      states=[AIState(name="switch"), AIState(name="Strike")]))
    assert "SwitchState" in {st.name for st in s.states}


def test_states_colliding_after_sanitising_are_dropped_not_duplicated():
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="chase player"), AIState(name="Chase Player"),
        AIState(name="Strike")]))
    names = [st.name for st in s.states]
    assert names.count("ChasePlayer") == 1


# ── Roles: what the generator actually emits code from ────────────────────────

@pytest.mark.parametrize("name,role", [
    ("Prowl", "patrol"), ("Sweep", "patrol"), ("Freeze", "alert"),
    ("Pursue", "chase"), ("Lunge", "attack"), ("Bail", "flee"),
    ("Expire", "dead"), ("Xyzzy", "idle"),     # unknown -> idle, never dropped
])
def test_role_is_inferred_from_the_state_name(name, role):
    assert infer_role(name) == role


def test_explicit_role_beats_inference():
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Prowl", role="attack"), AIState(name="Strike")]))
    assert next(st for st in s.states if st.name == "Prowl").role == "attack"


@pytest.mark.parametrize("role,marker", [
    ("patrol", "PatrolStep();"),
    ("chase",  "_agent.SetDestination(ChaseDestination())"),
    ("attack", "TryAttack(dist);"),
    ("dead",   "enabled = false;"),
])
def test_each_role_emits_its_behaviour(dummy_config, tmp_path, role, marker):
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Act", role=role), AIState(name="Wait", role="idle")]))
    cs, _ = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert marker in cs


def test_patrol_degrades_where_the_variant_has_no_patrol_helper(dummy_config, tmp_path):
    """behaviour_tree_lite emits neither patrolPoints nor PatrolStep, so a patrol
    role there would be CS0103."""
    s = _fix(AISchema(name="Foe", variant="behaviour_tree_lite", states=[
        AIState(name="Prowl"), AIState(name="Strike")]), "behaviour_tree_lite")
    cs, _ = _render(dummy_config, s, "behaviour_tree_lite", tmp_path)
    assert next(st for st in s.states if st.name == "Prowl").role == "idle"
    assert "PatrolStep" not in cs


def test_states_sharing_a_role_do_not_collide_on_locals(dummy_config, tmp_path):
    """C# switch cases share one scope, so two states of the same role emitting
    the same local name would be CS0128."""
    import re
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Watch"), AIState(name="Listen"),   # both alert
        AIState(name="Bail"), AIState(name="Escape"),    # both flee
        AIState(name="Hunt")]))
    cs, _ = _render(dummy_config, s, "fsm_guard", tmp_path)
    locals_ = re.findall(r"Vector3 (\w+)", cs)
    assert locals_ and len(locals_) == len(set(locals_))


# ── Transitions: a closed vocabulary, never authored C# ───────────────────────

def test_transition_condition_vocabulary_is_clamped():
    """A hallucinated condition must not reach the emitted file as code."""
    t = AITransition(from_state="A", to_state="B",
                     when="_playerSanity < 0.3f && _moonPhase == Waning")
    assert t.when == "always"
    assert t.condition == "true"


def test_authored_transitions_are_emitted(dummy_config, tmp_path):
    s = _fix(AISchema(name="Foe", variant="fsm_guard",
                      states=[AIState(name="Prowl"), AIState(name="Lunge")],
                      transitions=[AITransition(from_state="Prowl", to_state="Lunge",
                                                when="in_attack_range")]))
    cs, _ = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert ("if (_state == FoeState.Prowl && (_target != null && dist < attackRadius))"
            in cs)
    assert "SetState(FoeState.Lunge);" in cs


def test_dangling_transitions_are_dropped():
    s = _fix(AISchema(name="Foe", variant="fsm_guard",
                      states=[AIState(name="Prowl"), AIState(name="Lunge")],
                      transitions=[
                          AITransition(from_state="Prowl", to_state="Nowhere"),
                          AITransition(from_state="Ghost", to_state="Lunge"),
                          AITransition(from_state="Prowl", to_state="Prowl"),
                          AITransition(from_state="Prowl", to_state="Lunge",
                                       when="target_detected")]))
    assert [(t.from_state, t.to_state) for t in s.transitions] == [("Prowl", "Lunge")]


def test_default_graph_is_built_from_the_states_that_exist():
    """Never the hardcoded triple: a two-state FSM gets a two-state graph."""
    s = _fix(AISchema(name="Foe", variant="fsm_guard",
                      states=[AIState(name="Prowl"), AIState(name="Lunge")]))
    names = {st.name for st in s.states}
    assert s.transitions
    for t in s.transitions:
        assert t.from_state in names and t.to_state in names


def test_every_emitted_transition_endpoint_is_a_declared_state(dummy_config, tmp_path):
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Prowl"), AIState(name="Freeze"),
        AIState(name="Pursue"), AIState(name="Lunge"), AIState(name="Bail")]))
    cs, enum = _render(dummy_config, s, "fsm_guard", tmp_path)
    declared = {st.name for st in s.states}
    import re
    # Comments name the sibling file "FoeState.cs" — scan code lines only.
    code = "\n".join(ln for ln in cs.splitlines()
                     if not ln.lstrip().startswith(("//", "///")))
    for ref in set(re.findall(r"FoeState\.(\w+)", code)):
        assert ref in declared, f"{ref} referenced but not declared"


# ── Composer: the brief's behaviour prose becomes the FSM ─────────────────────

def test_behavior_prose_becomes_named_states():
    ents = [EntitySpec(name="Stalker", kind="enemy", count=3,
                       behavior="Prowls the gantries, freezes when it hears you, "
                                "then charges at full thrust")]
    states = _ai_states_from_brief(ents, None)
    assert [(s["name"], s["role"]) for s in states] == [
        ("Prowl", "patrol"), ("Freeze", "alert"), ("Charge", "chase")]


def test_enemy_script_description_is_a_behaviour_source():
    """Where a structured brief actually puts its behaviour prose: EntitySpec has
    no description field, and `behavior` often holds a script reference."""
    from src.unity_schema import UnityScript
    sc = UnityScript(name="SentryAI", type="enemy", description=(
        "drones patrol the catwalks, investigate noises, hunt the intruder "
        "and retreat when damaged"))
    assert [(s["name"], s["role"]) for s in _ai_states_from_brief([], None, [sc])] == [
        ("Patrol", "patrol"), ("Investigate", "alert"),
        ("Hunt", "chase"), ("Retreat", "flee")]


def test_prose_with_no_behaviour_vocabulary_yields_no_states():
    """A racing rival that "learns the racing line and defends the fast lane" is
    not a patrol/chase FSM — abstain rather than force one."""
    from src.unity_schema import UnityScript
    sc = UnityScript(name="RivalRacerAI", type="enemy", description=(
        "three rival cars that learn the player's racing line over laps and move "
        "to defend the fast lane; per-rival aggression"))
    assert _ai_states_from_brief([], None, [sc]) == []


def test_one_behaviour_role_is_not_a_state_machine():
    ents = [EntitySpec(name="Drone", kind="enemy", behavior="patrols the dock")]
    assert _ai_states_from_brief(ents, None) == []


def test_undescribed_enemy_keeps_the_stateless_variant():
    ents = [EntitySpec(name="Drone", kind="enemy", count=4)]
    assert _ai_states_from_brief(ents, None) == []


def test_composed_project_upgrades_to_fsm_when_the_brief_describes_states(tmp_path):
    ents = [EntitySpec(name="Stalker", kind="enemy", count=3,
                       behavior="Prowls the gantries, freezes when it hears you, "
                                "then charges at full thrust")]
    subs = subsystems_for("3D", ["ai"], entities=ents,
                          missions=[MissionSpec(id="m1", title="Run", objectives=[
                              MissionObjective(id="o1", description="Escape",
                                               type="reach")])])
    ai = next(s for s in subs if s.kind == "ai")
    assert ai.variant == "fsm_guard"

    assets = Path(tmp_path) / "Assets"
    MicroComposer("AiProof", project_config={"audio": {"backend": "dsp"}}).compose(
        subs, assets)
    cs = "\n".join(f.read_text(encoding="utf-8")
                   for f in sorted((assets / "Micros" / "ai" / "Scripts").glob("*.cs")))
    for name in ("Prowl", "Freeze", "Charge"):
        assert f"case EnemyAIState.{name}:" in cs
