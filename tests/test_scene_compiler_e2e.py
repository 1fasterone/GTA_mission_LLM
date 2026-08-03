"""E2E: a Scene Compiler layout must survive all the way into the generated
Unity project — the author's own model binaries, .metas, textures, and the
exact transforms they arranged, with the generated wiring left alone.

Pure Python (no LLM, no Unity Editor). The layout JSON and the fake FBX bytes
here are TEST DATA under tests/, not pipeline output: the pieces they exercise
(src/unity_sceneinput.py -> src/unity_scenebind.py -> the generator) are all
post-schema deterministic steps.
"""

import json
import re
from pathlib import Path

import pytest

from src.unity_generator import UnityProjectGenerator
from src.unity_scenebind import apply_scene
from src.unity_sceneinput import build_scene
from src.unity_schema import EntitySpec, UnitySchema, UnityScript

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sc_models(tmp_path):
    """A stand-in for the Scene Compiler's models/ folder."""
    root = tmp_path / "sc" / "models"
    (root / "Textures").mkdir(parents=True)
    (root / "monaco-gp-circuit.fbx").write_bytes(b"FBXCIRCUIT")
    (root / "victorian-monobike.fbx").write_bytes(b"FBXBIKE")
    (root / "Textures" / "asphalt.png").write_bytes(b"PNG")
    return root


@pytest.fixture
def layout():
    """Three bikes on a grid beside a circuit — the MONACO.json shape."""
    def _it(name, file, pos, yaw):
        return {"name": name, "file": file, "type": "model", "orient": None,
                "position": list(pos), "yaw_deg": yaw, "scale": 1,
                "base_scale": 1}
    return {"version": 2, "units": "meters", "view_mode": "top", "items": [
        _it("circuit_01", "monaco-gp-circuit.fbx", (0.5, 0, 0), 0),
        _it("bike_01", "victorian-monobike.fbx", (-64, 0, -40), 150),
        _it("bike_02", "victorian-monobike.fbx", (-65.5, 0, -41), 150),
        _it("bike_03", "victorian-monobike.fbx", (-67, 0, -42), 150),
    ]}


def _schema():
    """What the pipeline would hand Step 5: a racing project whose entities
    name a track and rival bikes, plus a group the layout says nothing about."""
    return UnitySchema(
        project_name="SceneCompilerE2E", company_name="Test",
        description="a victorian monobike race around the monaco circuit",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", scenes=["Main"], locomotion="drive",
        entities=[
            EntitySpec(name="MonacoCircuit", kind="road", count=1),
            EntitySpec(name="MonobikeRivals", kind="vehicle", count=8,
                       placement="scatter", model_hint="victorian monobike"),
            EntitySpec(name="Spectators", kind="crowd", count=10,
                       placement="ring", model_hint="cheering crowd"),
        ],
    )


def _grid_schema():
    """The live Monaco shape: a singleton Player machine plus named rivals,
    against a layout that holds three copies of one bike."""
    return UnitySchema(
        project_name="SceneCompilerGrid", company_name="Test",
        description="a victorian monobike race around the monaco circuit",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", scenes=["Main"], locomotion="drive",
        scripts=[UnityScript(name="PlayerController", type="player",
                             description="drives the player's monobike")],
        entities=[
            EntitySpec(name="MonacoCircuit", kind="road", count=1),
            EntitySpec(name="PlayerMonobike", kind="vehicle", count=1,
                       tag="Player", model_hint="victorian monobike"),
            EntitySpec(name="RivalMonobikeOne", kind="vehicle", count=1),
            EntitySpec(name="RivalMonobikeTwo", kind="vehicle", count=1),
        ],
    )


@pytest.fixture
def project(tmp_path, sc_models, layout):
    scene, warnings = build_scene(layout, sc_models)
    assert warnings == []          # base_scale present -> nothing to warn about
    schema = _schema()
    apply_scene(schema, scene)

    gen = UnityProjectGenerator(
        template_dir=str(REPO_ROOT / "templates" / "unity"))
    out = tmp_path / "out"
    import os
    cwd = os.getcwd()
    os.chdir(REPO_ROOT)            # lookups/ resolves relative to repo root
    try:
        root, _ = gen.write(schema, {}, output_dir=str(out))
    finally:
        os.chdir(cwd)
    return root, schema


# ── the models ───────────────────────────────────────────────────────────────

def test_authored_binaries_and_metas_ship(project):
    root, _ = project
    res = root / "Assets" / "Resources" / "Models" / "SceneCompiler" / "Authored"
    for name in ("monaco-gp-circuit.fbx", "victorian-monobike.fbx"):
        assert (res / name).is_file(), f"missing authored binary {name}"
        assert (res / f"{name}.meta").is_file(), f"missing .meta for {name}"
    # The user-visible mirror copy too.
    assert (root / "Assets" / "Models" / "Authored" / "monaco-gp-circuit.fbx").is_file()


def test_authored_textures_land_under_the_authored_subtree(project):
    root, _ = project
    tex = (root / "Assets" / "Resources" / "Models" / "SceneCompiler"
           / "Textures" / "asphalt.png")
    assert tex.is_file(), "authored textures must sit on the model's ancestor chain"


