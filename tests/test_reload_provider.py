"""R4 (S6): the Reload atom and the reload input share one firing-provider gate.

Every schema and controller below is engineer-authored test data, never
Pipeline LLM output. Coverage is per controller variant: each variant whose
controller implements IFiringProvider is rendered, checked structurally and
compiled with Roslyn against the real Unity reference assemblies
(`component_api.unity_editor`); each variant that does not is shown to emit no
reload gate and to leave a Reload atom with no implementer (a build warning,
a contract-only copy, a logged runtime refusal). The Roslyn tests skip when the
pinned SDK or the Editor install is absent; REQUIRE_COMPONENT_API /
REQUIRE_UNITY_REFERENCES turn those skips into failures, as in
tests/test_firing_provider.py.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from src import firing_provider
from src.atom_bindings import BINDINGS_KEY, bind_atom, contract_implementers_for
from src.atoms import get_atom
from src.atoms.backends import unity
from src.atoms.backends.unity import unity_atom_snippet, unity_coroutine_body
from src.atoms.base import AtomInvocation
from src.micro.controller import (
    VARIANTS,
    ControllerPipeline,
    ControllerSchema,
    WeaponDef,
    controller_template,
    provides_firing,
)
from src.unity_compose import ComposedUnit
from src.unity_generator import UnityProjectGenerator, _make_guid
from src.unity_schema import EntitySpec, UnitySchema

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = f"BindProof/Assets/Scripts/{firing_provider.SCRIPT}"
ABILITIES = ["shoot", "reload"]
CONFIG = {
    "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
    "unity_pipeline": {
        "unity_output_dir":  str(REPO_ROOT / "output" / "unity"),
        "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
    },
}


def _survived(variant: str, abilities: list[str] | None = None) -> ControllerSchema:
    pipeline = ControllerPipeline(CONFIG)
    schema = ControllerSchema(name="Shooter", variant=variant, input_backend="legacy",
                              abilities=list(abilities or ABILITIES))
    return pipeline.default_fix(schema, variant)


# Variants whose rendered controller implements the provider, read through the
# same predicate the controller micro and the atom bindings use.
FIRING_VARIANTS = [v for v in VARIANTS if provides_firing(_survived(v).abilities, v)]
NON_FIRING_VARIANTS = [v for v in VARIANTS if v not in FIRING_VARIANTS]


def test_the_variant_split_is_the_reviewed_one():
    """A new or re-pruned variant must be reviewed here before R4 counts it."""
    assert FIRING_VARIANTS == [
        "first_person", "third_person", "top_down",
        "2d_platformer", "2d_topdown", "2d_fighting", "platformer_2_5d",
        "flying", "swimming", "climbing", "aircraft", "fighting",
    ]
    assert NON_FIRING_VARIANTS == [
        "vehicle_car", "vehicle_bike", "vehicle_hover", "pointer", "multi_mode",
    ]
    # Every variant is on exactly one side.
    assert sorted(FIRING_VARIANTS + NON_FIRING_VARIANTS) == sorted(VARIANTS)


def _write(tmp_path: Path, variant: str, name: str = "Shooter",
           input_backend: str = "legacy",
           abilities: list[str] | None = None) -> tuple[ControllerSchema, Path]:
    pipeline = ControllerPipeline(CONFIG)
    schema = ControllerSchema(name=name, variant=variant, input_backend=input_backend,
                              abilities=list(abilities or ABILITIES))
    schema = pipeline.default_fix(schema, variant)
    pipeline.write_files(schema, variant, tmp_path)
    return schema, tmp_path / "Scripts"


def _method(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start:source.index("\n    }\n", start) + len("\n    }\n")]


def _routine_calls(source: str) -> list[str]:
    return [line for line in source.splitlines()
            if "ReloadRoutine()" in line and not line.lstrip().startswith("//")
            and "IEnumerator ReloadRoutine()" not in line]


# ── the controller's one reload gate, per variant ─────────────────────────────

@pytest.mark.parametrize("variant", FIRING_VARIANTS)
def test_reload_input_and_reload_atom_reach_one_gate(tmp_path, variant):
    _, scripts = _write(tmp_path, variant)
    controller = (scripts / "Shooter.cs").read_text(encoding="utf-8")

    assert "IFiringProvider" in controller.splitlines()[
        next(i for i, line in enumerate(controller.splitlines())
             if line.startswith("public class Shooter"))]
    # The reload input asks the gate; it no longer inlines its own checks.
    assert "if (reloadDown) TryReload();" in controller
    assert "if (reloadDown && " not in controller

    gate = _method(controller, "public ReloadResult TryReload()")
    # Refusals come first, in a fixed order, then the one accepted path.
    order = [gate.index(token) for token in (
        "ReloadResult.AlreadyReloading", "ReloadResult.MagazineFull",
        "ReloadResult.NoReserveAmmo", "StartCoroutine(ReloadRoutine());",
        "return ReloadResult.Started;")]
    assert order == sorted(order)
    # The gate moves no rounds itself; the routine it starts does.
    for spend in ("_currentAmmo +=", "_reserveAmmo -="):
        assert spend not in gate
        assert spend in _method(controller, "private IEnumerator ReloadRoutine()")

    # Only the gate starts the routine.
    calls = _routine_calls(controller)
    assert len(calls) == 1 and calls[0] in gate

    contract = (scripts / firing_provider.SCRIPT).read_text(encoding="utf-8")
    assert "ReloadResult TryReload();" in contract
    assert "public static ReloadResult ReloadPlayer()" in contract
    assert controller.count("{") == controller.count("}")


@pytest.mark.parametrize("input_backend", ["legacy", "input_system"])
def test_both_input_backends_reload_through_the_gate(tmp_path, input_backend):
    _, scripts = _write(tmp_path, "third_person", input_backend=input_backend)
    controller = (scripts / "Shooter.cs").read_text(encoding="utf-8")
    assert "bool reloadDown" in controller
    assert controller.count("TryReload();") == 1
    assert "if (reloadDown) TryReload();" in controller


@pytest.mark.parametrize("variant", NON_FIRING_VARIANTS)
def test_variants_without_a_firing_controller_emit_no_reload_gate(tmp_path, variant):
    schema, scripts = _write(tmp_path, variant)
    assert not provides_firing(schema.abilities, variant)
    controller = (scripts / "Shooter.cs").read_text(encoding="utf-8")
    assert "TryReload" not in controller
    assert "IFiringProvider" not in controller
    assert not (scripts / firing_provider.SCRIPT).exists()
    # Why: vehicles prune shoot; pointer and multi_mode render other templates.
    assert ("shoot" not in schema.abilities
            or controller_template(variant) != "Controller.cs.j2")


def test_the_reload_ability_token_adds_nothing_beyond_shoot(tmp_path):
    """Reload belongs to the shoot section, as before R4: same source either way."""
    _, with_token = _write(tmp_path / "a", "first_person", abilities=["shoot", "reload"])
    _, without = _write(tmp_path / "b", "first_person", abilities=["shoot"])
    assert ((with_token / "Shooter.cs").read_text(encoding="utf-8")
            == (without / "Shooter.cs").read_text(encoding="utf-8"))


# ── the Reload lowerer and its typed binding ──────────────────────────────────

def _reload(**args) -> AtomInvocation:
    return AtomInvocation(atom="Reload", args=args)


def test_reload_lowers_to_the_one_resolver_and_names_every_refusal():
    snippet = unity_atom_snippet("Reload", {})
    assert snippet.startswith(f"var _reload = {firing_provider.RELOAD_CALL};")
    assert 'Debug.LogError("[atom.Reload] no IFiringProvider on the Player' in snippet
    assert ('Debug.LogWarning("[atom.Reload] the firing provider refused the reload: " '
            '+ _reload)') in snippet
    # It owns no weapon state and no timing of its own.
    for owned in ("_currentAmmo", "_reserveAmmo", "reload_time", "WaitForSeconds",
                  "StartCoroutine", "ReloadRoutine"):
        assert owned not in snippet
    for alias in ("reload", "refill", "reload_weapon"):
        assert get_atom(alias).name == "Reload"
    definition = get_atom("Reload")
    assert definition.params == []
    assert definition.duration == "instant"
    assert firing_provider.ATOM_CALLS["Reload"] == firing_provider.RELOAD_CALL
    assert firing_provider.ATOM_GATES["Reload"] == "TryReload"


def test_reload_binding_records_its_runtime_provider():
    implemented = bind_atom(0, _reload(), {firing_provider.CONTRACT: "Shooter"})
    assert implemented.status == "lowered"
    assert implemented.provider == "src/atoms/backends/unity.py:_reload"
    # Host-independent: it resolves the Player itself.
    assert implemented.host_members == []
    assert implemented.runtime_provider is not None
    assert implemented.runtime_provider.model_dump() == {
        "contract": "IFiringProvider", "host": "Player", "implementer": "Shooter"}

    missing = bind_atom(0, _reload(), {})
    assert missing.runtime_provider is not None
    assert missing.runtime_provider.implementer is None


def test_retired_reload_time_is_reported_not_honoured():
    binding = bind_atom(0, _reload(reload_time=0.5))
    assert binding.ignored_args == ["reload_time"]
    assert unity_atom_snippet("Reload", {"reload_time": 0.5}) == unity_atom_snippet("Reload", {})

    generator, _ = _generate(_schema(atoms=[_reload(reload_time=0.5)]))
    assert any("atoms[0] 'Reload' declares no parameters, so arg(s) reload_time change nothing"
               in warning for warning in generator.last_atom_binding_report.warnings)


# ── generation, per variant ───────────────────────────────────────────────────

def _schema(**overrides) -> UnitySchema:
    fields = dict(project_name="BindProof", game_type="3D", platform="PC",
                  render_pipeline="URP", scenes=["MainMenu", "Main"])
    fields.update(overrides)
    return UnitySchema(**fields)


def _unit(variant: str, target: str = "player") -> ComposedUnit:
    # ComposedUnit.abilities is the survived list (after default_fix).
    return ComposedUnit(kind="controller", variant=variant, class_name="Shooter",
                        guid=_make_guid("BindProof/Micros/controller/Shooter"),
                        target=target, abilities=_survived(variant).abilities)


def _generate(schema: UnitySchema, units: list[ComposedUnit] | None = None
              ) -> tuple[UnityProjectGenerator, dict[str, str]]:
    generator = UnityProjectGenerator("templates/unity")
    return generator, generator.generate(schema, {}, composed_units=units)


def _binding(generator: UnityProjectGenerator, source: str = "atoms"):
    route = generator.last_atom_bindings.route(source)
    assert route is not None
    return next(binding for binding in route.atoms if binding.atom == "Reload")


@pytest.mark.parametrize("variant", FIRING_VARIANTS)
def test_reload_binds_to_every_firing_variant(variant):
    assert contract_implementers_for([_unit(variant)]) == {firing_provider.CONTRACT: "Shooter"}
    generator, files = _generate(_schema(atoms=[_reload()]), [_unit(variant)])
    binding = _binding(generator)
    assert binding.runtime_provider is not None
    assert binding.runtime_provider.implementer == "Shooter"
    assert firing_provider.RELOAD_CALL in files["BindProof/Assets/Scripts/GameSequencer.cs"]
    # The controller micro emits the contract; a second copy would be CS0101.
    assert CONTRACT_PATH not in files
    assert not any("IFiringProvider" in warning
                   for warning in generator.last_atom_binding_report.warnings)


@pytest.mark.parametrize("variant", NON_FIRING_VARIANTS)
def test_reload_without_a_firing_variant_compiles_against_the_contract_and_warns(variant):
    assert contract_implementers_for([_unit(variant)]) == {}
    generator, files = _generate(_schema(atoms=[_reload()]), [_unit(variant)])
    binding = _binding(generator)
    assert binding.runtime_provider is not None
    assert binding.runtime_provider.implementer is None
    assert CONTRACT_PATH in files and f"{CONTRACT_PATH}.meta" in files
    warnings = generator.last_atom_binding_report.warnings
    assert any("atoms[0] 'Reload' resolves IFiringProvider on the Player at runtime, but no "
               "composed class implements IFiringProvider" in warning
               and firing_provider.REMEDY in warning for warning in warnings)
    assert not any("acts on its own host" in warning for warning in warnings)


def test_a_reloading_controller_off_the_player_is_not_the_implementer():
    off_player = _unit("first_person", target="systems")
    assert contract_implementers_for([off_player]) == {}
    generator, files = _generate(_schema(atoms=[_reload()]), [off_player])
    binding = _binding(generator)
    assert binding.runtime_provider is not None
    assert binding.runtime_provider.implementer is None
    # The off-Player controller's micro already emits FiringProvider.cs.
    assert CONTRACT_PATH not in files


def test_reload_in_an_entity_route_resolves_the_same_provider():
    entity = EntitySpec(name="Armory", kind="prop", count=1, atoms=[_reload()])
    generator, files = _generate(_schema(entities=[entity]), [_unit("first_person")])
    binding = _binding(generator, "entities[Armory].atoms")
    assert binding.runtime_provider is not None
    assert binding.runtime_provider.implementer == "Shooter"
    behavior = files["BindProof/Assets/Scripts/ArmoryBehavior.cs"]
    assert firing_provider.RELOAD_CALL in behavior


def test_replaying_a_reload_schema_twice_reproduces_the_record():
    first = _schema(atoms=[_reload(), AtomInvocation(atom="Shoot")])
    _generate(first, [_unit("first_person")])
    saved = json.loads(first.model_dump_json())
    for _ in range(2):
        replayed = UnitySchema.model_validate(saved)
        generator, _ = _generate(replayed, [_unit("first_person")])
        assert any("replayed identically" in note
                   for note in generator.last_atom_binding_report.notes)
        assert replayed.meta[BINDINGS_KEY] == saved["meta"][BINDINGS_KEY]


# ── the ownership audit route ─────────────────────────────────────────────────

def _row(report: dict, descriptor: str) -> dict:
    return next(row for row in report["representative_connections"]
                if row["descriptor"] == descriptor)


def test_reload_is_owned_by_the_one_firing_provider():
    from src.atoms.ownership_audit import CONNECTED_AND_OWNED, _source_ref, build_report

    reload_row = _row(build_report(), "unity:AtomDef:Reload")
    assert reload_row["classification"] == CONNECTED_AND_OWNED
    assert reload_row["mode"] == "shared_firing_provider"
    assert reload_row["provider"] == _source_ref(unity._SNIPPETS["Reload"])
    assert reload_row["owner_candidates"] == ["ControllerPipeline"]
    assert reload_row["generated_host"] == "Player"
    assert "IFiringProvider.TryReload" in reload_row["lifecycle_seam"]
    assert "reload input" in reload_row["lifecycle_seam"]
    assert reload_row["next_slice"] is None
    assert reload_row["template_owner_preserved"] == "ControllerPipeline"


def test_a_reload_lowerer_that_bypasses_the_provider_loses_its_seam(monkeypatch):
    from src.atoms.ownership_audit import (
        CONNECTED_BUT_NOT_ATTACHED, build_report, validate_report)

    monkeypatch.setitem(unity._SNIPPETS, "Reload",
                        lambda _args: "StartCoroutine(GetComponent<Gun>().Refill());")
    reopened = build_report()
    reload_row = _row(reopened, "unity:AtomDef:Reload")
    assert reload_row["classification"] == CONNECTED_BUT_NOT_ATTACHED
    assert "does not call FiringProviders.ReloadPlayer()" in reload_row["missing_seam"]
    assert any("emitted but unattached" in error for error in validate_report(reopened))
    # Shoot's route is proven separately and stays owned.
    assert _row(reopened, "unity:AtomDef:Shoot")["missing_seam"] is None


def _audit_root(tmp_path: Path, mutate) -> Path:
    """A copy of every file the firing proof reads, with the controller mutated."""
    from src.atoms import ownership_audit

    read = {relative for proof in ownership_audit._FIRING_GATE_PROOFS.values()
            for relative, _ in proof.tokens}
    read |= {relative for relative, _ in ownership_audit._FIRING_SHARED_TOKENS}
    for relative in sorted(read):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        if relative == ownership_audit._CONTROLLER_TEMPLATE:
            text = mutate(text)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def test_an_auto_reload_that_skips_the_gate_loses_the_seam(tmp_path, monkeypatch):
    from src.atoms import ownership_audit

    def auto_reload(text: str) -> str:
        line = "        if (reloadDown) TryReload();\n"
        assert line in text
        return text.replace(line, line + "        if (_currentAmmo == 0) StartCoroutine(ReloadRoutine());\n")

    monkeypatch.setattr(ownership_audit, "ROOT", _audit_root(tmp_path, auto_reload))
    seam, gap, _ = ownership_audit._firing_seam("Reload")
    assert seam is None
    assert gap is not None and "reaches ReloadRoutine() from 2 call sites; only TryReload() may" in gap
    # The shot gate is untouched by that mutation.
    assert ownership_audit._firing_seam("Shoot")[1] is None


def test_a_routine_called_outside_its_gate_loses_the_seam(tmp_path, monkeypatch):
    from src.atoms import ownership_audit

    def moved_call(text: str) -> str:
        # One call site still, but no longer inside TryReload().
        text = text.replace("        StartCoroutine(ReloadRoutine());\n"
                            "        return ReloadResult.Started;\n",
                            "        return ReloadResult.Started;\n")
        return text.replace("        if (reloadDown) TryReload();\n",
                            "        if (reloadDown) TryReload();\n"
                            "        if (reloadDown) StartCoroutine(ReloadRoutine());\n")

    monkeypatch.setattr(ownership_audit, "ROOT", _audit_root(tmp_path, moved_call))
    gap = ownership_audit._firing_seam("Reload")[1]
    assert gap is not None and "from 1 call sites; only TryReload() may" in gap


def test_method_body_reads_one_member_at_class_indentation():
    from src.atoms.ownership_audit import _method_body

    lines = ["    // public ReloadResult TryReload() in a comment",
             "    public ReloadResult TryReload()",
             "    {",
             "        if (x) { y(); }",
             "    }",
             "    private void Other()",
             "    {",
             "    }"]
    assert _method_body(lines, "public ReloadResult TryReload()") == lines[1:5]
    assert _method_body(lines, "public void Missing()") is None
    assert _method_body(lines[:4], "public ReloadResult TryReload()") is None


def test_the_committed_ownership_inventory_carries_the_reload_route():
    inventory = json.loads((REPO_ROOT / "docs/mechanic_ownership_inventory.json")
                           .read_text(encoding="utf-8"))
    reload_row = _row(inventory, "unity:AtomDef:Reload")
    assert reload_row["classification"] == "connected_and_owned"
    baseline = json.loads((REPO_ROOT / "lookups/atoms/support_baseline.json")
                          .read_text(encoding="utf-8"))
    assert "unity:Reload" not in baseline["missing_lowerers"]
    assert "unity:Shoot" not in baseline["missing_lowerers"]


# ── Roslyn against the real Unity reference assemblies, per variant ───────────

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


def _probe(atoms: list[dict]) -> str:
    return ("using System.Collections;\nusing UnityEngine;\n\n"
            "public class WeaponAtomProbe : MonoBehaviour\n{\n"
            "    private IEnumerator Start()\n    {\n"
            + "\n".join("        " + line for line in unity_coroutine_body(atoms).splitlines())
            + "\n        yield return null;\n    }\n}\n")


@pytest.fixture(scope="module")
def input_reader_sources(tmp_path_factory) -> dict[str, str]:
    from src.micro.input_actions import InputPipeline, InputSchema

    out = tmp_path_factory.mktemp("input")
    reader = InputPipeline(CONFIG)
    reader_schema = reader.default_fix(InputSchema(name="InputReader"), "keyboard_mouse")
    reader.write_files(reader_schema, "keyboard_mouse", out)
    return {f"Assets/Micros/input/Scripts/{path.name}": path.read_text(encoding="utf-8")
            for path in sorted((out / "Scripts").glob("*.cs"))}


@pytest.mark.parametrize("variant", FIRING_VARIANTS)
def test_each_firing_variant_and_the_weapon_atoms_compile_against_real_unity(
        tmp_path, variant, input_reader_sources):
    weapons = [WeaponDef(delivery="projectile_3d")] if variant == "first_person" else None
    pipeline = ControllerPipeline(CONFIG)
    schema = ControllerSchema(name="Shooter", variant=variant, input_backend="legacy",
                              abilities=list(ABILITIES), weapons=weapons or [])
    schema = pipeline.default_fix(schema, variant)
    pipeline.write_files(schema, variant, tmp_path)
    sources = {f"Assets/Micros/controller/Scripts/{path.name}": path.read_text(encoding="utf-8")
               for path in sorted((tmp_path / "Scripts").glob("*.cs"))}
    sources.update(input_reader_sources)
    if variant == "climbing":
        # The project generator, not the micro, emits the climbing contract
        # the controller implements (UnityProjectGenerator, climbing_contract).
        sources["Assets/Scripts/IClimbingState.cs"] = UnityProjectGenerator(
            "templates/unity")._render("scripts/IClimbingState.cs.j2", {})
    sources["Assets/Scripts/WeaponAtomProbe.cs"] = _probe([
        {"atom": "Reload", "args": {}}, {"atom": "Shoot", "args": {}}])
    check = _compile(sources)
    # The input micro's InputAction members need the Input System package
    # (unverified per R0, not findings). Everything R4 emits must bind cleanly.
    unverified = [error for error in check.errors
                  if error["path"].startswith("Assets/Micros/input/")
                  and error["code"] in {"CS0246", "CS0234"}]
    findings = [error for error in check.errors if error not in unverified]
    assert findings == [], (variant, findings)


def test_contract_only_reload_atom_compiles_against_real_unity():
    _, files = _generate(_schema(atoms=[_reload()]), [_unit("pointer")])
    check = _compile({
        "Assets/Scripts/FiringProvider.cs": files[CONTRACT_PATH],
        "Assets/Scripts/GameSequencer.cs": files["BindProof/Assets/Scripts/GameSequencer.cs"],
    })
    assert check.accepted, check.errors


def test_report_rows_are_derived_not_copied():
    """The Reload row is built by the same helper as Shoot's, from live source."""
    from src.atoms.ownership_audit import build_report

    report = build_report()
    shoot = copy.deepcopy(_row(report, "unity:AtomDef:Shoot"))
    reload_row = copy.deepcopy(_row(report, "unity:AtomDef:Reload"))
    for row in (shoot, reload_row):
        for key in ("descriptor", "provider", "lifecycle_seam", "diagnostic", "provenance"):
            row.pop(key)
    assert shoot == reload_row


# ── the Editor acceptance covers every firing variant ─────────────────────────

def test_play_mode_acceptance_names_every_firing_variant():
    """The hand-written Play Mode test, the acceptance generator and this
    suite's variant split must name the same shooters, so the advancing Editor
    proof cannot silently skip a variant."""
    import re

    from tools.generate_firing_provider_acceptance import reload_shooter_name, reload_variants

    assert reload_variants() == FIRING_VARIANTS
    shooters = [reload_shooter_name(variant) for variant in FIRING_VARIANTS]
    assert len(set(shooters)) == len(shooters)
    test = (REPO_ROOT / "tests/unity_firing_provider/ReloadProviderPlayModeTests.cs"
            ).read_text(encoding="utf-8")
    block = re.search(r"string\[\] Shooters =\s*\{(.*?)\};", test, re.S)
    assert block is not None
    assert re.findall(r'"(\w+)"', block.group(1)) == shooters
    runner = (REPO_ROOT / "tools/run_firing_provider_acceptance.ps1").read_text(encoding="utf-8")
    assert "ReloadProviderPlayModeTests.cs" in runner
    assert "RELOAD_PROVIDER_VARIANT_PASS shooter=$shooter " in runner
