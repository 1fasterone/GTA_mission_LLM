"""Tests for src/gta5_compile_check.py — the real ScriptHookVDotNet3.dll
compile gate `unity_cli.py gta-verify`/`e2e-verify` prefer when the Roslyn
toolchain is available.

Roslyn's own diagnostics can't catch a Function.Call(Hash.X, ...) with the
wrong argument count (InputArgument[] is variadic — it compiles clean
regardless of count and only fails at runtime), so compile_check() folds in
its own "ARITY" findings mined from lookups/gta_native_arity.json. The
`_find_arity_findings` tests below run everywhere (pure text scan, no
toolchain). The full `compile_check()` integration tests need the real SDK +
.NET Framework + vendored DLL and are skipped without them.
"""

import os
import tempfile

import pytest

from src.gta5_compile_check import _find_arity_findings, compile_check, gate_available

_TOOLCHAIN_AVAILABLE, _TOOLCHAIN_INFO = gate_available()
_needs_toolchain = pytest.mark.skipif(
    not _TOOLCHAIN_AVAILABLE,
    reason=f"real SHVDN3 compile toolchain unavailable: {_TOOLCHAIN_INFO}")


def _write_cs(tmp_path, body: str) -> str:
    f = tmp_path / "Probe.cs"
    f.write_text(
        "using GTA;\nusing GTA.Native;\n\n"
        "public class ProbeScript : Script\n{\n"
        f"    private void DoThing()\n    {{\n{body}\n    }}\n}}\n",
        encoding="utf-8",
    )
    return str(f)


# ── _find_arity_findings — pure text scan, runs everywhere, real lookups ─────
# Uses the REAL lookups/gta_native_arity.json (via load_native_arity() inside
# the function), not a mock — PLAY_SOUND_FRONTEND mines to a consistent arity
# of 4 across 3,345 real decompiled call sites.

def test_find_arity_findings_catches_wrong_count(tmp_path):
    path = _write_cs(tmp_path, '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X");')
    findings = _find_arity_findings(path)
    assert len(findings) == 1
    f = findings[0]
    assert f["code"] == "ARITY"
    assert f["sev"] == "error"
    assert "PLAY_SOUND_FRONTEND" in f["msg"]
    assert "passes 2 argument(s)" in f["msg"]
    assert "mines to 4" in f["msg"]


def test_find_arity_findings_ignores_correct_count(tmp_path):
    path = _write_cs(
        tmp_path,
        '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);')
    assert _find_arity_findings(path) == []


def test_find_arity_findings_ignores_unknown_native(tmp_path):
    path = _write_cs(tmp_path, "        Function.Call(Hash.SOME_INVENTED_NATIVE, 1, 2);")
    assert _find_arity_findings(path) == []


def test_find_arity_findings_returns_empty_for_missing_file(tmp_path):
    assert _find_arity_findings(str(tmp_path / "does_not_exist.cs")) == []


# ── compile_check() — full Roslyn integration, needs the real toolchain ─────

@_needs_toolchain
def test_compile_check_fails_on_wrong_arity_despite_clean_syntax(tmp_path):
    """The live bug this closes: InputArgument[] is variadic, so csc alone
    reports 0 errors on a truncated Function.Call — this must still fail the
    gate via the synthesized ARITY finding."""
    path = _write_cs(tmp_path, '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X");')
    ok, findings, info = compile_check(path)
    assert ok is False
    codes = [f["code"] for f in findings]
    assert "ARITY" in codes
    arity_finding = next(f for f in findings if f["code"] == "ARITY")
    assert arity_finding["guidance"]   # _enrich() fills this from _CS_GUIDANCE["ARITY"]
    assert arity_finding["fix"] is None


@_needs_toolchain
def test_compile_check_passes_correct_arity_call(tmp_path):
    path = _write_cs(
        tmp_path,
        '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);')
    ok, findings, info = compile_check(path)
    assert ok is True
    assert findings == []
