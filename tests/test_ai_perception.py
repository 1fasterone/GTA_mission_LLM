"""
AI perception — vision cone + raycast LOS, hearing, memory, and squad alert
propagation, layered under the existing FSM/role machinery in src/micro/ai.py
and templates/unity/micro/ai/AI.cs.j2.

Scope: navmesh_patrol / fsm_guard / behaviour_tree_lite only. Those are the
only variants with a real "have I noticed the target yet?" gate today;
chase_player/cover_shooter/flocking/flying_swarm/waypoint_follow act on the
target unconditionally once found and are left untouched — this file also
pins that they stay untouched, so a future edit that leaks perception code
into them (or drops it from the three that should have it) is caught here.

Offline and deterministic — no LLM, no Unity, no network. Assertions are
either on the Python schema/fixer or on rendered .cs text, the same style
tests/test_ai_fsm_authored_states.py already uses (this repo's test suite
cannot execute C#, so behaviour is pinned at the source-text level).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.ai import AIPipeline, AISchema, AIState, AITransition

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


def _fix(schema, variant):
    return AIPipeline.default_fix(AIPipeline.__new__(AIPipeline), schema, variant)


def _render(cfg, schema, variant, tmp_path) -> str:
    p = AIPipeline(cfg)
    p.write_files(schema, variant, tmp_path)
    return (tmp_path / "Scripts" / f"{schema.name}.cs").read_text(encoding="utf-8")


# ── Schema defaults + clamping ──────────────────────────────────────────────

def test_perception_defaults():
    s = AISchema(name="X")
    assert s.vision_angle == 120.0
    assert s.vision_obstruction is True
    assert s.hearing_radius == 6.0
    assert s.memory_seconds == 3.0
    assert s.squad_alert_radius == 10.0


@pytest.mark.parametrize("field,raw,expected", [
    ("vision_angle",          0.0,    1.0),     # below the [1, 360] floor
    ("vision_angle",          500.0,  360.0),   # above the ceiling
    ("eye_height",            -2.0,   0.0),
    ("hearing_radius",        -1.0,   0.0),
    ("noise_speed_threshold", -5.0,   0.1),
    ("memory_seconds",        -1.0,   0.0),
    ("memory_seconds",        999.0,  60.0),
    ("squad_alert_radius",    -3.0,   0.0),
])
def test_perception_fields_are_clamped(field, raw, expected):
    s = _fix(AISchema(name="X", variant="fsm_guard", **{field: raw}), "fsm_guard")
    assert getattr(s, field) == expected


def test_perception_internal_names_are_reserved():
    """The names UpdatePerception()/CanSeeTarget() actually declare must be in
    the collision set apply_extensions_safe_block checks extensions_fields
    against — otherwise an LLM-invented extensions_fields entry named e.g.
    "_targetVisible" would silently shadow the real one (CS0102)."""
    reserved = AIPipeline._RESERVED_FIELD_NAMES
    for name in ("visionangle", "visionobstruction", "eyeheight", "hearingradius",
                 "noisespeedthreshold", "memoryseconds", "squadalertradius",
                 "_targetvisible", "_heardnoise", "_wasperceiving", "_allyalerted",
                 "_lastknownposition", "_searchtimer", "_prevtargetpos",
                 "_haveprevtargetpos", "_squad"):
        assert name in reserved, f"{name!r} declared in AI.cs.j2 but not reserved"


# ── Transition vocabulary ───────────────────────────────────────────────────

def test_heard_noise_and_ally_alerted_are_valid_when_values():
    t1 = AITransition(from_state="A", to_state="B", when="heard_noise")
    t2 = AITransition(from_state="A", to_state="B", when="ally_alerted")
    assert t1.when == "heard_noise" and t1.condition == "_heardNoise"
    assert t2.when == "ally_alerted" and t2.condition == "_allyAlerted"


def test_target_detected_and_lost_read_the_perception_gate():
    detected = AITransition(from_state="A", to_state="B", when="target_detected")
    lost     = AITransition(from_state="A", to_state="B", when="target_lost")
    assert detected.condition == "HasPerceivedTarget()"
    assert lost.condition     == "!HasPerceivedTarget()"


def test_in_attack_range_is_unaffected_by_perception():
    """attack range stays a pure distance check — perception governs whether
    an AI notices the target at all, not whether it can hit one already in
    its face. Also locks in the exact string test_ai_fsm_authored_states.py's
    test_authored_transitions_are_emitted depends on."""
    t = AITransition(from_state="A", to_state="B", when="in_attack_range")
    assert t.condition == "_target != null && dist < attackRadius"


# ── Default transition graph: ally_alerted rides alongside target_detected ──

def test_default_graph_wires_ally_alerted_next_to_target_detected():
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Idle", role="idle"), AIState(name="Chase", role="chase"),
        AIState(name="Attack", role="attack")]), "fsm_guard")
    pairs = {(t.from_state, t.to_state, t.when) for t in s.transitions}
    assert ("Idle", "Chase", "target_detected") in pairs
    assert ("Idle", "Chase", "ally_alerted") in pairs


def test_default_graph_wires_ally_alerted_through_an_alert_state_too():
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Idle", role="idle"), AIState(name="Freeze", role="alert"),
        AIState(name="Chase", role="chase")]), "fsm_guard")
    pairs = {(t.from_state, t.to_state, t.when) for t in s.transitions}
    assert ("Idle", "Freeze", "target_detected") in pairs
    assert ("Idle", "Freeze", "ally_alerted") in pairs


# ── Rendered C#: perception surface exists only where it should ────────────

_PERCEPTION_MARKERS = ("UpdatePerception()", "HasPerceivedTarget()",
                      "CanSeeTarget()", "ChaseDestination()")


@pytest.mark.parametrize("variant", ["navmesh_patrol", "fsm_guard", "behaviour_tree_lite"])
def test_perception_variants_get_the_sensor_methods(dummy_config, tmp_path, variant):
    s = _fix(AISchema(name="X", variant=variant), variant)
    cs = _render(dummy_config, s, variant, tmp_path)
    for marker in _PERCEPTION_MARKERS:
        assert marker in cs, f"{variant}: missing {marker}"
    assert "private void UpdatePerception()" in cs
    assert "private bool CanSeeTarget()" in cs


@pytest.mark.parametrize("variant", ["chase_player", "cover_shooter", "flocking",
                                     "flying_swarm", "waypoint_follow"])
def test_non_perception_variants_stay_untouched(dummy_config, tmp_path, variant):
    s = _fix(AISchema(name="X", variant=variant), variant)
    cs = _render(dummy_config, s, variant, tmp_path)
    for marker in _PERCEPTION_MARKERS:
        assert marker not in cs, f"{variant}: unexpectedly has {marker}"
    assert "[Header(\"Perception\")]" not in cs


def test_navmesh_patrol_gate_uses_perception_not_raw_distance(dummy_config, tmp_path):
    s = _fix(AISchema(name="Wanderer", variant="navmesh_patrol"), "navmesh_patrol")
    cs = _render(dummy_config, s, "navmesh_patrol", tmp_path)
    assert "if (HasPerceivedTarget())" in cs
    assert "_agent.SetDestination(ChaseDestination())" in cs
    # The old unconditional sphere check must be gone, not just supplemented.
    assert "Vector3.Distance(transform.position, _target.position) < detectionRadius" not in cs


# ── Squad alert propagation is opt-in via squad_alert_radius ───────────────

def test_squad_disabled_omits_squad_machinery(dummy_config, tmp_path):
    s = _fix(AISchema(name="Lone", variant="fsm_guard", squad_alert_radius=0.0), "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    for marker in ("_squad", "AlertSquad", "ReceiveAllyAlert", "squadAlertRadius"):
        assert marker not in cs
    # The default graph must not reference a transition condition it never emits.
    assert "_allyAlerted" not in cs


def test_squad_enabled_emits_squad_machinery(dummy_config, tmp_path):
    s = _fix(AISchema(name="Pack", variant="fsm_guard", squad_alert_radius=8.0), "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "private static readonly List<Pack> _squad = new List<Pack>();" in cs
    assert "_squad.Add(this);" in cs
    assert "_squad.Remove(this);" in cs
    assert "private void AlertSquad()" in cs
    assert "public void ReceiveAllyAlert(Vector3 reportedPosition)" in cs
    assert "_allyAlerted = false;" in cs   # consumed-then-reset, mirrors _tookDamage


def test_authored_ally_alerted_transition_is_dropped_when_squad_disabled(dummy_config, tmp_path):
    """An authored (or, via _default_transitions, synthesised) transition on
    "ally_alerted" only compiles when squad_alert_radius > 0 — that is what
    gates _allyAlerted/_squad/AlertSquad in AI.cs.j2. Without this guard an
    LLM that authors both squad_alert_radius=0 AND an ally_alerted edge would
    ship an undeclared-identifier build break (CS0103)."""
    s = _fix(AISchema(name="Foe", variant="fsm_guard", squad_alert_radius=0.0,
                      states=[AIState(name="Idle", role="idle"),
                              AIState(name="Chase", role="chase")],
                      transitions=[AITransition(from_state="Idle", to_state="Chase",
                                                when="ally_alerted")]),
             "fsm_guard")
    # Dropped, not silently kept — and since it was the ONLY authored edge,
    # the default graph fills in behind it (never an empty transition list).
    assert all(t.when != "ally_alerted" for t in s.transitions)
    assert s.transitions
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "_allyAlerted" not in cs


def test_squad_alert_shares_last_known_position_not_live_tracking(dummy_config, tmp_path):
    """The payoff of alert propagation: an alerted ally searches WHERE it was
    told, it does not get the other AI's omniscient live target reference."""
    s = _fix(AISchema(name="Pack", variant="fsm_guard", squad_alert_radius=8.0), "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "ally.ReceiveAllyAlert(_lastKnownPosition);" in cs
    assert "_lastKnownPosition  = reportedPosition;" in cs


# ── 3D vs 2D backend selection inside CanSeeTarget ──────────────────────────

def test_3d_uses_physics_linecast_and_forward_facing(dummy_config, tmp_path):
    s = _fix(AISchema(name="Guard3D", variant="fsm_guard"), "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "Physics.Linecast(eye, targetEye, out RaycastHit hit)" in cs
    assert "Physics2D.Linecast" not in cs
    assert "Vector3.Angle(transform.forward, flat)" in cs


def test_2d_uses_physics2d_linecast_and_right_facing(dummy_config, tmp_path):
    s = _fix(AISchema(name="Guard2D", variant="fsm_guard", meta={"game_type": "2D"}),
             "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "Physics2D.Linecast(origin, dest)" in cs
    assert "Physics.Linecast" not in cs
    assert "Vector3.Angle(transform.right, flat)" in cs
    assert "NavMeshAgent" not in cs   # 2D still gets no NavMesh dependency


def test_vision_obstruction_is_a_runtime_inspector_toggle(dummy_config, tmp_path):
    """vision_obstruction (like visionAngle/hearingRadius) is baked as a
    [SerializeField] default and checked at runtime (`if (visionObstruction)`),
    not compiled away — a Unity user can flip it in the Inspector without
    regenerating code. The raycast call itself is always present."""
    s = _fix(AISchema(name="XRay", variant="fsm_guard", vision_obstruction=False),
             "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "private bool  visionObstruction     = false;" in cs
    assert "if (visionObstruction)" in cs
    assert "Physics.Linecast(eye, targetEye, out RaycastHit hit)" in cs


# ── Chase role destination ──────────────────────────────────────────────────

def test_chase_role_uses_chase_destination_helper(dummy_config, tmp_path):
    s = _fix(AISchema(name="Foe", variant="fsm_guard", states=[
        AIState(name="Act", role="chase"), AIState(name="Wait", role="idle")]),
        "fsm_guard")
    cs = _render(dummy_config, s, "fsm_guard", tmp_path)
    assert "_agent.SetDestination(ChaseDestination());" in cs
    assert "_agent.SetDestination(_target.position);" not in cs
