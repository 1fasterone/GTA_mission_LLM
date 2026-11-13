"""Emit R3 (S5) firing-provider sources into a disposable Unity acceptance project.

Every C# file written here comes from a production emitter: two controllers
from ``MicroComposer.compose`` (one hitscan, one pooled ``projectile_3d``), the
``IInputReader`` interface the controller reads, copied verbatim from the real
input micro's emission, and the ``GameSequencer`` that ``UnityProjectGenerator``
renders for a schema whose ``atoms`` is ``[Shoot]``. The schemas are
engineer-authored acceptance fixtures, never Pipeline LLM output.

Only the Play Mode test (tests/unity_firing_provider/) is hand-written.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src import firing_provider  # noqa: E402
from src.atoms.base import AtomInvocation  # noqa: E402
from src.intent.projectile import physical_projectile_contract_for  # noqa: E402
from src.unity_compose import MicroComposer, Subsystem, subsystems_for  # noqa: E402
from src.unity_generator import UnityProjectGenerator  # noqa: E402
from src.unity_schema import UnitySchema  # noqa: E402

PROJECT = "FiringProviderAcceptance"
DESTINATION = Path("Assets") / "FiringProviderAcceptance"

# Small magazines so the Play Mode test walks every FireResult in a few
# seconds. fire_rate is a full second so every cadence assertion sits far from
# its boundary however long a batchmode frame takes; the test waits 1.1 s.
HITSCAN = Subsystem(kind="controller", variant="first_person", target="player",
                    name="HitscanShooter", options={
                        "input_backend": "legacy",
                        "abilities": ["shoot"],
                        "starting_reserve_ammo": 3,
                        "weapons": [{
                            "name": "Pistol", "damage": 10.0, "fire_rate": 1.0,
                            "magazine_size": 3, "reload_time": 0.3, "shoot_range": 50.0,
                            "delivery": "hitscan",
                        }],
                    })
BALLISTIC = Subsystem(kind="controller", variant="first_person", target="player",
                      name="BallisticShooter", options={
                          "input_backend": "legacy",
                          "abilities": ["shoot"],
                          "weapons": [{
                              "name": "Launcher", "damage": 10.0, "fire_rate": 0.05,
                              "magazine_size": 5, "reload_time": 0.3, "shoot_range": 50.0,
                              "delivery": "projectile_3d", "projectile_speed": 1.0,
                              "projectile_use_gravity": False, "projectile_lifetime": 30.0,
                              "projectile_pool_size": 1, "projectile_pool_growth": "fixed",
                          }],
                      })
INPUT = Subsystem(kind="input", variant="keyboard_mouse", target="systems",
                  name="InputReader")
# The Slice 4 firearm adapter, selected exactly as a fresh run selects it: the
# brief's closed fast_round contract handed through subsystems_for.
RIFLE_BRIEF = "An automatic rifle firing visible physical bullets."

_INTERFACE = re.compile(r"^public interface IInputReader\s*\{.*?^\}", re.S | re.M)


def _firearm_subsystem() -> Subsystem:
    contract = physical_projectile_contract_for("3D", RIFLE_BRIEF)
    if contract is None or "firearm_presentation" not in contract:
        raise RuntimeError("the rifle brief selected no firearm presentation contract")
    controller = next(sub for sub in subsystems_for("3D", [], projectile_contract=contract)
                      if sub.kind == "controller")
    options = deepcopy(controller.options)
    # The one declared accommodation: this project carries no Input System
    # package, so the controller reads the legacy input backend.
    options["input_backend"] = "legacy"
    return Subsystem(kind="controller", variant=controller.variant, target="player",
                     name="FirearmShooter", options=options)


def _compose(subsystem: Subsystem, staging: Path) -> tuple[dict[str, str], list]:
    units = MicroComposer(PROJECT).compose([subsystem], staging)
    scripts = staging / "Micros" / subsystem.kind / "Scripts"
    return ({path.name: path.read_text(encoding="utf-8") for path in sorted(scripts.glob("*.cs"))},
            units)


def emit(project_path: Path) -> dict[str, str]:
    """Write the acceptance sources; return {relative path: source}."""
    written: dict[str, str] = {}
    shared: dict[str, str] = {}
    controller_units = []
    with tempfile.TemporaryDirectory(prefix="firing-provider-") as temp_dir:
        for index, subsystem in enumerate((HITSCAN, BALLISTIC, _firearm_subsystem())):
            sources, units = _compose(subsystem, Path(temp_dir) / f"c{index}" / "Assets")
            controller_units.extend(unit for unit in units if unit.kind == "controller")
            for name, source in sources.items():
                if name == f"{subsystem.name}.cs":
                    written[name] = source
                elif shared.setdefault(name, source) != source:
                    # Both controllers ship the same helpers; a divergence would
                    # mean the acceptance compiled one and not the other.
                    raise RuntimeError(f"controllers emitted different {name}")
        reader, _ = _compose(INPUT, Path(temp_dir) / "input" / "Assets")

    written.update(shared)
    if firing_provider.SCRIPT not in written:
        raise RuntimeError("the shooting controllers emitted no firing contract")
    interface = next((m.group(0) for source in reader.values()
                      if (m := _INTERFACE.search(source))), None)
    if interface is None:
        raise RuntimeError("the input micro emitted no IInputReader interface")
    written["IInputReader.cs"] = (
        "// Copied verbatim from the input micro's emission (InputReader.cs); the rest\n"
        "// of that file needs the Input System package this project does not carry.\n"
        + interface + "\n")

    schema = UnitySchema(project_name=PROJECT, game_type="3D", platform="PC",
                         render_pipeline="URP", scenes=["Main"],
                         atoms=[AtomInvocation(atom="Shoot")])
    generator = UnityProjectGenerator(str(REPOSITORY_ROOT / "templates" / "unity"))
    files = generator.generate(schema, {}, composed_units=controller_units[:1])
    route = generator.last_atom_bindings.route("atoms")
    binding = route.atoms[0] if route is not None and route.atoms else None
    if (binding is None or binding.runtime_provider is None
            or binding.runtime_provider.implementer != HITSCAN.name):
        raise RuntimeError(f"Shoot did not bind to {HITSCAN.name}: {binding!r}")
    if f"{PROJECT}/Assets/Scripts/{firing_provider.SCRIPT}" in files:
        raise RuntimeError("a contract-only copy was emitted beside an implementer")
    written["GameSequencer.cs"] = files[f"{PROJECT}/Assets/Scripts/GameSequencer.cs"]

    destination = project_path / DESTINATION
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in sorted(written.items()):
        (destination / name).write_text(source, encoding="utf-8")
    return {str(DESTINATION / name).replace("\\", "/"): source
            for name, source in sorted(written.items())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_path", type=Path)
    args = parser.parse_args()
    written = emit(args.project_path)
    print("firing_provider_sources=" + ",".join(sorted(written)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
