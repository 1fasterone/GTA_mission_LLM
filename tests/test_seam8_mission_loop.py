"""
SEAM 8 — the mission loop.

Before this seam the objectives micro received real, LLM-authored objectives from
schema.missions and then sat inert: nothing in the entire repo ever called
ReportProgress, so OnMissionComplete was unreachable and no generated game could
be finished. These tests pin the three event sources that now drive it, the
deterministic one-objective-per-source rule, and the loud abstention for
objective types a composed project has no honest signal for.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

from pathlib import Path

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_schema import EntitySpec, MissionObjective, MissionSpec


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mission_project(tmp_path, project="Seam8Proof", *, objectives, triggers=(),
                     entities=None, game_type="3D",
                     reach_target_entities=None) -> Path:
    """Compose a whole project around one authored mission; return its Assets dir."""
    mission = MissionSpec(id="m1", title="Vault Run", triggers=list(triggers),
                          objectives=objectives)
    ents = entities if entities is not None else [
        EntitySpec(name="Sentry Drone", kind="enemy", count=8)]
    subs = subsystems_for(game_type, [], missions=[mission], entities=ents)
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project, project_config={"audio": {"backend": "dsp"}}).compose(
        subs, assets, reach_target_entities=reach_target_entities, game_type=game_type)
    return assets


def _multi_mission_project(tmp_path, project="Seam8Scenes", *, missions,
                           objective_scenes=None, entities=None,
                           game_type="3D") -> Path:
    """Compose a project around several authored missions, optionally with a
    resolved {"<TrackerClass>.<objective.id>": scene_name} map (what
    `_objective_scene_map`, src/unity_pipeline.py, would hand to `compose()`
    in the real pipeline)."""
    ents = entities if entities is not None else [
        EntitySpec(name="Sentry Drone", kind="enemy", count=8)]
    subs = subsystems_for(game_type, [], missions=missions, entities=ents)
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project, project_config={"audio": {"backend": "dsp"}}).compose(
        subs, assets, objective_scenes=objective_scenes)
    return assets


def _micro_cs(assets: Path, kind: str) -> str:
    """Concatenated source of every script a composed micro emitted."""
    d = assets / "Micros" / kind / "Scripts"
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(d.glob("*.cs")))


def _gamewiring(assets: Path) -> str:
    return (assets / "Micros" / "wiring" / "Scripts"
            / "GameWiring.cs").read_text(encoding="utf-8")


# One objective of every type the IR can author, including the two that have no
# event source, so each test sees the full dispatch surface at once.
_ALL_TYPES = [
    MissionObjective(id="reach_vault", description="Get to the vault", type="reach"),
    MissionObjective(id="grab_cores", description="Collect 3 cores", type="collect", count=3),
    MissionObjective(id="cull_drones", description="Destroy 8 drones", type="defeat", count=8),
    MissionObjective(id="hold_out", description="Survive 90s", type="survive", count=90),
    MissionObjective(id="escort_tech", description="Escort the tech", type="escort"),
]


# ── The seam itself ───────────────────────────────────────────────────────────

def test_each_objective_type_binds_to_its_real_event_source(tmp_path):
    gw = _gamewiring(_mission_project(tmp_path, objectives=_ALL_TYPES))
    # reach -> the trigger zone's typed event
    assert "_scenetrigger.OnActivated += _onReachObjective;" in gw
    assert 'ReportProgress("reach_vault")' in gw
    # collect -> the interactable pickup
    assert "_worldinteractable.OnInteract += _onCollectObjective;" in gw
    assert 'ReportProgress("grab_cores")' in gw
    # eliminate -> the enemy spawner's despawn signal
    assert "_enemyspawner.OnDespawned += _onEliminateObjective;" in gw
    assert 'ReportProgress("cull_drones")' in gw


def test_delegates_are_stored_fields_and_unsubscribed_on_destroy(tmp_path):
    """An inline lambda creates a fresh delegate per call, so -= would silently
    no-op and the subscription would outlive the scene."""
    gw = _gamewiring(_mission_project(tmp_path, objectives=_ALL_TYPES))
    for field, ctype in (("_onReachObjective", "Action"),
                         ("_onCollectObjective", "Action"),
                         ("_onEliminateObjective", "Action<GameObject>")):
        assert f"private {ctype} {field};" in gw
    destroy = gw.split("private void OnDestroy()")[1]
    assert "_scenetrigger.OnActivated -= _onReachObjective;" in destroy
    assert "_worldinteractable.OnInteract -= _onCollectObjective;" in destroy
    assert "_enemyspawner.OnDespawned -= _onEliminateObjective;" in destroy


def test_every_subscribed_member_is_declared_by_its_micro(tmp_path):
    """Semantic guard the Roslyn syntax gate cannot provide: a member GameWiring
    references must actually exist on the micro that owns it, or the project only
    fails once a human opens Unity (CS1061)."""
    assets = _mission_project(tmp_path, objectives=_ALL_TYPES)
    for kind, member in (("trigger", "public event Action OnActivated;"),
                         ("interactable", "public event Action OnInteract;"),
                         ("spawner", "public event Action<GameObject> OnDespawned;"),
                         ("objectives", "public bool ReportProgress(string objectiveId")):
        assert member in _micro_cs(assets, kind), f"{kind} does not declare: {member}"


def test_survive_is_driven_by_the_tracker_clock_not_a_seam(tmp_path):
    """`survive` has no external event source — the tracker runs its own clock,
    so it is covered without being wired and must not be reported as unbound."""
    assets = _mission_project(tmp_path, objectives=_ALL_TYPES)
    obj = _micro_cs(assets, "objectives")
    assert "_surviveTimer += Time.deltaTime;" in obj
    assert 'ReportProgress("hold_out");' in obj
    assert "_onSurviveObjective" not in _gamewiring(assets)


def test_objectives_with_no_honest_source_are_named_not_faked(tmp_path, capsys):
    """Abstain loudly: escort has no signal in a composed project."""
    _mission_project(tmp_path, objectives=_ALL_TYPES)
    out = capsys.readouterr().out
    assert "no event source" in out
    assert "escort_tech" in out
    # The bound ones must not appear in the warning.
    warning = out.split("no event source", 1)[1]
    for bound in ("reach_vault", "grab_cores", "cull_drones", "hold_out"):
        assert bound not in warning


def test_one_source_binds_exactly_one_objective(tmp_path):
    """Two eliminate objectives share the single enemy spawner. Wiring both would
    advance both on every kill, which is worse than leaving the second unwired."""
    objs = [
        MissionObjective(id="cull_drones", description="Destroy 8 drones",
                         type="defeat", count=8),
        MissionObjective(id="kill_boss", description="Destroy the boss",
                         type="defeat", count=1),
    ]
    gw = _gamewiring(_mission_project(tmp_path, objectives=objs))
    assert 'ReportProgress("cull_drones")' in gw
    assert 'ReportProgress("kill_boss")' not in gw


# ── SEAM 8c — reach-target relay (multiple `reach` objectives, one scene) ─────
# Live bug (DropZone): a mission with TWO `reach` objectives shares one scene's
# single TriggerZone, which can report only ONE arrival — the same
# one-source-per-type rule as the eliminate test above, but here the SECOND
# objective's own target was a real, named, spawned entity sitting right in
# the schema (FormationAISkydivers) rather than nothing at all.
# WireReachTargetRelay closes it: a purpose-built relay attaches straight to
# that entity's own GameObject(s), independent of the shared trigger, so any
# number of `reach` objectives per scene can bind as long as each names a
# real entity.

def test_second_reach_objective_in_one_scene_binds_via_target_relay(tmp_path):
    objs = [
        MissionObjective(id="reach_vault", description="Get to the vault", type="reach"),
        MissionObjective(id="reach_locker", description="Reach the gear locker",
                         type="reach", target_ref="GearLocker"),
    ]
    gw = _gamewiring(_mission_project(
        tmp_path, objectives=objs, reach_target_entities={"GearLocker": 1}))
    # The FIRST reach objective still claims the shared trigger, unchanged.
    assert "_scenetrigger.OnActivated += _onReachObjective;" in gw
    assert 'ReportProgress("reach_vault")' in gw
    # The SECOND gets its own relay instead of staying unbound.
    assert ('StartCoroutine(ReachTargetRelaySetup("GearLocker", 1, '
            '() => _vaultrunobjectives.ReportProgress("reach_locker"), 5.0f));') in gw
    assert "private IEnumerator ReachTargetRelaySetup(" in gw
    assert "public class ReachTargetRelay : MonoBehaviour" in gw
    assert "no event source for reach_locker" not in gw


def test_reach_target_relay_uses_3d_collider_for_3d_game(tmp_path):
    objs = [MissionObjective(id="reach_vault", description="Get to the vault", type="reach"),
            MissionObjective(id="reach_locker", description="Reach the locker",
                             type="reach", target_ref="GearLocker")]
    gw = _gamewiring(_mission_project(
        tmp_path, objectives=objs, game_type="3D",
        reach_target_entities={"GearLocker": 1}))
    assert "gameObject.AddComponent<SphereCollider>();" in gw
    assert "private void OnTriggerEnter(Collider other)" in gw
    assert "CircleCollider2D" not in gw


def test_reach_target_relay_uses_2d_collider_for_2d_game(tmp_path):
    objs = [MissionObjective(id="reach_vault", description="Get to the vault", type="reach"),
            MissionObjective(id="reach_locker", description="Reach the locker",
                             type="reach", target_ref="GearLocker")]
    gw = _gamewiring(_mission_project(
        tmp_path, objectives=objs, game_type="2D",
        reach_target_entities={"GearLocker": 1}))
    assert "gameObject.AddComponent<CircleCollider2D>();" in gw
    assert "private void OnTriggerEnter2D(Collider2D other)" in gw
    assert "SphereCollider" not in gw


def test_reach_target_relay_omitted_without_a_matching_entity(tmp_path):
    """No reach_target_entities passed at all -- the second reach objective is
    named as unbound exactly as it was before this seam existed, not silently
    wired to a target that was never confirmed to exist."""
    objs = [MissionObjective(id="reach_vault", description="Get to the vault", type="reach"),
            MissionObjective(id="reach_locker", description="Reach the locker",
                             type="reach", target_ref="GearLocker")]
    gw = _gamewiring(_mission_project(tmp_path, objectives=objs))
    assert "ReachTargetRelaySetup" not in gw
    assert "public class ReachTargetRelay" not in gw


def test_reach_target_relay_relays_every_instance_of_a_multi_count_target(tmp_path):
    """`target_ref` naming a multi-instance entity (e.g. a 3-member formation)
    relays EVERY spawned instance — reaching any one member counts, matching
    EntitySpawner.cs.j2's own '<name>_0', '<name>_1', ... naming."""
    objs = [MissionObjective(id="reach_vault", description="Get to the vault", type="reach"),
            MissionObjective(id="reach_formation", description="Reach the formation",
                             type="reach", target_ref="Skydivers")]
    gw = _gamewiring(_mission_project(
        tmp_path, objectives=objs, reach_target_entities={"Skydivers": 3}))
    assert ('StartCoroutine(ReachTargetRelaySetup("Skydivers", 3, '
            '() => _vaultrunobjectives.ReportProgress("reach_formation"), 5.0f));') in gw


