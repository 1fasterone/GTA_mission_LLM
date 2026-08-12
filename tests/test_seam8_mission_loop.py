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
                     entities=None, game_type="3D") -> Path:
    """Compose a whole project around one authored mission; return its Assets dir."""
    mission = MissionSpec(id="m1", title="Vault Run", triggers=list(triggers),
                          objectives=objectives)
    ents = entities if entities is not None else [
        EntitySpec(name="Sentry Drone", kind="enemy", count=8)]
    subs = subsystems_for(game_type, [], missions=[mission], entities=ents)
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project, project_config={"audio": {"backend": "dsp"}}).compose(
        subs, assets)
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
                         ("objectives", "public void ReportProgress(string objectiveId")):
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