def test_spawner_loads_the_authored_models(project):
    root, _ = project
    cs = (root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(encoding="utf-8")
    assert "Models/SceneCompiler/Authored/monaco-gp-circuit" in cs
    assert "Models/SceneCompiler/Authored/victorian-monobike" in cs


# ── the placement ────────────────────────────────────────────────────────────

def test_exact_authored_transforms_reach_the_scene(project):
    root, schema = project
    cs = (root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(encoding="utf-8")
    # three.js (-64, 0, -40) -> Unity (-64, 0, 40); yaw 150 -> 210
    assert "new Vector3(-64.000f, 0.000f, 40.000f)" in cs
    assert "new Vector3(-65.500f, 0.000f, 41.000f)" in cs
    assert "new Vector3(-67.000f, 0.000f, 42.000f)" in cs
    assert "210.000f" in cs

    bikes = next(e for e in schema.entities if e.name == "MonobikeRivals")
    assert bikes.count == 3        # the layout's three, not the schema's eight


def test_the_layout_pass_did_not_move_them(project):
    _, schema = project
    bikes = next(e for e in schema.entities if e.name == "MonobikeRivals")
    assert bikes.instance_positions == [[-64.0, 0.0, 40.0], [-65.5, 0.0, 41.0],
                                        [-67.0, 0.0, 42.0]]


# ── everything the layout did NOT touch ──────────────────────────────────────

def test_uncovered_entities_still_resolve_through_the_catalog(project):
    root, schema = project
    cs = (root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(encoding="utf-8")
    spectators = re.search(r'SpawnGroup\("Spectators".*?"([^"]*)"[^"]*\);', cs,
                           re.S)
    assert spectators, "Spectators group missing from the spawner"
    # Whatever it resolved to, it is NOT an authored model — the layout has no
    # crowd, so the normal tiers still own that entity.
    assert "SceneCompiler" not in (spectators.group(1) or "")


def test_generated_wiring_is_untouched(project):
    root, _ = project
    scripts = root / "Assets" / "Scripts"
    assert (scripts / "ModelSwap.cs").is_file()
    assert (scripts / "EntitySpawner.cs").is_file()


def test_provenance_records_what_the_layout_decided(project):
    _, schema = project
    prov = schema.meta["authored_scene"]
    assert prov["groups"] == 2
    assert prov["bound"] == 2          # circuit -> MonacoCircuit, bike -> Rivals
    assert prov["injected"] == 0
    assert set(schema.meta["authored_models"]) == {"MonacoCircuit",
                                                   "MonobikeRivals"}


# ── the starting grid: the player must actually ride the authored bike ───────

@pytest.fixture
def grid_project(tmp_path, sc_models, layout):
    scene, _ = build_scene(layout, sc_models)
    schema = _grid_schema()
    apply_scene(schema, scene)

    gen = UnityProjectGenerator(
        template_dir=str(REPO_ROOT / "templates" / "unity"))
    import os
    cwd = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        files = gen.generate(schema, {})
    finally:
        os.chdir(cwd)
    return files, schema


def test_player_object_wears_the_authored_model(grid_project):
    """The regression the first live Monaco run exposed: all three bikes went
    to PlayerMonobike (count=3), which silently disqualifies it from the
    generator's player-merge — the player drove an invisible capsule."""
    files, _ = grid_project
    scene_yaml = next(v for k, v in files.items() if k.endswith(".unity"))
    assert "Models/SceneCompiler/Authored/victorian-monobike" in scene_yaml, (
        "the Player object did not receive the authored model — player-merge "
        "did not fire")


def test_player_spawns_at_its_authored_grid_slot(grid_project):
    files, schema = grid_project
    scene_yaml = next(v for k, v in files.items() if k.endswith(".unity"))
    player = next(e for e in schema.entities if e.name == "PlayerMonobike")
    assert player.count == 1
    assert player.instance_positions == [[-64.0, 0.0, 40.0]]   # pole position
    # The merged player object is lifted 1 m (capsule pivot is its center).
    assert "x: -64.0, y: 1.0, z: 40.0" in scene_yaml


def test_each_rival_got_one_authored_bike(grid_project):
    _, schema = grid_project
    rivals = [e for e in schema.entities if e.name.startswith("RivalMonobike")]
    assert [e.instance_positions for e in rivals] == [[[-65.5, 0.0, 41.0]],
                                                      [[-67.0, 0.0, 42.0]]]


def test_rebuild_is_byte_identical(tmp_path, sc_models, layout):
    """Determinism gate: same layout + same schema -> same project."""
    def _build(tag):
        scene, _ = build_scene(layout, sc_models)
        schema = _schema()
        apply_scene(schema, scene)
        gen = UnityProjectGenerator(
            template_dir=str(REPO_ROOT / "templates" / "unity"))
        import os
        cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            files = gen.generate(schema, {})
        finally:
            os.chdir(cwd)
        return files["SceneCompilerE2E/Assets/Scripts/EntitySpawner.cs"]

    assert _build("a") == _build("b")