# ── Scene-scoped binding (multi-mission, multi-scene projects) ────────────────
# Live bug (GearCitadel, 3 missions across 3 hall scenes): the single global
# trigger/interactable meant only the FIRST mission's `reach`/`collect`
# objectives could ever bind — every other mission's were permanently
# unadvanceable, and playability.txt marked the build NOT PLAYABLE. Passing
# `objective_scenes` (what src/unity_pipeline.py's `_objective_scene_map`
# derives from `_infer_mission_scenes`) lets each scene's own local trigger/
# interactable instance bind its OWN mission's objectives, gated at runtime
# on `_scene == "..."` — the same idiom EntitySpawner.cs.j2 already uses.

def test_two_missions_scoped_to_different_scenes_each_get_gated_binding(tmp_path):
    m1 = MissionSpec(id="m1", title="Hall One Clearance", objectives=[
        MissionObjective(id="reach_gate_1", description="Reach the gate", type="reach"),
    ])
    m2 = MissionSpec(id="m2", title="Vertical Ascent", objectives=[
        MissionObjective(id="reach_gate_2", description="Reach the other gate", type="reach"),
    ])
    gw = _gamewiring(_multi_mission_project(
        tmp_path, missions=[m1, m2],
        objective_scenes={"HallOneClearanceObjectives.reach_gate_1": "HallOne",
                          "VerticalAscentObjectives.reach_gate_2": "HallTwo"}))
    assert 'if (_scene == "HallOne")' in gw
    assert 'if (_scene == "HallTwo")' in gw
    assert 'ReportProgress("reach_gate_1")' in gw
    assert 'ReportProgress("reach_gate_2")' in gw
    # Both are real bindings, not just named/abstained.
    assert "no event source for reach_gate_2" not in gw


