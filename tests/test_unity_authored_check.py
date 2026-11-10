"""R0: Step 3.5 corrections judged by Roslyn against real Unity references.

Engineer-authored fixtures (labeled test data), never pipeline output. The
integration tests need the pinned .NET SDK and an installed Unity Editor at
the configured `component_api.unity_editor`; they skip otherwise. A job that
promises the SDK sets REQUIRE_COMPONENT_API=1, and one that promises an
Editor install sets REQUIRE_UNITY_REFERENCES=1, so neither passes vacuously.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from src.unity_authored_check import (AuthoredFindings, AuthoredSourceChecker,
                                      introduced_findings, resolve_references)
from src.unity_component_check import CandidateCheck
from src.unity_reference_profile import (ReferenceProfileUnavailable,
                                         unity_reference_profile)


# ── Reference profile resolution (no SDK, no Editor needed) ───────────────────

def _fake_editor(root: Path, *modules: str) -> Path:
    managed = root / "Editor" / "Data" / "Managed" / "UnityEngine"
    managed.mkdir(parents=True)
    for name in modules:
        (managed / name).write_bytes(b"fixture")
    (managed / "UnityEditor.CoreModule.dll").write_bytes(b"editor-only")
    ns = root / "Editor" / "Data" / "NetStandard" / "ref" / "2.1.0"
    ns.mkdir(parents=True)
    (ns / "netstandard.dll").write_bytes(b"fixture")
    return root


def test_no_configured_editor_abstains_with_a_remedy():
    with pytest.raises(ReferenceProfileUnavailable, match="component_api.unity_editor"):
        unity_reference_profile({})


def test_a_path_that_is_not_an_editor_abstains(tmp_path):
    with pytest.raises(ReferenceProfileUnavailable, match="not a Unity Editor"):
        unity_reference_profile({"component_api": {"unity_editor": str(tmp_path)}})


def test_profile_holds_runtime_modules_and_netstandard_only(tmp_path):
    editor = _fake_editor(tmp_path / "6000.1.4f1", "UnityEngine.CoreModule.dll",
                          "UnityEngine.dll")
    profile = unity_reference_profile({"component_api": {"unity_editor": str(editor)}})
    names = [p.name for p in profile.paths]
    assert names == sorted(names, key=str.lower)
    assert set(names) == {"UnityEngine.CoreModule.dll", "UnityEngine.dll", "netstandard.dll"}
    assert profile.editor_version == "6000.1.4f1"
    evidence = profile.describe()
    assert evidence["format"] == "unity-reference-profile/v1"
    assert all("/" not in a["name"] and "\\" not in a["name"] for a in evidence["assemblies"])


def test_package_folder_skips_editor_assemblies(tmp_path):
    editor = _fake_editor(tmp_path / "e", "UnityEngine.CoreModule.dll")
    packages = tmp_path / "ScriptAssemblies"
    packages.mkdir()
    (packages / "Unity.TextMeshPro.dll").write_bytes(b"x")
    (packages / "Unity.TextMeshPro.Editor.dll").write_bytes(b"x")
    (packages / "Assembly-CSharp.dll").write_bytes(b"x")
    profile = unity_reference_profile({"component_api": {
        "unity_editor": str(editor), "package_assemblies": [str(packages)]}})
    names = {p.name for p in profile.paths}
    assert "Unity.TextMeshPro.dll" in names
    assert "Unity.TextMeshPro.Editor.dll" not in names
    assert "Assembly-CSharp.dll" not in names      # the project's own scripts


def test_two_copies_of_one_assembly_abstain(tmp_path):
    editor = _fake_editor(tmp_path / "e", "UnityEngine.CoreModule.dll")
    dup = tmp_path / "pkgs"
    dup.mkdir()
    (dup / "UnityEngine.CoreModule.dll").write_bytes(b"other")
    with pytest.raises(ReferenceProfileUnavailable, match="duplicate reference assembly"):
        unity_reference_profile({"component_api": {
            "unity_editor": str(editor), "package_assemblies": [str(dup)]}})


def test_resolve_references_records_unavailability_instead_of_raising():
    paths, evidence = resolve_references({})
    assert paths == ()
    assert evidence["status"] == "unavailable" and "unity_editor" in evidence["error"]


# ── Introduced-error delta (pure) ─────────────────────────────────────────────

def _check(errors, sources):
    return CandidateCheck({"accepted": not errors, "diagnostics": [
        {"severity": "error", **e} for e in errors]}, sources)


def test_only_errors_the_proposal_adds_are_returned():
    template_error = {"path": "a.cs", "code": "CS0246", "message": "TMPro missing",
                      "callsite": {"line_text": "using TMPro;"}}
    new_error = {"path": "a.cs", "code": "CS0103", "message": "The name 'grid' does not exist",
                 "callsite": {"line_text": "grid.Go();"}}
    baseline = _check([template_error], {"a.cs": "using TMPro;"})
    candidate = _check([template_error, new_error], {"a.cs": "using TMPro;\ngrid.Go();"})
    findings = introduced_findings(baseline, candidate, ["a.cs"])
    assert [d["code"] for d in findings.introduced] == ["CS0103"]
    assert findings.corrections() == [
        "CS0103: The name 'grid' does not exist — `grid.Go();`"]


def test_unresolved_package_namespace_makes_type_errors_unverified_not_corrections():
    using = {"path": "a.cs", "code": "CS0246", "message": "TMPro missing",
             "callsite": {"line_text": "using TMPro;"}}
    tmp_type = {"path": "a.cs", "code": "CS0246", "message": "TMP_Text missing",
                "callsite": {"line_text": "TMP_Text label;"}}
    findings = introduced_findings(_check([using], {"a.cs": "using TMPro;"}),
                                   _check([using, tmp_type], {"a.cs": "using TMPro;\nTMP_Text label;"}),
                                   ["a.cs"])
    assert findings.introduced == []
    assert [d["message"] for d in findings.unverified] == ["TMP_Text missing"]


def test_errors_outside_the_candidate_paths_are_not_attributed():
    other = {"path": "b.cs", "code": "CS0103", "message": "x"}
    findings = introduced_findings(_check([], {}), _check([other], {}), ["a.cs"])
    assert findings.introduced == [] and findings.unverified == []


def test_corrections_are_capped():
    findings = AuthoredFindings(introduced=[{"code": "CS0103", "message": str(i)} for i in range(20)])
    assert len(findings.corrections()) == 6


# ── Real Roslyn + real Unity references ───────────────────────────────────────

def _references():
    if shutil.which("dotnet") is None:
        if os.environ.get("REQUIRE_COMPONENT_API") == "1":
            pytest.fail("required pinned SDK unavailable")
        pytest.skip("pinned SDK unavailable")
    config = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    local = Path("config.yaml")
    if local.is_file():
        config = yaml.safe_load(local.read_text(encoding="utf-8")) or config
    paths, evidence = resolve_references(config)
    if not paths:
        # Separate from REQUIRE_COMPONENT_API: CI runners carry the pinned SDK
        # but no Unity Editor install.
        if os.environ.get("REQUIRE_UNITY_REFERENCES") == "1":
            pytest.fail(evidence["error"])
        pytest.skip(evidence["error"])
    return paths


def _composed():
    from src.unity_component_api import extract_components
    from src.unity_compose import MicroComposer, Subsystem
    subs = [Subsystem(kind="health", variant="simple", target="player", name="PlayerHealth")]
    composer = MicroComposer("AuthoredCheck")
    return composer, subs, extract_components(composer, subs)


def test_real_compiler_reports_an_undeclared_name_and_a_bad_overload():
    refs = _references()
    composer, subs, api = _composed()
    checker = AuthoredSourceChecker(composer=composer, subsystems=subs,
                                    component_api=api, references=refs)
    findings = checker.subsystem(subs[0], {"extra_methods": [{
        "signature": "public void Probe()",
        "body": "grid.Go();\nvar rb = GetComponent<Rigidbody>(1, 2, 3);"}]})
    codes = {d["code"] for d in findings.introduced}
    assert {"CS0103", "CS1501"} <= codes
    # Judged, not applied.
    assert "extra_methods" not in subs[0].options


def test_real_compiler_accepts_inherited_unity_members_without_any_table():
    refs = _references()
    composer, subs, api = _composed()
    checker = AuthoredSourceChecker(composer=composer, subsystems=subs,
                                    component_api=api, references=refs)
    findings = checker.subsystem(subs[0], {"extra_methods": [{
        "signature": "public void Probe()",
        "body": ("transform.position += Vector3.up * Time.deltaTime;\n"
                 "if (TryGetComponent<Rigidbody>(out var rb)) rb.AddForce(Vector3.up);\n"
                 "StartCoroutine(null as System.Collections.IEnumerator);")}]})
    assert findings.introduced == [], findings.introduced


def test_real_compiler_judges_a_custom_script_in_project_context():
    from src.unity_schema import UnityScript, UnitySchema
    refs = _references()
    composer, subs, api = _composed()
    script = UnityScript(name="ScoreKeeper", type="custom", description="Counts score.")
    schema = UnitySchema(project_name="AuthoredCheck", company_name="T", game_type="3D",
                         platform="PC", render_pipeline="Built-in", unity_version="6000.1",
                         color_space="Linear", description="d", features=[], scenes=["Main"],
                         scripts=[script], packages=[])
    checker = AuthoredSourceChecker(composer=composer, subsystems=subs, component_api=api,
                                    references=refs, schema=schema)
    # A genuinely undeclared call. (`_score += 1;` would NOT do: Step 6's
    # _auto_declare_undeclared_scalars declares `_score`, so it ships clean.)
    bad = checker.script(script, {"update_extra": "Launch(3);"})
    assert [d["code"] for d in bad.introduced] == ["CS0103"]
    good = checker.script(script, {
        "extra_fields": [{"name": "_score", "type": "int", "default": "0"}],
        "update_extra": "_score += 1;\nvar hp = GameContext.Instance.Get<PlayerHealth>();"})
    assert good.introduced == [], good.introduced
    assert script.update_extra == ""


def test_final_record_reports_script_defects_with_source_preserved():
    """The erasers' replacement: a custom script's invented collaborator is a
    recorded diagnostic and the authored line still ships."""
    from src.unity_authored_check import final_component_record
    from src.unity_compose import MicroComposer, Subsystem
    from src.unity_schema import UnityScript, UnitySchema
    _references()
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) \
        if Path("config.yaml").is_file() else yaml.safe_load(
            Path("config.example.yaml").read_text(encoding="utf-8"))
    body = "var p = FindFirstObjectByType<AntiqueMirror>();"
    script = UnityScript(name="MirrorSpawning", type="custom", description="d",
                         update_extra=body)
    schema = UnitySchema(project_name="AuthoredCheck", company_name="T", game_type="3D",
                         platform="PC", render_pipeline="Built-in", unity_version="6000.1",
                         color_space="Linear", description="d", features=[], scenes=["Main"],
                         scripts=[script], packages=[])
    subs = [Subsystem(kind="health", variant="simple", target="player", name="PlayerHealth")]
    record, warnings = final_component_record(MicroComposer("AuthoredCheck"), subs,
                                              config, schema)
    check = record["script_source_check"]
    assert check["accepted"] is False
    assert {d["code"] for d in check["diagnostics"]} == {"CS0246"}
    assert record["reference_profile"]["status"] == "resolved"
    assert any("custom script source" in w for w in warnings)
    assert script.update_extra == body


# ── Step 6 parity: the check compiles exactly what generate() writes ──────────
# Review of #292: the checker used to rebuild Step 6's script rendering by
# hand and diverged five ways (lint only, a generator per script, a short
# known-type set, payload lists replacing Step 3's, and no non-custom scripts
# or GameContext companions). It now calls the generator's own helpers.

def _parity_project():
    from src.unity_compose import MicroComposer, drop_owned_scripts, subsystems_for
    from src.unity_pipeline import inject_boilerplate_scripts
    from src.unity_schema import CodeField, CodeMethod, UnitySchema, UnityScript
    scripts = [
        # Two scripts naming one invented type: one stub project-wide.
        UnityScript(name="Backpack", type="custom", description="d",
                    extra_fields=[CodeField(name="contract", type="BountyContract"),
                                  # Declared by the audio micro's template.
                                  CodeField(name="cue", type="AudioCue")]),
        UnityScript(name="Shop", type="custom", description="d",
                    extra_fields=[CodeField(name="offer", type="BountyContract")],
                    update_extra="_score += 1;"),
        # Step 6 retypes this field to Coroutine.
        UnityScript(name="Spawner", type="custom", description="d",
                    extra_usings=["System.Collections"],
                    extra_fields=[CodeField(name="spawnRoutine", type="IEnumerator")],
                    awake_extra="spawnRoutine = StartCoroutine(SpawnLoop());",
                    extra_methods=[CodeMethod(signature="private IEnumerator SpawnLoop()",
                                              body="yield return null;")]),
    ]
    schema = UnitySchema(project_name="ShipParity", game_type="3D", platform="PC",
                         render_pipeline="builtin", scenes=["Main"],
                         features=["inventory", "audio"], scripts=scripts)
    subs = subsystems_for("3D", ["inventory", "audio"])
    composer = MicroComposer("ShipParity")
    units = composer.plan(subs)
    # What Step 5 does before Step 6 sees the schema.
    inject_boilerplate_scripts(schema)
    schema.scripts = drop_owned_scripts(schema.scripts, units)
    return schema, subs, composer, units


def test_checker_renders_assets_scripts_byte_identical_to_step6():
    from src.unity_generator import UnityProjectGenerator
    schema, subs, composer, units = _parity_project()
    before = schema.model_dump()
    # Rendering needs no compiler: component_api is only read by check_candidate.
    checker = AuthoredSourceChecker(composer=composer, subsystems=subs,
                                    component_api=None, references=(), schema=schema)
    mine = checker._project_sources(schema)
    assert schema.model_dump() == before          # judged on a copy, never mutated

    shipped = UnityProjectGenerator().generate(schema.model_copy(deep=True), {},
                                               composed_units=units)
    prefix = "ShipParity/Assets/Scripts/"
    theirs = {path[len(prefix):]: source for path, source in shipped.items()
              if path.startswith(prefix) and path.endswith(".cs")}
    rendered = {path[len("scripts/"):]: source for path, source in mine.items()
                if path.startswith("scripts/")}
    assert set(rendered) == {f"{s.name}.cs" for s in schema.scripts}
    for name, source in rendered.items():
        assert theirs[name] == source, name

    assert {"context/GameContext.cs", "context/StatModifier.cs"} <= set(mine)
    assert "GameManager.cs" in rendered               # Step 5 boilerplate
    stubs = "".join(rendered.values())
    assert stubs.count("public class BountyContract") == 1
    assert "public class AudioCue" not in stubs
    assert "Coroutine spawnRoutine" in rendered["Spawner.cs"]


def test_step3_5_check_sees_step5_boilerplate_before_step5_runs():
    """At Step 3.5 the schema has no GameManager yet; Step 5 always adds one,
    so the check must compile against it. Idempotent once Step 5 has run."""
    from src.unity_compose import MicroComposer, Subsystem
    from src.unity_pipeline import inject_boilerplate_scripts
    from src.unity_schema import UnitySchema, UnityScript
    schema = UnitySchema(project_name="Early", game_type="3D", platform="PC",
                         render_pipeline="builtin", scenes=["Main"],
                         scripts=[UnityScript(name="Keeper", type="custom", description="d")])
    subs = [Subsystem(kind="health", variant="simple", target="player", name="PlayerHealth")]
    checker = AuthoredSourceChecker(composer=MicroComposer("Early"), subsystems=subs,
                                    component_api=None, references=(), schema=schema)
    assert "scripts/GameManager.cs" in checker._project_sources(schema)
    assert [s.type for s in schema.scripts] == ["custom"]
    first = inject_boilerplate_scripts(schema)
    assert first and inject_boilerplate_scripts(schema) == []


def test_script_templates_read_only_the_shared_render_context():
    """Drift guard: Step 6 renders Assets/Scripts with script_render_context
    alone, so a template reading any other key fails in both paths."""
    from jinja2 import Environment, FileSystemLoader, meta
    from src.unity_generator import _LEGACY_SCRIPT_TEMPLATES, script_render_context
    from src.unity_schema import UnitySchema
    env = Environment(loader=FileSystemLoader("templates/unity"))
    per_script = {"script", "atom_body", "stub_classes", "needs_rigidbody",
                  "needs_player_rigidbody"}
    allowed = set(script_render_context(UnitySchema(
        project_name="Ctx", game_type="3D", platform="PC",
        render_pipeline="builtin", scenes=["Main"]), None)) | per_script
    templates = [f"scripts/{name}" for name in _LEGACY_SCRIPT_TEMPLATES.values()]
    for template in [*templates, "scripts/IClimbingState.cs.j2"]:
        source = env.loader.get_source(env, template)[0]
        assert meta.find_undeclared_variables(env.parse(source)) <= allowed, template


def _config():
    local = Path("config.yaml")
    return yaml.safe_load((local if local.is_file() else Path("config.example.yaml"))
                          .read_text(encoding="utf-8"))


def _ship_subsystems():
    from src.unity_compose import Subsystem
    return [Subsystem(kind="health", variant="simple", target="player", name="PlayerHealth"),
            Subsystem(kind="inventory", variant="slot_based", target="player",
                      name="PlayerInventory"),
            Subsystem(kind="audio", variant="categorical_mixer", target="systems",
                      name="AudioDirector")]


def _ship_schema(script_list):
    from src.unity_schema import UnitySchema
    return UnitySchema(project_name="ShipCheck", game_type="3D", platform="PC",
                       render_pipeline="builtin", scenes=["Main"], scripts=list(script_list))


def _checker_for(script_list, refs):
    from src.unity_component_api import extract_components
    from src.unity_compose import MicroComposer
    subs, schema = _ship_subsystems(), _ship_schema(script_list)
    composer = MicroComposer("ShipCheck")
    return AuthoredSourceChecker(composer=composer, subsystems=subs,
                                 component_api=extract_components(composer, subs),
                                 references=refs, schema=schema)


def test_real_compiler_binds_game_manager_before_step5_injects_it():
    from src.unity_schema import UnityScript
    refs = _references()
    script = UnityScript(name="ScoreKeeper", type="custom", description="d")
    checker = _checker_for([script], refs)
    findings = checker.script(script, {
        "update_extra": "if (GameManager.Instance != null) GameManager.Instance.AddScore(1);"})
    assert findings.introduced == [], findings.introduced


def test_real_compiler_judges_a_proposal_on_top_of_step3_fields():
    """Payload lists APPEND, as _apply_script lands them; they never replace."""
    from src.unity_schema import CodeField, UnityScript
    refs = _references()
    script = UnityScript(name="Diver", type="custom", description="d",
                         extra_fields=[CodeField(name="oxygen", type="float", default="100f")])
    checker = _checker_for([script], refs)
    findings = checker.script(script, {
        "update_extra": "oxygen -= drainRate * Time.deltaTime;",
        "extra_fields": [{"name": "drainRate", "type": "float", "default": "1f"}]})
    assert findings.introduced == [], findings.introduced
    assert [f.name for f in script.extra_fields] == ["oxygen"]     # judged, not applied
    assert script.update_extra == ""


def test_real_compiler_judges_the_step6_normalized_source():
    from src.unity_schema import UnityScript
    refs = _references()
    script = UnityScript(name="Spawner", type="custom", description="d")
    checker = _checker_for([script], refs)
    findings = checker.script(script, {
        "extra_usings": ["System.Collections"],
        "extra_fields": [{"name": "spawnRoutine", "type": "IEnumerator"}],
        "awake_extra": "spawnRoutine = StartCoroutine(SpawnLoop());",
        "extra_methods": [{"signature": "private IEnumerator SpawnLoop()",
                           "body": "yield return null;"}]})
    assert findings.introduced == [], findings.introduced


def test_real_compiler_knows_types_micro_templates_declare():
    from src.unity_schema import UnityScript
    refs = _references()
    script = UnityScript(name="Jukebox", type="custom", description="d")
    checker = _checker_for([script], refs)
    findings = checker.script(script, {
        "extra_fields": [{"name": "cue", "type": "AudioCue"}],
        "update_extra": "if (cue != null) Debug.Log(cue.name);"})
    assert findings.introduced == [], findings.introduced


def test_final_record_accepts_a_project_that_compiles():
    """GameContext's GameManager/StatModifier references and two scripts
    sharing an invented type all compile in the shipped project, so the
    final record must accept it."""
    from src.unity_authored_check import final_component_record
    from src.unity_compose import MicroComposer
    from src.unity_schema import CodeField, UnityScript
    _references()
    backpack = UnityScript(name="Backpack", type="custom", description="d",
                           extra_fields=[CodeField(name="contract", type="BountyContract")])
    shop = UnityScript(name="Shop", type="custom", description="d",
                       extra_fields=[CodeField(name="offer", type="BountyContract")],
                       update_extra="if (GameManager.Instance != null) "
                                    "GameManager.Instance.AddScore(1);")
    record, warnings = final_component_record(MicroComposer("ShipCheck"),
                                              _ship_subsystems(), _config(),
                                              _ship_schema([backpack, shop]))
    check = record["script_source_check"]
    assert check["accepted"] is True, check["diagnostics"]
    assert not any("custom script source" in w for w in warnings)
