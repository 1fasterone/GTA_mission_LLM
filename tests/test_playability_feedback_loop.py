"""
Playability feedback loop — the evaluator's findings change what gets built.

Two defects, one loop:

  1. `objective` passed on the mere PRESENCE of something to do, so a build
     whose mission nothing could ever advance graded 91/100 while a winnable
     one graded 90. `mission_loop` grades the loop itself, from what the wiring
     pass actually bound (schema.meta["wiring"]) rather than from what the
     schema declares.

  2. Everything the evaluator found, it found AFTER the project was written.
     Step 5c now grades the schema BEFORE the generator runs and applies the
     deterministic remedies its findings carry, so a brief that described its
     progression as NARRATIVE still ships a game that can be completed.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_playability import evaluate
from src.unity_playability_repair import apply_remedies, repair_and_regrade
from src.unity_schema import (EntitySpec, MissionObjective, MissionSpec,
                              StoryBeat, UnitySchema, UnityScript)


def _schema(**kw) -> UnitySchema:
    base = dict(
        project_name="Proof", game_type="3D", platform="pc",
        render_pipeline="URP",
        entities=[EntitySpec(name="Hero", kind="player", tag="Player"),
                  EntitySpec(name="Hut", kind="collectible", count=6)],
        scripts=[UnityScript(name="P", type="player", description="d")],
    )
    base.update(kw)
    return UnitySchema(**base)


_BEATS = [
    StoryBeat(id="cold_start", label="Cold Start",
              description="Crew chief walks the warm-up lap", trigger="on_start"),
    StoryBeat(id="first_blood", label="First Blood",
              description="Take a podium place off a rival", trigger="on_enter_zone"),
    StoryBeat(id="stud_failure", label="Stud Failure",
              description="Studs wear to nothing mid-race", trigger="on_timer"),
]


def _compose(schema, tmp_path) -> tuple[str, dict]:
    """Compose the schema and return (GameWiring.cs, wiring_report)."""
    subs = subsystems_for(schema.game_type, schema.features,
                          missions=schema.missions, entities=schema.entities,
                          scripts=schema.scripts)
    assets = Path(tmp_path) / "Assets"
    comp = MicroComposer("Proof", project_config={"audio": {"backend": "dsp"}})
    comp.compose(subs, assets)
    wiring = (assets / "Micros" / "wiring" / "Scripts"
              / "GameWiring.cs").read_text(encoding="utf-8")
    return wiring, comp.wiring_report


# ── The remedy: story beats -> missions ───────────────────────────────────────

def test_beats_become_objectives_typed_by_their_own_trigger():
    schema = _schema(story_beats=list(_BEATS))
    _rep, msgs = repair_and_regrade(schema)
    assert msgs
    assert len(schema.missions) == 1
    got = {o.id: o.type for o in schema.missions[0].objectives}
    assert got == {"first_blood": "reach", "stud_failure": "survive"}


def test_beat_descriptions_survive_into_the_objective():
    """This is the line SEAM 9 puts on the HUD — it must be the brief's words."""
    schema = _schema(story_beats=list(_BEATS))
    repair_and_regrade(schema)
    obj = next(o for o in schema.missions[0].objectives if o.id == "first_blood")
    assert obj.description == "Take a podium place off a rival"


def test_on_start_and_manual_beats_become_no_objective():
    """Neither describes something the player does, so neither is a goal."""
    schema = _schema(story_beats=[
        StoryBeat(id="a", label="Open", description="d", trigger="on_start"),
        StoryBeat(id="b", label="Cue", description="d", trigger="manual")])
    _rep, msgs = repair_and_regrade(schema)
    assert not schema.missions          # abstained rather than manufacture one
    assert any("no player-driven trigger" in m for m in msgs)


def test_survive_objectives_get_a_duration_not_a_count():
    """Objectives.cs.j2 drains a survive objective at one count per second."""
    schema = _schema(story_beats=[StoryBeat(id="hold", label="Hold",
                                            description="d", trigger="on_timer")])
    repair_and_regrade(schema)
    assert schema.missions[0].objectives[0].count > 1


def test_an_authored_mission_is_never_overwritten():
    mission = MissionSpec(id="authored", title="Authored", objectives=[
        MissionObjective(id="o1", description="Reach it", type="reach")])
    schema = _schema(story_beats=list(_BEATS), missions=[mission])
    _rep, msgs = repair_and_regrade(schema)
    assert schema.missions == [mission]
    assert not msgs