def test_scene_gated_delegates_get_distinct_field_names(tmp_path):
    """Two scene buckets binding the SAME objective type must not both
    declare `_onReachObjective` — a duplicate C# field (CS0102)."""
    m1 = MissionSpec(id="m1", title="Hall One Clearance", objectives=[
        MissionObjective(id="reach_gate_1", description="Reach the gate", type="reach"),
    ])
    m2 = MissionSpec(id="m2", title="Vertical Ascent", objectives=[
        MissionObjective(id="reach_gate_2", description="Reach the other gate", type="reach"),
    ])
    gw = _gamewiring(_multi_mission_project(
        tmp_path, missions=[m1, m2],
        objective_scenes={"HallOneClearanceObjectives.reach_gate_1": "HallOne",
                          "VerticalAscentObjectives.reach_gate_2": "HallTwo"}))
    assert "private Action _onReachObjective;" not in gw   # bare/unqualified — would be a duplicate
    assert "private Action _onReachObjective_HallOne;" in gw
    assert "private Action _onReachObjective_HallTwo;" in gw
    # OnDestroy unsubscribes the SAME qualified delegate it subscribed.
    destroy = gw.split("private void OnDestroy()")[1]
    assert "_onReachObjective_HallOne" in destroy
    assert "_onReachObjective_HallTwo" in destroy


