"""CI gates reject missing evidence and generate from a captured schema."""
from __future__ import annotations

import hashlib

import pytest

from tools.ci_prepare_unity_fixture import DEFAULT_SCHEMA, SMOKE_NAME, prepare_fixture
from tools.ci_verify_unity_results import verify_results


def test_fixture_is_generated_without_changing_the_captured_schema(tmp_path):
    before = hashlib.sha256(DEFAULT_SCHEMA.read_bytes()).hexdigest()
    output = tmp_path / "fresh"
    project = prepare_fixture(output)
    assert (project / "ProjectSettings/ProjectVersion.txt").is_file()
    assert (project / "Packages/manifest.json").is_file()
    assert list((project / "Assets").rglob("*.cs"))
    assert SMOKE_NAME in (project / "Assets/Tests/Editor/GeneratedCompileSmoke.cs").read_text()
    assert hashlib.sha256(DEFAULT_SCHEMA.read_bytes()).hexdigest() == before
    with pytest.raises(FileExistsError):
        prepare_fixture(output)


def test_verify_requires_a_real_passed_smoke_test(tmp_path):
    with pytest.raises(RuntimeError, match="no passing"):
        verify_results(tmp_path)
    result = tmp_path / "results.xml"
    result.write_text(f'<test-run><test-case fullname="Compile.{SMOKE_NAME}" '
                      'result="Skipped" /></test-run>', encoding="utf-8")
    with pytest.raises(RuntimeError, match="did not pass"):
        verify_results(tmp_path)
    result.write_text(f'<test-run><test-case fullname="Compile.{SMOKE_NAME}" '
                      'result="Passed" /></test-run>', encoding="utf-8")
    assert verify_results(tmp_path) == result


def test_semantic_probe_refuses_missing_sdk(monkeypatch):
    from tools import ci_semantic_probe
    monkeypatch.setattr(ci_semantic_probe.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="SDK is required"):
        ci_semantic_probe.run()
