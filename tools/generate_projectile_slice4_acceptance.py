"""Emit the canonical Slice 4 firearm adapter acceptance scripts."""

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

ACCEPTANCE_CLASS = "ProjectileSlice4Acceptance"
ACCEPTANCE_SCRIPTS = (
    f"{ACCEPTANCE_CLASS}.cs",
    "ProjectileController.cs",
    "ProjectilePool.cs",
)
INPUT_READER_STUB = """using System;
using UnityEngine;

public interface IInputReader
{
    event Action<Vector2> OnMove;
    event Action OnJump;
    event Action OnSprint;
    event Action OnSprintReleased;
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_path", type=Path)
    args = parser.parse_args()

    contract = physical_projectile_contract_for(
        "3D", "An automatic rifle firing visible physical bullets."
    )
    if contract is None:
        raise RuntimeError("Slice 4 firearm contract was not selected")
    visual = dict(contract["projectile_visual"])
    visual.update({"resolution": "primitive", "resource_path": ""})

    controller = Subsystem(
        kind="controller",
        variant="third_person",
        target="player",
        name=ACCEPTANCE_CLASS,
        options={
            "input_backend": "input_system",
            "abilities": ["shoot"],
            "weapons": [dict(contract["weapon"])],
            "meta": {
                "projectile_visuals": [visual],
                "firearm_presentation": dict(contract["firearm_presentation"]),
            },
        },
    )

    with tempfile.TemporaryDirectory(prefix="projectile-slice4-") as temp_dir:
        staging_assets = Path(temp_dir) / "Assets"
        units = MicroComposer("ProjectileSlice4Acceptance").emit(
            [controller], staging_assets
        )
        generated_scripts = staging_assets / "Micros" / "controller" / "Scripts"
        destination = args.project_path / "Assets" / "Micros" / "controller" / "Scripts"
        destination.mkdir(parents=True, exist_ok=True)
        for filename in ACCEPTANCE_SCRIPTS:
            source = generated_scripts / filename
            if not source.is_file():
                raise RuntimeError(f"missing generated Slice 4 script: {source}")
            shutil.copy2(source, destination / filename)
        (destination / "IInputReader.cs").write_text(
            INPUT_READER_STUB, encoding="utf-8"
        )

    print("generated_units=" + ",".join(unit.class_name for unit in units))
    print("accepted_projectile_files=" + ",".join(ACCEPTANCE_SCRIPTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