def test_unmapped_mission_keeps_todays_global_ungated_binding(tmp_path):
    """A mission `_infer_mission_scenes` could not resolve (absent from
    `objective_scenes`) must bind exactly as it did before this feature
    existed — no scene gate, no behavior change."""
    objs = [MissionObjective(id="reach_it", description="Reach it", type="reach")]
    gated   = _gamewiring(_multi_mission_project(
        tmp_path, project="Gated", missions=[MissionSpec(id="m1", title="X", objectives=objs)],
        objective_scenes=None))
    ungated_baseline = _gamewiring(_mission_project(tmp_path, project="Baseline", objectives=objs))
    assert 'if (_scene ==' not in gated
    assert 'ReportProgress("reach_it")' in gated
    # Same shape as the pre-existing single-mission path (module-level
    # regression guard, not a byte-diff — field ordering/comments can differ
    # with a different project name).
    assert ('if (_scene ==' not in ungated_baseline
            and 'ReportProgress("reach_it")' in ungated_baseline)


def test_no_scene_var_declared_when_nothing_needs_it(tmp_path):
    """The common case (single mission, or every project before this
    feature) must not gain the SceneManager using/`_scene` local at all."""
    gw = _gamewiring(_mission_project(tmp_path, objectives=[
        MissionObjective(id="reach_it", description="Reach it", type="reach"),
    ]))
    assert "SceneManager" not in gw
    assert "_scene =" not in gw


def test_no_mission_means_no_seam(tmp_path):
    """Projects without authored objectives compose exactly as before."""
    assets = Path(tmp_path) / "Assets"
    MicroComposer("NoMission").compose(subsystems_for("3D", []), assets)
    assert "SEAM 8" not in _gamewiring(assets)


# ── The vocabulary bug SEAM 8 surfaced ────────────────────────────────────────

def test_defeat_objective_survives_the_schema_to_micro_boundary(tmp_path):
    """unity_schema.MissionObjType says `defeat`/`timer`; the micro's ObjectiveType
    says `eliminate`/`interact`. The mismatch used to fall through _clamp_type's
    fallback and rewrite both to "reach" — silently, and for every kill objective
    ever authored."""
    from src.micro.objectives import ObjectiveEntry
    assert ObjectiveEntry(id="x", type="defeat").type == "eliminate"
    assert ObjectiveEntry(id="x", type="timer").type == "survive"
    assert ObjectiveEntry(id="x", type="nonsense").type == "reach"   # still clamps
    obj = _micro_cs(_mission_project(tmp_path, objectives=[
        MissionObjective(id="cull_drones", description="Destroy 8 drones",
                         type="defeat", count=8)]), "objectives")
    assert 'type = "eliminate"' in obj


# ── MissionSpec.triggers reaching the trigger micro ───────────────────────────

def test_trigger_actions_come_from_the_authored_mission(tmp_path):
    """schema.missions[].triggers was present in the IR and dropped by the
    composer; the trigger micro always fell back to its one default action."""
    src = _micro_cs(_mission_project(
        tmp_path, objectives=_ALL_TYPES,
        triggers=["player enters the pump house", "reactor alarm sounds"]), "trigger")
    assert 'SendMessage("OnPlayerEntersThePumpHouse"' in src
    assert 'SendMessage("OnReactorAlarmSounds"' in src


