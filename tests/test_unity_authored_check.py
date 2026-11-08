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
    bad = checker.script(script, {"update_extra": "_score += 1;"})
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
