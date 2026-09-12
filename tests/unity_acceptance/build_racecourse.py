"""Emit a real Unity compiler acceptance project with labeled test data.

No Step 2/3 or asset-search claims: geometry and prefab identities are controlled
fixtures. Layout, project generation, EntitySpawner and micros are production code.
Run from the repository root: python tests/unity_acceptance/build_racecourse.py
"""
import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from test_racecourse_correctness import course_fixture  # noqa: E402
from src.unity_compose import MicroComposer, Subsystem  # noqa: E402
from src.unity_generator import UnityProjectGenerator  # noqa: E402
from src.unity_layout import compute_layout  # noqa: E402
from src.unity_schema import UnityScript  # noqa: E402


def build():
    schema = course_fixture()
    schema.scripts = [UnityScript(name="GameManager", type="game_manager",
                                  description="Compiler acceptance game lifecycle")]
    compute_layout(schema)
    subs = [
        Subsystem(kind="health", name="TestHealth", target="systems"),
        Subsystem(kind="inventory", name="TestInventory", target="systems"),
        Subsystem(kind="objectives", name="TestObjectives", target="systems",
                  options={"objectives": [{"id": "finish", "required_flag": "key"}]}),
        Subsystem(kind="interactable", variant="door", name="TestDoor", target="world",
                  options={"locked": True, "lock_id": "key"}),
    ]
    composer = MicroComposer(schema.project_name)
    units = composer.plan(subs)
    paths = {e.name: "Acceptance/Gate" if "Checkpoint" in e.name or "Arch" in e.name
             else "Acceptance/Prop" for e in schema.entities}
    paths["TrackSurface"] = "Acceptance/Road"
    generator = UnityProjectGenerator()
    # Explicit test seam: Unity constructs the controlled prefabs below.
    deps = {"com.unity.test-framework": "1.4.5", "com.unity.ugui": "2.0.0"}
    for module in ("physics", "physics2d", "audio", "animation", "particlesystem",
                   "imgui", "ui", "jsonserialize", "terrain", "terrainphysics", "imageconversion"):
        deps["com.unity.modules." + module] = "1.0.0"
    with patch.object(generator, "_resolve_all_models", return_value=({}, paths)):
        project, _ = generator.write(
            schema, deps,
            output_dir=str(ROOT / "output" / "acceptance"), composed_units=units)
    composer.compose(subs, project / "Assets", has_game_manager=True)
    (project / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.1.4f1\n", encoding="utf-8")
    editor = project / "Assets" / "Editor"
    editor.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("RacecourseFixtureAssets.cs"),
                    editor / "RacecourseFixtureAssets.cs")
    tests = project / "Assets" / "AcceptanceTests"
    tests.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("RacecourseAcceptance.cs"), tests / "RacecourseAcceptance.cs")
    shutil.copyfile(ROOT / "templates/unity/editor/PlayModeSmoke.Tests.asmdef",
                    tests / "PlayModeSmoke.Tests.asmdef")
    expected = {"groups": [{"name": e.name, "positions": [
        {"x": p[0], "y": p[1], "z": p[2]} for p in e.instance_positions]}
        for e in schema.entities[1:]]}
    (project / "expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    (project / "fixture-schema.json").write_text(schema.model_dump_json(indent=2), encoding="utf-8")
    print(project)


if __name__ == "__main__":
    build()
