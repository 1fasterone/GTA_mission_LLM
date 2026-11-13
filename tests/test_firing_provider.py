"""R3 (S5): one firing provider shared by the fire input and the Shoot atom.

Every schema and controller below is engineer-authored test data, never
Pipeline LLM output. The Roslyn tests compile against the real Unity reference
assemblies (`component_api.unity_editor`) and skip when the pinned SDK or the
Editor install is absent; REQUIRE_COMPONENT_API / REQUIRE_UNITY_REFERENCES turn
those skips into failures, as in tests/test_unity_authored_check.py.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from src import firing_provider
from src.atom_bindings import (
    BINDINGS_KEY,
    bind_atom,
    contract_implementers_for,
    derive_atom_bindings,
    stamp_atom_bindings,
)
from src.atoms import get_atom
from src.atoms.backends.unity import unity_atom_snippet, unity_coroutine_body
from src.atoms.base import AtomInvocation
from src.micro.controller import ControllerPipeline, ControllerSchema, WeaponDef, provides_firing
from src.unity_compose import ComposedUnit
from src.unity_generator import UnityProjectGenerator, _make_guid
from src.unity_schema import UnitySchema

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = f"BindProof/Assets/Scripts/{firing_provider.SCRIPT}"


@pytest.fixture
def dummy_config():
    """Pipeline ctor needs LMStudio config, but tests never call the LLM."""
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":  str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


def _shooter(tmp_path: Path, config: dict, variant: str = "first_person",
             abilities: list[str] | None = None,
             weapons: list[WeaponDef] | None = None) -> tuple[ControllerSchema, Path]:
    pipeline = ControllerPipeline(config)
    schema = ControllerSchema(name="Shooter", variant=variant, input_backend="legacy",
                              abilities=abilities if abilities is not None else ["shoot"],
                              weapons=weapons or [])
    schema = pipeline.default_fix(schema, variant)
    pipeline.write_files(schema, variant, tmp_path)
    return schema, tmp_path / "Scripts"


def _shoot(**args) -> AtomInvocation:
    return AtomInvocation(atom="Shoot", args=args)


def _schema(**overrides) -> UnitySchema:
    fields = dict(project_name="BindProof", game_type="3D", platform="PC",
                  render_pipeline="URP", scenes=["MainMenu", "Main"])
    fields.update(overrides)
    return UnitySchema(**fields)


def _controller_unit(abilities: list[str], variant: str = "first_person") -> ComposedUnit:
    return ComposedUnit(kind="controller", variant=variant, class_name="Shooter",
                        guid=_make_guid("BindProof/Micros/controller/Shooter"),
                        target="player", abilities=abilities)


def _generate(schema: UnitySchema, units: list[ComposedUnit] | None = None
              ) -> tuple[UnityProjectGenerator, dict[str, str]]:
    generator = UnityProjectGenerator("templates/unity")
    return generator, generator.generate(schema, {}, composed_units=units)


def _shoot_binding(generator: UnityProjectGenerator):
    route = generator.last_atom_bindings.route("atoms")
    assert route is not None
    return next(binding for binding in route.atoms if binding.atom == "Shoot")


# ── the controller's one gate ─────────────────────────────────────────────────

@pytest.mark.parametrize("delivery", ["hitscan", "projectile_3d"])
def test_fire_input_and_shoot_atom_reach_one_gate(tmp_path, dummy_config, delivery):
    _, scripts = _shooter(tmp_path, dummy_config, weapons=[WeaponDef(delivery=delivery)])
    controller = (scripts / "Shooter.cs").read_text(encoding="utf-8")

    assert "public class Shooter : MonoBehaviour, IFiringProvider" in controller
    assert "if (fireDown) TryFire();" in controller
    gate = controller[controller.index("public FireResult TryFire()"):
                      controller.index("private bool TryShoot()")]
    # Refusals come first, in a fixed order, and the gate itself changes nothing.
    order = [gate.index(token) for token in (
        "FireResult.Reloading", "FireResult.EmptyMagazine", "FireResult.CoolingDown",
        "TryShoot() ? FireResult.Fired : FireResult.NoProjectile")]
    assert order == sorted(order)
    assert "_currentAmmo--" not in gate and "_lastFireTime =" not in gate

    # Only the gate reaches the shot, and only the shot spends ammo or cadence.
    calls = [line for line in controller.splitlines()
             if "TryShoot()" in line and not line.lstrip().startswith("//")
             and "bool TryShoot()" not in line]
    assert len(calls) == 1 and "return TryShoot() ?" in calls[0]
    shot = controller[controller.index("private bool TryShoot()"):]
    shot = shot[:shot.index("\n    }\n")]
    for spend in ("_currentAmmo--;", "_lastFireTime = Time.time;"):
        assert controller.count(spend) == shot.count(spend) >= 1
    # No shot has fired yet, so the first TryFire() is never "cooling down".
    assert "private float _lastFireTime = float.NegativeInfinity;" in controller

    contract = (scripts / firing_provider.SCRIPT).read_text(encoding="utf-8")
    assert contract.startswith("// Auto-generated by Unity Micro Pipeline")
    assert "FireResult TryFire();" in contract
    assert controller.count("{") == controller.count("}")


def test_a_round_that_cannot_launch_spends_nothing(tmp_path, dummy_config):
    _, scripts = _shooter(tmp_path, dummy_config,
                          weapons=[WeaponDef(delivery="projectile_3d")])
    controller = (scripts / "Shooter.cs").read_text(encoding="utf-8")
    branch = controller[controller.index('if (weaponDelivery[_weaponIndex] == "projectile_3d")'):]
    branch = branch[:branch.index("return true;")]
    assert branch.index("if (!LaunchProjectile()) return false;") < branch.index("_currentAmmo--;")


@pytest.mark.parametrize(("variant", "abilities"), [
    ("first_person", ["jump"]),
    ("pointer", ["shoot"]),
    ("multi_mode", ["shoot"]),
])
def test_no_firing_contract_without_a_firing_controller(tmp_path, dummy_config,
                                                        variant, abilities):
    schema, scripts = _shooter(tmp_path, dummy_config, variant=variant, abilities=abilities)
    assert not provides_firing(schema.abilities, variant)
    assert not (scripts / firing_provider.SCRIPT).exists()
    assert "IFiringProvider" not in (scripts / "Shooter.cs").read_text(encoding="utf-8")


def test_both_emitters_render_one_contract(tmp_path, dummy_config):
    _, scripts = _shooter(tmp_path, dummy_config)
    micro = (scripts / firing_provider.SCRIPT).read_text(encoding="utf-8")
    _, files = _generate(_schema(atoms=[_shoot()]))
    project = files[CONTRACT_PATH]
    assert project.startswith("// Auto-generated by Unity Project Pipeline")
    # The two environments differ in whitespace handling; the template has no
    # block tags, so only the banner may differ.
    assert micro.splitlines()[1:] == project.splitlines()[1:]


# ── the Shoot lowerer and its typed binding ───────────────────────────────────

def test_shoot_lowers_to_the_one_resolver_and_names_every_refusal():
    snippet = unity_atom_snippet("Shoot", {})
    assert snippet.startswith(f"var _shot = {firing_provider.RESOLVER_CALL};")
    assert 'Debug.LogError("[atom.Shoot] no IFiringProvider on the Player' in snippet
    assert 'Debug.LogWarning("[atom.Shoot] the firing provider refused the shot: " + _shot)' in snippet
    # It owns no weapon state of its own.
    for owned in ("_currentAmmo", "fire_rate", "magazine", "Raycast"):
        assert owned not in snippet
    assert get_atom("fire").name == "Shoot"
    assert get_atom("Shoot").params == []


def test_shoot_binding_records_its_runtime_provider():
    implemented = bind_atom(0, _shoot(), {firing_provider.CONTRACT: "Shooter"})
    assert implemented.status == "lowered"
    assert implemented.provider == "src/atoms/backends/unity.py:_shoot"
    # Host-independent: it resolves the Player itself, so _GameSequencer may host it.
    assert implemented.host_members == []
    assert implemented.runtime_provider is not None
    assert implemented.runtime_provider.model_dump() == {
        "contract": "IFiringProvider", "host": "Player", "implementer": "Shooter"}

    missing = bind_atom(0, _shoot(), {})
    assert missing.runtime_provider is not None
    assert missing.runtime_provider.implementer is None
    # An atom that resolves no contract records none.
    assert bind_atom(0, AtomInvocation(atom="Wait", args={"seconds": 1})).runtime_provider is None


def test_retired_shoot_args_are_reported_not_honoured():
    binding = bind_atom(0, _shoot(fire_rate=0.5, magazine=3, shoot_range=9.0))
    assert binding.ignored_args == ["fire_rate", "magazine", "shoot_range"]
    assert unity_atom_snippet("Shoot", {"fire_rate": 0.5}) == unity_atom_snippet("Shoot", {})
    # Atoms that declare parameters keep their arguments.
    assert bind_atom(0, AtomInvocation(atom="Wait", args={"seconds": 1})).ignored_args == []

    generator, _ = _generate(_schema(atoms=[_shoot(fire_rate=0.5)]))
    assert any("atoms[0] 'Shoot' declares no parameters, so arg(s) fire_rate change nothing"
               in warning for warning in generator.last_atom_binding_report.warnings)


def test_implementers_come_from_the_survived_controller_abilities():
    assert contract_implementers_for(None) == {}
    assert contract_implementers_for([_controller_unit(["jump"])]) == {}
    assert contract_implementers_for([_controller_unit(["shoot"], "pointer")]) == {}
    assert contract_implementers_for([_controller_unit(["shoot"])]) == {
        firing_provider.CONTRACT: "Shooter"}


# ── generation: with and without an implementer ───────────────────────────────

def test_shoot_without_a_firing_controller_compiles_against_the_contract_and_warns():
    generator, files = _generate(_schema(atoms=[_shoot()]))
    assert CONTRACT_PATH in files and f"{CONTRACT_PATH}.meta" in files
    assert files[CONTRACT_PATH].startswith("// Auto-generated by Unity Project Pipeline")
    sequencer = files["BindProof/Assets/Scripts/GameSequencer.cs"]
    assert firing_provider.RESOLVER_CALL in sequencer
    binding = _shoot_binding(generator)
    assert binding.runtime_provider is not None and binding.runtime_provider.implementer is None
    warnings = generator.last_atom_binding_report.warnings
    assert any("atoms[0] 'Shoot' resolves IFiringProvider on the Player at runtime, but no "
               "composed class implements IFiringProvider" in warning
               and firing_provider.REMEDY in warning for warning in warnings)
    # No empty-host warning: Shoot never acts on _GameSequencer itself.
    assert not any("acts on its own host" in warning for warning in warnings)


def test_shoot_with_a_firing_controller_records_it_and_emits_no_second_contract():
    generator, files = _generate(_schema(atoms=[_shoot()]), [_controller_unit(["shoot"])])
    # The controller micro emits the contract beside its implementer; a second
    # copy in Assets/Scripts would be a duplicate type (CS0101).
    assert CONTRACT_PATH not in files
    binding = _shoot_binding(generator)
    assert binding.runtime_provider is not None
    assert binding.runtime_provider.implementer == "Shooter"
    assert not any("IFiringProvider" in warning
                   for warning in generator.last_atom_binding_report.warnings)


def test_a_build_with_no_shoot_atom_emits_no_contract_copy():
    _, files = _generate(_schema(atoms=[AtomInvocation(atom="Wait", args={"seconds": 1})]))
    assert CONTRACT_PATH not in files


def test_replaying_a_shoot_schema_twice_reproduces_the_record():
    first = _schema(atoms=[_shoot()])
    _generate(first, [_controller_unit(["shoot"])])
    saved = json.loads(first.model_dump_json())
    for _ in range(2):
        replayed = UnitySchema.model_validate(saved)
        generator, _ = _generate(replayed, [_controller_unit(["shoot"])])
        assert any("replayed identically" in note
                   for note in generator.last_atom_binding_report.notes)
        assert replayed.meta[BINDINGS_KEY] == saved["meta"][BINDINGS_KEY]


def test_an_r2_record_without_the_r3_fields_still_replays_identically():
    schema = _schema(atoms=[AtomInvocation(atom="Wait", args={"seconds": 1})])
    record = derive_atom_bindings(schema)
    saved = record.model_dump(mode="json")
    for route in saved["routes"]:
        for binding in route["atoms"]:
            del binding["runtime_provider"], binding["ignored_args"]
    schema.meta = {BINDINGS_KEY: saved}
    report = stamp_atom_bindings(schema, record)
    assert any("replayed identically" in note for note in report.notes)
    assert report.warnings == []


# ── Roslyn against the real Unity reference assemblies ────────────────────────

def _references() -> tuple[Path, ...]:
    from src.unity_authored_check import resolve_references

    if shutil.which("dotnet") is None:
        if os.environ.get("REQUIRE_COMPONENT_API") == "1":
            pytest.fail("required pinned SDK unavailable")
        pytest.skip("pinned SDK unavailable")
    config = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    local = REPO_ROOT / "config.yaml"
    if local.is_file():
        config = yaml.safe_load(local.read_text(encoding="utf-8")) or config
    paths, evidence = resolve_references(config)
    if not paths:
        if os.environ.get("REQUIRE_UNITY_REFERENCES") == "1":
            pytest.fail(evidence["error"])
        pytest.skip(evidence["error"])
    return paths


def _compile(sources: dict[str, str]):
    from src.unity_component_api import extract_sources
    from src.unity_component_check import check_candidate

    references = _references()
    *baseline_paths, candidate = sorted(sources)
    baseline = extract_sources({path: sources[path] for path in baseline_paths})
    return check_candidate(baseline, {candidate: sources[candidate]}, references=references)


def _micro_sources(kind: str, scripts: Path) -> dict[str, str]:
    return {f"Assets/Micros/{kind}/Scripts/{path.name}": path.read_text(encoding="utf-8")
            for path in sorted(scripts.glob("*.cs"))}


@pytest.mark.parametrize("delivery", ["hitscan", "projectile_3d"])
def test_controller_and_shoot_atom_compile_against_real_unity(tmp_path, dummy_config,
                                                              delivery):
    from src.micro.input_actions import InputPipeline, InputSchema

    _, scripts = _shooter(tmp_path / "controller", dummy_config,
                          weapons=[WeaponDef(delivery=delivery)])
    # The controller reads IInputReader, which every composed project gets
    # from the input micro beside it.
    reader = InputPipeline(dummy_config)
    reader_schema = reader.default_fix(InputSchema(name="InputReader"), "keyboard_mouse")
    reader.write_files(reader_schema, "keyboard_mouse", tmp_path / "input")
    sources = {**_micro_sources("controller", scripts),
               **_micro_sources("input", tmp_path / "input" / "Scripts")}
    sources["Assets/Scripts/ShootProbe.cs"] = (
        "using System.Collections;\nusing UnityEngine;\n\n"
        "public class ShootProbe : MonoBehaviour\n{\n"
        "    private IEnumerator Start()\n    {\n"
        + "\n".join("        " + line for line in
                    unity_coroutine_body([{"atom": "Shoot", "args": {}}]).splitlines())
        + "\n        yield return null;\n    }\n}\n")
    check = _compile(sources)
    # The input micro's InputAction members need the Input System package,
    # which the reference profile does not carry unless package_assemblies
    # lists it (R0: such namespaces are unverified, not findings). Everything
    # R3 emits (controller, contract, lowered Shoot) must bind cleanly.
    unverified = [error for error in check.errors
                  if error["path"].startswith("Assets/Micros/input/")
                  and error["code"] in {"CS0246", "CS0234"}]
    findings = [error for error in check.errors if error not in unverified]
    assert findings == [], findings


def test_contract_only_shoot_atom_compiles_against_real_unity():
    _, files = _generate(_schema(atoms=[_shoot()]))
    check = _compile({
        "Assets/Scripts/FiringProvider.cs": files[CONTRACT_PATH],
        "Assets/Scripts/GameSequencer.cs": files["BindProof/Assets/Scripts/GameSequencer.cs"],
    })
    assert check.accepted, check.errors
