"""E2E: a rich schema against the REAL asset library must produce a varied
scene — many distinct real models wired into EntitySpawner, binaries + .metas
on disk. Pure Python (no LLM, no Unity); the Unity Editor compile gate stays
behind `pytest -m unity_compile`.

This is the regression gate for the original symptom: 100s of Kenney assets on
disk but every generated scene spawning the same 1-2 models.
"""

import re
from pathlib import Path

import pytest

from src.unity_generator import UnityProjectGenerator
from src.unity_schema import EntitySpec, UnitySchema

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "Models" / "FBX format").is_dir()
    or not (REPO_ROOT / "lookups" / "unity" / "asset_catalog.json").is_file(),
    reason="real asset library / generated catalog not present",
)


def _rich_schema() -> UnitySchema:
    """Skatepark-in-a-forest: 11 entity groups a real prompt would produce."""
    return UnitySchema(
        project_name="AssetE2E", company_name="Test",
        description="a skatepark in a pine forest with ramps, rails and a crowd",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", scenes=["Main"],
        entities=[
            EntitySpec(name="PineTrees",   kind="tree",  count=12, placement="scatter",
                       model_hint="pine tree"),
            EntitySpec(name="BigRamps",    kind="ramp",  count=3,
                       model_hint="wooden ramp"),
            EntitySpec(name="GrindRails",  kind="rail",  count=4, placement="line",
                       model_hint="grind rail"),
            EntitySpec(name="Banners",     kind="prop",  count=6, placement="ring",
                       model_hint="hanging banner"),
            EntitySpec(name="CrateStacks", kind="prop",  count=5, placement="scatter",
                       model_hint="wooden pallet crate"),
            EntitySpec(name="Fans",        kind="prop",  count=2,
                       model_hint="spinning fan"),
            EntitySpec(name="HalfPipe",    kind="prop",  count=1,
                       model_hint="half pipe"),
            EntitySpec(name="BowlCorners", kind="prop",  count=4,
                       model_hint="bowl corner"),
            EntitySpec(name="Tunnels",     kind="prop",  count=2,
                       model_hint="tunnel tube"),
            EntitySpec(name="Steps",       kind="prop",  count=2,
                       model_hint="steps staircase"),
            EntitySpec(name="Skaters",     kind="npc",   count=3, placement="scatter",
                       model_hint="skater character"),
        ],
    )


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    gen = UnityProjectGenerator(template_dir=str(REPO_ROOT / "templates" / "unity"))
    out = tmp_path_factory.mktemp("e2e")
    import os
    cwd = os.getcwd()
    os.chdir(REPO_ROOT)          # lookups/ + Models/ resolve relative to repo root
    try:
        root, count = gen.write(_rich_schema(), {}, output_dir=str(out),
                                strict_assets=True)
    finally:
        os.chdir(cwd)
    return root


def test_scene_uses_many_distinct_models(project):
    cs = (project / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(encoding="utf-8")
    model_paths = set(re.findall(r'"(Models/[^"]+)"', cs))
    assert len(model_paths) >= 5, f"only {sorted(model_paths)}"
    kenney = {p for p in model_paths if p.startswith("Models/Kenney/")}
    assert len(kenney) >= 4, f"catalog picks missing: {sorted(model_paths)}"


def test_every_model_binary_and_meta_on_disk(project):
    cs = (project / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(encoding="utf-8")
    for res_path in set(re.findall(r'"(Models/[^"]+)"', cs)):
        fbx = project / "Assets" / "Resources" / f"{res_path}.fbx"
        assert fbx.is_file(), f"missing binary {fbx}"
        assert fbx.with_suffix(".fbx.meta").is_file(), f"missing meta for {fbx}"


def test_textures_ship_with_catalog_picks(project):
    tex = project / "Assets" / "Resources" / "Models" / "Kenney" / "Textures"
    assert tex.is_dir() and any(tex.glob("*.png")), "Kenney textures not copied"
    assert (project / "Assets" / "Models" / "Textures").is_dir()


def test_strict_mode_passed(project):
    # write() above ran with strict_assets=True — reaching here proves every
    # entity resolved to a real model with zero silent fallbacks.
    assert (project / "Assets" / "Scripts" / "ModelSwap.cs").is_file()
