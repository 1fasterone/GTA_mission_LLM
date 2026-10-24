"""Emit the canonical bullet/rock Slice 3 fallback acceptance scripts."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.unity_compose import MicroComposer, Subsystem
from src.unity_projectile_contract import physical_projectile_contract_for

ACCEPTANCE_CLASS = "ProjectileSlice3Acceptance"
ACCEPTANCE_SCRIPTS = (
    f"{ACCEPTANCE_CLASS}.cs",
    "ProjectileController.cs",
    "ProjectilePool.cs",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_path", type=Path)
    args = parser.parse_args()

    bullet = physical_projectile_contract_for(
        "3D", "A rifle firing visible physical bullets."
    )
    rock = physical_projectile_contract_for(
        "3D", "A hero throwing rocks at moving targets."
    )
    if bullet is None or rock is None:
        raise RuntimeError("Slice 3 projectile profiles were not selected")

    visual_specs = []
    for contract in (bullet, rock):
        visual = dict(contract["projectile_visual"])
        visual.update({"resolution": "primitive", "resource_path": ""})
        visual_specs.append(visual)
    input_reader = Subsystem(
        kind="input",
        variant="keyboard_mouse",
        target="systems",
        name="Slice3Input",
    )
    controller = Subsystem(
        kind="controller",
        variant="third_person",
        target="player",
        name=ACCEPTANCE_CLASS,
        options={
            "input_backend": "legacy",
            "abilities": ["shoot"],
            "weapons": [dict(bullet["weapon"]), dict(rock["weapon"])],
            "meta": {"projectile_visuals": visual_specs},
        },
    )

    with tempfile.TemporaryDirectory(prefix="projectile-slice3-") as temp_dir:
        staging_assets = Path(temp_dir) / "Assets"
        units = MicroComposer("ProjectileSlice3Acceptance").emit(
            [input_reader, controller], staging_assets
        )
        generated_scripts = staging_assets / "Micros" / "controller" / "Scripts"
        destination = args.project_path / "Assets" / "Micros" / "controller" / "Scripts"
        destination.mkdir(parents=True, exist_ok=True)
        for filename in ACCEPTANCE_SCRIPTS:
            source = generated_scripts / filename
            if not source.is_file():
                raise RuntimeError(f"missing generated Slice 3 script: {source}")
            shutil.copy2(source, destination / filename)
        input_source = staging_assets / "Micros" / "input"
        input_destination = args.project_path / "Assets" / "Micros" / "input"
        if not input_source.is_dir():
            raise RuntimeError(f"missing generated input micro: {input_source}")
        shutil.copytree(input_source, input_destination, dirs_exist_ok=True)

    print("generated_units=" + ",".join(unit.class_name for unit in units))
    print("accepted_projectile_files=" + ",".join(ACCEPTANCE_SCRIPTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