def test_repair_is_deterministic():
    a, b = _schema(story_beats=list(_BEATS)), _schema(story_beats=list(_BEATS))
    repair_and_regrade(a)
    repair_and_regrade(b)
    assert [(o.id, o.type, o.count, o.description)
            for o in a.missions[0].objectives] == \
           [(o.id, o.type, o.count, o.description)
            for o in b.missions[0].objectives]


def test_the_loop_terminates_when_nothing_more_applies():
    schema = _schema(story_beats=list(_BEATS))
    repair_and_regrade(schema)
    # A second pass over an already-repaired schema must be a no-op.
    assert apply_remedies(schema, evaluate(schema)) == []


def test_an_unknown_remedy_is_reported_not_guessed_at():
    from src.unity_playability import Finding, PlayabilityReport
    rep = PlayabilityReport()
    rep.findings.append(Finding("x", "info", "s", "src", remedy="teleport_player"))
    msgs = apply_remedies(_schema(), rep)
    assert any("no handler for remedy" in m for m in msgs)


# ── The loop: the repair reaches the built project ────────────────────────────

def test_beats_alone_compose_no_mission_loop(tmp_path):
    """The --no-llm shape, unrepaired: SEAM 8/9 have nothing to bind."""
    schema = _schema(story_beats=list(_BEATS))
    wiring, report = _compose(schema, tmp_path)
    assert "SEAM 8" not in wiring and "SEAM 9" not in wiring
    assert report["objectives_total"] == 0


def test_repair_closes_the_loop_end_to_end(tmp_path):
    schema = _schema(story_beats=list(_BEATS))
    repair_and_regrade(schema)
    wiring, report = _compose(schema, tmp_path)
    assert "SEAM 8" in wiring          # the mission can now be advanced
    assert "SEAM 9" in wiring          # and the player can see it
    assert report["objectives_bound"]
    assert report["hud_objective"] is True


# ── mission_loop grades the loop, not the content ─────────────────────────────

def test_mission_loop_is_ungraded_without_a_composed_build():
    """Absence of evidence is never evidence of playability."""
    rep = evaluate(_schema(story_beats=list(_BEATS)))
    assert "mission_loop" in rep.not_graded


def test_mission_loop_fails_when_nothing_can_advance_the_objectives():
    schema = _schema(missions=[MissionSpec(id="m", title="M", objectives=[
        MissionObjective(id="o1", description="d", type="escort")])])
    schema.meta["wiring"] = {"objectives_total": 1, "objectives_bound": [],
                             "objectives_unbound": ["M.o1 (escort)"],
                             "hud_objective": False, "seams": []}
    rep = evaluate(schema)
    assert rep.checks["mission_loop"] is False
    assert any(f.check == "mission_loop" and f.severity == "error"
               for f in rep.findings)


def test_mission_loop_passes_when_an_objective_is_bound(tmp_path):
    schema = _schema(story_beats=list(_BEATS))
    repair_and_regrade(schema)
    _wiring, report = _compose(schema, tmp_path)
    schema.meta["wiring"] = report
    rep = evaluate(schema)
    assert rep.checks["mission_loop"] is True


def test_an_unwinnable_build_no_longer_outscores_a_winnable_one(tmp_path):
    """The defect that motivated this: 91/100 for the build that cannot be
    completed, 90 for the one that can."""
    unwinnable = _schema(story_beats=list(_BEATS))
    _w, r = _compose(unwinnable, tmp_path / "a")
    unwinnable.meta["wiring"] = r

    winnable = _schema(story_beats=list(_BEATS))
    repair_and_regrade(winnable)
    _w2, r2 = _compose(winnable, tmp_path / "b")
    winnable.meta["wiring"] = r2

    assert evaluate(winnable).score > evaluate(unwinnable).score


def test_progress_that_cannot_be_seen_is_reported(tmp_path):
    schema = _schema(missions=[MissionSpec(id="m", title="M", objectives=[
        MissionObjective(id="o1", description="d", type="reach")])])
    schema.meta["wiring"] = {"objectives_total": 1, "objectives_bound": ["M.o1"],
                             "objectives_unbound": [], "hud_objective": False,
                             "seams": ["SEAM 8"]}
    rep = evaluate(schema)
    assert rep.checks["mission_visible"] is False
    assert any(f.check == "mission_visible" for f in rep.findings)


@pytest.mark.parametrize("trigger,obj_type", [
    ("on_enter_zone", "reach"), ("on_pickup", "collect"),
    ("on_enemy_death", "defeat"), ("on_timer", "survive"),
])
def test_every_player_driven_trigger_maps_to_an_objective(trigger, obj_type):
    schema = _schema(story_beats=[StoryBeat(id="b", label="B", description="d",
                                            trigger=trigger)])
    repair_and_regrade(schema)
    assert schema.missions[0].objectives[0].type == obj_type
