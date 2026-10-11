"""Regression contracts for lazy runtime lookup initialization in Unity micros."""

from pathlib import Path

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_schema import AIBehaviorTree, BTNode, BehaviorTree, MissionObjective, MissionSpec


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = REPOSITORY_ROOT / "templates" / "unity"


def _composer() -> MicroComposer:
    return MicroComposer("RuntimeInitProof", template_dir=str(TEMPLATE_ROOT))


def _render_objectives(tmp_path: Path) -> str:
    mission = MissionSpec(
        id="m1",
        title="Vault Raid",
        objectives=[
            MissionObjective(
                id="crack_vault",
                description="Crack the vault door",
                type="interact",
                count=1,
            ),
        ],
    )
    subsystems = [
        subsystem
        for subsystem in subsystems_for("3D", [], missions=[mission])
        if subsystem.kind == "objectives"
    ]
    assets = tmp_path / "Assets"
    unit = _composer().compose(subsystems, assets)[0]
    return (assets / "Micros" / "objectives" / "Scripts" / f"{unit.class_name}.cs").read_text(
        encoding="utf-8"
    )


def _render_behavior_tree(tmp_path: Path) -> str:
    tree = AIBehaviorTree(
        id="bt1",
        name="GuardBrain",
        tree=BehaviorTree(
            root="root",
            nodes=[
                BTNode(id="root", type="selector", children=["idle"]),
                BTNode(id="idle", type="action", action="Idle"),
            ],
        ),
    )
    subsystems = [
        subsystem
        for subsystem in subsystems_for("3D", [], behavior_trees=[tree])
        if subsystem.kind == "behavior_tree"
    ]
    assets = tmp_path / "Assets"
    unit = _composer().compose(subsystems, assets)[0]
    return (assets / "Micros" / "behavior_tree" / "Scripts" / f"{unit.class_name}.cs").read_text(
        encoding="utf-8"
    )


def test_objectives_lazy_index_initialization_does_not_reset_progress(tmp_path):
    source = _render_objectives(tmp_path)

    assert "private void EnsureObjectiveIndex()" in source
    assert "if (_byId != null) return;" in source
    assert "private void Awake()\n    {\n        EnsureObjectiveIndex();" in source
    assert "public bool ReportProgress(string objectiveId, int amount = 1)\n    {\n        EnsureObjectiveIndex();" in source
    assert source.count("new Dictionary<string, Objective>();") == 1


def test_behavior_tree_lazy_index_initialization_covers_runtime_entry_points(tmp_path):
    source = _render_behavior_tree(tmp_path)

    assert "private void EnsureNodeIndex()" in source
    assert "if (_byId != null) return;" in source
    assert "private void Awake()\n    {\n        EnsureNodeIndex();" in source
    assert "private void Update()\n    {\n        EnsureNodeIndex();" in source
    assert "public BTStatus Evaluate(string nodeId)\n    {\n        EnsureNodeIndex();" in source
    assert source.count("new Dictionary<string, BTNodeDef>();") == 1
