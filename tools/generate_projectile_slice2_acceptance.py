"""Emit generated projectile companions into a Unity acceptance project."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.unity_compose import MicroComposer, subsystems_for
from src.unity_projectile_contract import physical_projectile_contract_for

PROJECTILE_COMPANIONS = ("ProjectileController.cs", "ProjectilePool.cs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_path", type=Path)
    args = parser.parse_args()

    contract = physical_projectile_contract_for(
        "3D", "A rifle firing pooled physical bullets at thin targets."
    )
    if contract is None:
        raise RuntimeError("physical projectile contract was not selected")

    with tempfile.TemporaryDirectory(prefix="projectile-slice2-") as temp_dir:
        staging_assets = Path(temp_dir) / "Assets"
        units = MicroComposer("ProjectileSlice2Acceptance").emit(
            subsystems_for("3D", [], projectile_contract=contract),
            staging_assets,
        )
        generated_scripts = (
            staging_assets / "Micros" / "controller" / "Scripts"
        )
        destination = (
            args.project_path / "Assets" / "Micros" / "controller" / "Scripts"
        )
        destination.mkdir(parents=True, exist_ok=True)
        for filename in PROJECTILE_COMPANIONS:
            source = generated_scripts / filename
            if not source.is_file():
                raise RuntimeError(f"missing generated projectile companion: {source}")
            shutil.copy2(source, destination / filename)

    print("generated_units=" + ",".join(unit.class_name for unit in units))
    print("accepted_projectile_files=" + ",".join(PROJECTILE_COMPANIONS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
