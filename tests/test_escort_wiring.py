"""Escort objective wiring — src/unity_escort_follow.py + SEAM 8b.

`escort`-type objectives had no honest event source at all (see
tests/test_seam8_mission_loop.py's own history): nothing in a composed
project ever made the escorted NPC actually go anywhere, so wiring a `reach`-
style trigger straight to it would let the player complete the objective
alone. `resolve_escort_follow` gives the target entity a real Follow-atom
behavior; SEAM 8b (src/unity_compose.py) then wires the trigger conditionally
on that entity's own live position, using `WireTriggerToEscortObjective`
(src/atoms/backends/unity.py).

Offline and deterministic — no LLM, no Unity, no network.
"""
from __future__ import annotations

from pathlib import Path

from src.atoms.backends.unity import unity_atom_snippet
from src.atoms.base import AtomInvocation
from src.unity_compose import MicroComposer, subsystems_for
from src.unity_escort_follow import resolve_escort_follow
from src.unity_schema import EntitySpec, MissionObjective, MissionSpec, UnitySchema


# ── resolve_escort_follow ────────────────────────────────────────────────────

def _schema(entities, missions):
    return UnitySchema(
        project_name="EscortTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        entities=entities, missions=missions,
    )


def _escort_mission(target_ref="Engineer") -> MissionSpec:
    return MissionSpec(
        id="m1", title="Extraction",
        objectives=[MissionObjective(id="obj_escort", type="escort",
                                     target_ref=target_ref,
                                     description="Escort the engineer out.")])


def test_fills_a_follow_atom_and_behavior_for_the_escorted_entity():
    s = _schema(
        entities=[EntitySpec(name="Engineer", kind="npc", count=1)],
        missions=[_escort_mission()],
    )
    msgs = resolve_escort_follow(s)
    entity = s.entities[0]
    assert entity.atoms and entity.atoms[0].atom == "Follow"
    assert entity.atoms[0].args.get("target") == "Player"
    assert entity.behavior == "EngineerBehavior"
    assert any("Engineer" in m and "follows the player" in m for m in msgs)


def test_fill_only_never_overrides_an_authored_behavior():
    s = _schema(
        entities=[EntitySpec(name="Engineer", kind="npc", count=1,
                             behavior="MyHandAuthoredBehavior")],
        missions=[_escort_mission()],
    )
    resolve_escort_follow(s)
    assert s.entities[0].behavior == "MyHandAuthoredBehavior"
    assert s.entities[0].atoms == []


def test_multi_instance_escort_target_abstains_loudly():
    s = _schema(
        entities=[EntitySpec(name="Engineer", kind="npc", count=3)],
        missions=[_escort_mission()],
    )
    msgs = resolve_escort_follow(s)
    assert s.entities[0].behavior == ""
    assert any("count=3" in m and "stays unwired" in m for m in msgs)


def test_unresolvable_target_ref_is_a_silent_noop_here():
    """resolve_objective_targets (an earlier Step 5 pass) is the one that
    names an unfillable target_ref; this pass only acts once one exists."""
    s = _schema(
        entities=[EntitySpec(name="SomeoneElse", kind="npc", count=1)],
        missions=[_escort_mission(target_ref="Engineer")],
    )
    msgs = resolve_escort_follow(s)
    assert msgs == []
    assert s.entities[0].behavior == ""


# ── WireTriggerToEscortObjective atom snippet ───────────────────────────────

def test_escort_snippet_gates_on_the_followed_entitys_own_position():
    snippet = unity_atom_snippet("WireTriggerToEscortObjective", {
        "trigger_var": "_trigger", "objectives_var": "_objectives",
        "delegate_var": "_onEscortObjective0", "objective_id": "obj_escort",
        "entity_go_name": "Engineer_0",
    })
    assert 'GameObject.Find("Engineer_0")' in snippet
    assert "Vector3.Distance" in snippet
    assert 'ReportProgress("obj_escort")' in snippet
    assert "_trigger.OnActivated += _onEscortObjective0;" in snippet


def test_escort_snippet_omits_without_a_matched_entity():
    snippet = unity_atom_snippet("WireTriggerToEscortObjective", {
        "trigger_var": "_trigger", "objectives_var": "_objectives",
        "objective_id": "obj_escort",
        # entity_go_name deliberately absent
    })
    assert "seam omitted" in snippet
    assert "ReportProgress" not in snippet


# ── Compose-level integration (SEAM 8b) ─────────────────────────────────────

def test_escort_objective_binds_when_the_entity_is_followed(tmp_path):
    entity = EntitySpec(
        name="Engineer", kind="npc", count=1,
        atoms=[AtomInvocation(atom="Follow", args={"target": "Player"})],
        behavior="EngineerBehavior",
    )
    mission = _escort_mission()
    subs = subsystems_for("3D", [], missions=[mission], entities=[entity])
    assets = Path(tmp_path) / "Assets"
    composer = MicroComposer("EscortBound", project_config={"audio": {"backend": "dsp"}})
    composer.compose(subs, assets, escort_followed_entities={"Engineer"})

    gw = (assets / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "SEAM 8b" in gw
    assert 'GameObject.Find("Engineer_0")' in gw
    assert any(b.endswith(".obj_escort") for b in composer.wiring_report["objectives_bound"])
    assert "obj_escort" not in " ".join(composer.wiring_report["objectives_unbound"])
    # EngineerBehavior.cs itself (the Follow-atom coroutine) is generated by
    # the full unity_generator.py pipeline, not MicroComposer -- out of scope
    # for this compose-level test; covered separately by resolve_escort_
    # follow's own unit tests above and by unity_generator.py's existing
    # entity-behavior generation tests.


def test_escort_objective_stays_unbound_without_a_followed_entity(tmp_path):
    """No entity.atoms/.behavior was ever set (the fixer abstained, or this
    is a hand-authored schema that skipped it) -- wiring the trigger straight
    to the objective here would be exactly the dishonest signal this whole
    seam exists to avoid."""
    mission = _escort_mission()
    subs = subsystems_for("3D", [], missions=[mission],
                          entities=[EntitySpec(name="Engineer", kind="npc", count=1)])
    assets = Path(tmp_path) / "Assets"
    composer = MicroComposer("EscortUnbound", project_config={"audio": {"backend": "dsp"}})
    composer.compose(subs, assets)   # escort_followed_entities omitted

    gw = (assets / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "SEAM 8b" not in gw
    assert "obj_escort" in " ".join(composer.wiring_report["objectives_unbound"])
