"""Emit firing-provider sources into a disposable Unity acceptance project.

R3 (S5) proved the Shoot atom and the fire input share one gate; R4 (S6) proves
the Reload atom and the reload input share one gate, in every controller
variant that implements the provider.

Every C# file written here comes from a production emitter:

- three R3 controllers from ``MicroComposer.compose`` (one hitscan, one pooled
  ``projectile_3d``, one Slice 4 firearm);
- one R4 reload shooter per firing variant (``reload_variants()``), each from
  ``MicroComposer.compose`` with the same small loadout;
- the ``IInputReader`` interface the controllers read, copied verbatim from the
  real input micro's emission;
- ``IClimbingState``, rendered from the project generator's own template (the
  project generator, not the micro, emits it beside a climbing controller);
- the ``GameSequencer`` and ``ArmoryBehavior`` that ``UnityProjectGenerator``
  renders for a schema whose ``atoms`` is ``[Shoot]`` and whose one entity's
  ``atoms`` is ``[Reload, Despawn]``.

The schemas are engineer-authored acceptance fixtures, never Pipeline LLM
output. Only the Play Mode tests (tests/unity_firing_provider/) are
hand-written.
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
from src.micro.controller import (  # noqa: E402
    VARIANTS,
    ControllerPipeline,
    ControllerSchema,
    provides_firing,
)
from src.unity_compose import MicroComposer, Subsystem, subsystems_for  # noqa: E402
from src.unity_generator import UnityProjectGenerator, entity_behavior_class_name  # noqa: E402
from src.unity_schema import EntitySpec, UnitySchema  # noqa: E402

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

# R4 (S6) reload shooters. Two rounds a magazine and three in reserve walk
# Started, AlreadyReloading, MagazineFull and NoReserveAmmo. fire_rate 0 keeps
# cadence out of the reload proof. reload_time is a full second, so a reload
# is still running after the two frames the atom host needs to start, however
# long a batchmode frame takes (Time.maximumDeltaTime caps one at 1/3 s); the
# test waits 1.3 s for it to finish.
RELOAD_OPTIONS = {
    "input_backend": "legacy",
    "abilities": ["shoot", "reload"],
    "starting_reserve_ammo": 3,
    "weapons": [{
        "name": "Sidearm", "damage": 10.0, "fire_rate": 0.0,
        "magazine_size": 2, "reload_time": 1.0, "shoot_range": 50.0,
        "delivery": "hitscan",
    }],
}
# The entity whose atoms route hosts the lowered Reload atom. Its sequence
# ends in Despawn (of itself), so EntityBehavior runs it exactly once; a
# sequence that does not end in Despawn loops (one pass per frame since #302),
# which would ask for a reload every frame.
RELOAD_ENTITY = "Armory"
RELOAD_ENTITY_ATOMS = [AtomInvocation(atom="Reload"),
                       AtomInvocation(atom="Despawn", args={"entity": RELOAD_ENTITY})]

_INTERFACE = re.compile(r"^public interface IInputReader\s*\{.*?^\}", re.S | re.M)
_CONFIG = {
    "lm_studio": {"base_url": "http://localhost:1234/v1", "model": "acceptance"},
    "unity_pipeline": {"unity_template_dir": str(REPOSITORY_ROOT / "templates" / "unity")},
}


def reload_variants() -> list[str]:
    """Every controller variant whose rendered class implements the provider.

    Read through the same predicate the controller micro and the atom
    bindings use, after the same ``default_fix`` pruning, so a variant that
    starts (or stops) implementing the provider changes this list.
    """
    pipeline = ControllerPipeline(_CONFIG)
    variants = []
    for variant in VARIANTS:
        schema = pipeline.default_fix(
            ControllerSchema(name="Probe", variant=variant,
                             input_backend="legacy",
                             abilities=list(RELOAD_OPTIONS["abilities"])),
            variant)
        if provides_firing(schema.abilities, variant):
            variants.append(variant)
    return variants


def reload_shooter_name(variant: str) -> str:
    """The class name of a variant's reload shooter (``2d_platformer`` -> ``Reload2dPlatformer``)."""
    return "Reload" + "".join(part[:1].upper() + part[1:] for part in variant.split("_"))