def test_on_trigger_activate_is_always_kept_for_seam7(tmp_path):
    """SEAM 7's CheckpointSaveRelay listens for exactly this message — replacing
    the action list rather than extending it would break checkpoint saving."""
    src = _micro_cs(_mission_project(
        tmp_path, objectives=_ALL_TYPES,
        triggers=["player enters the pump house"]), "trigger")
    assert 'SendMessage("OnTriggerActivate"' in src


def test_mission_actions_dedupe_and_preserve_dimension_meta(tmp_path):
    """2D projects branch their collider callbacks on meta.game_type; merging the
    mission actions in must not clobber it."""
    src = _micro_cs(_mission_project(
        tmp_path, objectives=_ALL_TYPES, game_type="2D",
        triggers=["alarm sounds", "alarm sounds"]), "trigger")
    assert src.count('SendMessage("OnAlarmSounds"') == 1
    assert "OnTriggerEnter2D" in src


# ── The public surfaces the seam required ─────────────────────────────────────

def test_spawner_reports_despawns_every_frame(tmp_path):
    """A wave_spawner stops calling SpawnOne() once its waves finish, so pruning
    only on spawn would stop counting kills exactly as the last wave clears."""
    src = _micro_cs(_mission_project(tmp_path, objectives=_ALL_TYPES), "spawner")
    assert "public event Action<GameObject> OnDespawned;" in src
    assert "private void LateUpdate() => Prune();" in src
    assert "OnDespawned?.Invoke(go);" in src


def test_trigger_exposes_a_typed_event_alongside_sendmessage(tmp_path):
    src = _micro_cs(_mission_project(tmp_path, objectives=_ALL_TYPES), "trigger")
    assert "public event Action OnActivated;" in src
    assert "OnActivated?.Invoke();" in src
    assert "SendMessage(" in src        # loose hook retained for authored scripts


def test_objectives_exposes_a_readout_the_hud_can_bind(tmp_path):
    src = _micro_cs(_mission_project(tmp_path, objectives=_ALL_TYPES), "objectives")
    for member in ("public string Title =>", "public int CompletedRequired",
                   "public int TotalRequired", "public Objective ActiveObjective"):
        assert member in src


def test_an_unreadable_wiring_report_is_ungraded_not_failed():
    """A report that declares objectives and accounts for NONE of them predates
    `objectives_self_driven` — a survive-only mission from that era was
    correctly self-driven and left out of `objectives_unbound`, so the silence
    looks identical to a build where nothing bound. Grading it ERROR accuses an
    older project of being unwinnable on evidence that was never recorded."""
    from src.unity_playability import evaluate
    from src.unity_schema import EntitySpec, MissionObjective, MissionSpec, UnitySchema

    s = UnitySchema(
        project_name="Legacy", game_type="3D", platform="pc", render_pipeline="URP",
        entities=[EntitySpec(name="Hero", kind="player", tag="Player")],
        missions=[MissionSpec(id="m1", title="Hold", objectives=[
            MissionObjective(id="o1", type="survive", description="survive")])])
    s.meta["wiring"] = {                      # the pre-self_driven shape
        "seams": ["SEAM 7", "SEAM 9"], "objectives_bound": [],
        "objectives_unbound": [], "objectives_total": 1, "hud_objective": True,
    }
    rep = evaluate(s)
    assert rep.checks.get("mission_loop") is None, "must not be graded"
    assert "predates" in rep.not_graded.get("mission_loop", "")
    assert not [f for f in rep.errors if f.check == "mission_loop"]


def test_a_current_report_with_nothing_bound_still_fails():
    """The guard above must not swallow a real unwinnable build: a report that
    DOES account for its objectives, as unbound, is still an ERROR."""
    from src.unity_playability import evaluate
    from src.unity_schema import EntitySpec, MissionObjective, MissionSpec, UnitySchema

    s = UnitySchema(
        project_name="Dead", game_type="3D", platform="pc", render_pipeline="URP",
        entities=[EntitySpec(name="Hero", kind="player", tag="Player")],
        missions=[MissionSpec(id="m1", title="Escort", objectives=[
            MissionObjective(id="o1", type="escort", description="escort")])])
    s.meta["wiring"] = {
        "seams": [], "objectives_bound": [], "objectives_unbound": ["Escort.o1 (escort)"],
        "objectives_self_driven": [], "objectives_total": 1,
        "objectives_required": 1, "hud_objective": True,
    }
    rep = evaluate(s)
    assert rep.checks.get("mission_loop") is False
    assert [f for f in rep.errors if f.check == "mission_loop"]
