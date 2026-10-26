"""Emit Slice 3 projectile sources into a disposable Unity acceptance project."""

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

PROJECTILE_SOURCES = (
    "PlayerController.cs",
    "ProjectileController.cs",
    "ProjectilePool.cs",
)


def _emit_profile(
    brief: str,
    destination: Path,
    *,
    full_micro_tree: bool,
) -> tuple[str, list[str]]:
    contract = physical_projectile_contract_for("3D", brief)
    if contract is None:
        raise RuntimeError("physical projectile contract was not selected")

    with tempfile.TemporaryDirectory(prefix="projectile-slice3-") as temp_dir:
        staging_assets = Path(temp_dir) / "Assets"
        units = MicroComposer("ProjectileSlice3Acceptance").emit(
            subsystems_for("3D", [], projectile_contract=contract),
            staging_assets,
        )
        generated_micros = staging_assets / "Micros"
        generated_scripts = generated_micros / "controller" / "Scripts"
        if full_micro_tree:
            shutil.copytree(generated_micros, destination, dirs_exist_ok=True)
        else:
            destination.mkdir(parents=True, exist_ok=True)
            for filename in PROJECTILE_SOURCES:
                source = generated_scripts / filename
                if not source.is_file():
                    raise RuntimeError(f"missing generated projectile source: {source}")
                shutil.copy2(source, destination / filename)

    return str(contract["motion_profile"]), [unit.class_name for unit in units]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_path", type=Path)
    args = parser.parse_args()

    runtime_destination = args.project_path / "Assets" / "Micros"
    motion_profile, units = _emit_profile(
        "A rifle firing pooled physical bullets at thin targets.",
        runtime_destination,
        full_micro_tree=True,
    )

    # The second closed profile is retained as evidence rather than compiled in
    # the same Unity assembly: both deterministic emissions intentionally own
    # the same canonical runtime class names.
    evidence_destination = args.project_path / "Slice3Evidence" / "thrown_object"
    thrown_profile, _ = _emit_profile(
        "Throw a pooled physical rock at the target.",
        evidence_destination,
        full_micro_tree=False,
    )

    print("runtime_motion_profile=" + motion_profile)
    print("evidence_motion_profile=" + thrown_profile)
    print("generated_units=" + ",".join(units))
    print("accepted_projectile_files=" + ",".join(PROJECTILE_SOURCES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