def _reload_subsystem(variant: str) -> Subsystem:
    return Subsystem(kind="controller", variant=variant, target="player",
                     name=reload_shooter_name(variant), options=deepcopy(RELOAD_OPTIONS))


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
    controllers = [HITSCAN, BALLISTIC, _firearm_subsystem(),
                   *(_reload_subsystem(variant) for variant in reload_variants())]
    with tempfile.TemporaryDirectory(prefix="firing-provider-") as temp_dir:
        for index, subsystem in enumerate(controllers):
            sources, units = _compose(subsystem, Path(temp_dir) / f"c{index}" / "Assets")
            composed = [unit for unit in units if unit.kind == "controller"]
            if [unit.class_name for unit in composed] != [subsystem.name]:
                raise RuntimeError(f"{subsystem.name} composed as "
                                   f"{[unit.class_name for unit in composed]}")
            controller_units.extend(composed)
            for name, source in sources.items():
                if name == f"{subsystem.name}.cs":
                    written[name] = source
                elif shared.setdefault(name, source) != source:
                    # Every controller ships the same helpers; a divergence would
                    # mean the acceptance compiled one and not another.
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

    generator = UnityProjectGenerator(str(REPOSITORY_ROOT / "templates" / "unity"))
    if "climbing" in reload_variants():
        # The project generator emits this contract beside a climbing
        # controller (its climbing_contract branch); the template takes no
        # context, so it is rendered directly from the generator's own env.
        written["IClimbingState.cs"] = generator._render("scripts/IClimbingState.cs.j2", {})

    schema = UnitySchema(project_name=PROJECT, game_type="3D", platform="PC",
                         render_pipeline="URP", scenes=["Main"],
                         atoms=[AtomInvocation(atom="Shoot")],
                         entities=[EntitySpec(name=RELOAD_ENTITY, kind="prop", count=1,
                                              atoms=list(RELOAD_ENTITY_ATOMS))])
    files = generator.generate(schema, {}, composed_units=controller_units[:1])
    for source, atom in (("atoms", "Shoot"), (f"entities[{RELOAD_ENTITY}].atoms", "Reload")):
        route = generator.last_atom_bindings.route(source)
        binding = route.atoms[0] if route is not None and route.atoms else None
        if (binding is None or binding.atom != atom or binding.runtime_provider is None
                or binding.runtime_provider.implementer != HITSCAN.name):
            raise RuntimeError(f"{atom} did not bind to {HITSCAN.name}: {binding!r}")
    if f"{PROJECT}/Assets/Scripts/{firing_provider.SCRIPT}" in files:
        raise RuntimeError("a contract-only copy was emitted beside an implementer")
    written["GameSequencer.cs"] = files[f"{PROJECT}/Assets/Scripts/GameSequencer.cs"]
    behavior = entity_behavior_class_name(RELOAD_ENTITY)
    written[f"{behavior}.cs"] = files[f"{PROJECT}/Assets/Scripts/{behavior}.cs"]
    if firing_provider.RELOAD_CALL not in written[f"{behavior}.cs"]:
        raise RuntimeError(f"{behavior} does not lower Reload through the provider")
    # Start() starts the sequence once. A loop (`while (true)` since #302) or a
    # second StartCoroutine(Run()) would repeat it; the acceptance needs one run.
    behavior_source = written[f"{behavior}.cs"]
    if (behavior_source.count("StartCoroutine(Run())") != 1
            or "while (true)" in behavior_source):
        raise RuntimeError(f"{behavior} repeats its sequence; the acceptance needs one run")

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
    # The runner requires one RELOAD_PROVIDER_VARIANT_PASS per shooter named here.
    print("reload_shooters=" + ",".join(reload_shooter_name(v) for v in reload_variants()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
