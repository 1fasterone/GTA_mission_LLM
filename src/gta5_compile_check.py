"""gta5_compile_check.py - real-compiler ground truth for the GTA5 compile gate.

`unity_cli.py gta-verify` checks generated missions against stubs/shvdn/GtaStubs.cs
(hand-written no-op stand-ins for the SHVDN3 API) via `dotnet build`. A stub can
only catch a mismatch for a member its author thought to include - anything
missing from the stub either falsely fails valid code or falsely passes
invented code the stub happens to accept. This module compiles the mission
against the REAL, vendored ScriptHookVDotNet3.dll (stubs/shvdn/real_dll/)
instead: 100% fidelity, no stub-completeness gap. gta-verify prefers this path
when the toolchain is available and falls back to the stub project otherwise
(e.g. non-Windows CI, where the .NET Framework reference assemblies this needs
don't exist).

Ported from the SCRIPTHVDN3 project's compile_check.py, which builds against
Roslyn (the SDK's csc.dll) + real .NET Framework 4.8 assemblies + the vendored
DLL, then translates each CSxxxx diagnostic into plain guidance the same way
the regex-based gta_allowlists.py checks do for their own findings.

Roslyn's diagnostics alone can't catch a Function.Call(Hash.X, ...) with the
wrong argument count -- InputArgument[] is variadic, so it compiles clean
regardless of count and only fails at runtime. compile_check() synthesizes
its own "ARITY" findings for that class of bug by scanning the source against
lookups/gta_native_arity.json (gta_allowlists.load_native_arity /
micro.code_fields.find_function_call_arity_mismatches -- the same ground
truth and detector the LLM-extras lint pass in pipeline.py uses), and folds
them into the same error list so a wrong-arity call fails this gate too.

Auto-detects toolchain; override with env vars:
    SHVDN3_DLL   - path to ScriptHookVDotNet3.dll
    ROSLYN_CSC   - path to csc.dll
    NETFX_DIR    - .NET Framework 4.x assembly dir
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDORED_DIR = os.path.join(_HERE, "..", "stubs", "shvdn", "real_dll")


# ── Toolchain discovery ───────────────────────────────────────────────────────

def _find_csc() -> "str | None":
    if os.environ.get("ROSLYN_CSC") and os.path.isfile(os.environ["ROSLYN_CSC"]):
        return os.environ["ROSLYN_CSC"]
    pats = [
        r"C:\Program Files\dotnet\sdk\*\Roslyn\bincore\csc.dll",
        r"C:\Program Files (x86)\dotnet\sdk\*\Roslyn\bincore\csc.dll",
    ]
    found: list[str] = []
    for p in pats:
        found += glob.glob(p)
    return sorted(found)[-1] if found else None  # newest SDK last after sort


def _find_netfx() -> "str | None":
    if os.environ.get("NETFX_DIR") and os.path.isdir(os.environ["NETFX_DIR"]):
        return os.environ["NETFX_DIR"]
    for d in (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319",
              r"C:\Windows\Microsoft.NET\Framework\v4.0.30319"):
        if os.path.isdir(d):
            return d
    return None


def _find_dll() -> "str | None":
    if os.environ.get("SHVDN3_DLL") and os.path.isfile(os.environ["SHVDN3_DLL"]):
        return os.environ["SHVDN3_DLL"]
    cands = [os.path.join(_VENDORED_DIR, "ScriptHookVDotNet3.dll")]
    cands += sorted(glob.glob(
        os.path.expanduser(r"~/.nuget/packages/scripthookvdotnet3/*/lib/net48/ScriptHookVDotNet3.dll")))
    for c in cands:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _find_dotnet() -> str:
    for d in (r"C:\Program Files\dotnet\dotnet.exe", "dotnet"):
        return d  # dotnet on PATH is the normal case; muxer runs csc.dll


_NETFX_REFS = ["mscorlib", "System", "System.Core", "System.Windows.Forms",
               "System.Drawing", "System.Xml", "System.Data"]


def gate_available() -> tuple[bool, dict]:
    """Return (bool, info_dict). False means fall back to the stub project."""
    csc, fx, dll = _find_csc(), _find_netfx(), _find_dll()
    info = {"csc": csc, "netfx": fx, "dll": dll}
    return bool(csc and fx and dll), info


# ── Diagnostic knowledge base ─────────────────────────────────────────────────
# Maps a compiler error code to human guidance. The per-symbol detail (which
# native/member) comes from the message itself; hash_suggestions.json supplies
# the specific "use X instead" for invalid Hash names.

_CS_GUIDANCE = {
    "CS0117": "Type doesn't contain that member. For Hash.* this is an invented "
              "native name; check the suggestion or lookups/gta_hash_allowlist.json.",
    "CS1061": "No such instance member on that type - it's invented or on a "
              "different type. Check lookups/gta_api_surface.json for the real member.",
    "CS0246": "Type/namespace not found - almost always a missing 'using' "
              "directive (GTA, GTA.Native, GTA.Math, System.Windows.Forms).",
    "CS0103": "Name doesn't exist in the current context - undeclared variable, "
              "missing using, or a static class referenced as a bare name.",
    "CS0266": "Missing explicit cast (usually double->float). Wrap the RHS: "
              "(float)(expr).",
    "CS1503": "Argument type mismatch - e.g. passing float x,y,z where a Vector3 "
              "is expected, or Keys where a GTA.Control is expected.",
    "CS1501": "No overload takes that argument count - e.g. v1.Distance(v2) "
              "instead of the static Vector3.Distance(v1, v2).",
    "CS1729": "Type has no constructor with those args - e.g. new Vehicle(hash); "
              "spawn via World.CreateVehicle(new Model(...), pos, heading).",
    "CS1002": "Syntax error (missing ';'). Often a markdown fence or smart-quote "
              "artifact leaked into the code.",
    "CS1056": "Unexpected character - backtick or smart/curly quote from a "
              "markdown code fence. Strip it.",
    "CS0019": "Operator can't be applied to these operands - e.g. comparing a "
              "struct (Vector3/Model) to null.",
    "CS0029": "Cannot implicitly convert - add a cast or use the right type.",
    "CS0012": "A referenced type lives in an assembly this gate didn't reference "
              "(tooling gap, not a mission bug). Add the assembly to _NETFX_REFS.",
    "ARITY": "Wrong argument count for this native. InputArgument[] is variadic, "
             "so csc accepts any count and can't catch this on its own -- verified "
             "instead against lookups/gta_native_arity.json, mined from real "
             "decompiled Rockstar mission scripts.",
}

_CS0117_HASH_RE = re.compile(r"'Hash' does not contain a definition for '([A-Z0-9_]+)'")


def _load_hash_suggestions() -> dict:
    p = os.path.join(_VENDORED_DIR, "hash_suggestions.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return {k: v for k, v in json.load(f).items() if v}
        except Exception:
            pass
    return {}


# Roslyn diagnostic line: path(line,col): error CSxxxx: message
_DIAG_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
    r"(?P<sev>error|warning)\s+(?P<code>CS\d+):\s+(?P<msg>.*)$")


def _parse_diagnostics(text: str) -> list[dict]:
    out = []
    for ln in text.splitlines():
        m = _DIAG_RE.match(ln.strip())
        if not m:
            continue
        out.append({
            "line": int(m.group("line")),
            "col": int(m.group("col")),
            "sev": m.group("sev"),
            "code": m.group("code"),
            "msg": m.group("msg").strip(),
        })
    return out


def _enrich(diag: dict, hash_sug: dict) -> dict:
    code = diag["code"]
    diag["guidance"] = _CS_GUIDANCE.get(code, "")
    diag["fix"] = None
    if code == "CS0117":
        m = _CS0117_HASH_RE.search(diag["msg"])
        if m and m.group(1) in hash_sug:
            diag["fix"] = "Hash.{} -> {}".format(m.group(1), hash_sug[m.group(1)])
    return diag


# ── Argument-count ground truth (Roslyn can't see this class of bug) ─────────

def _find_arity_findings(filepath: str) -> list[dict]:
    """Synthesize diagnostic-shaped findings for Function.Call(Hash.X, ...)
    argument-count mismatches — the same detector and ground truth
    (lookups/gta_native_arity.json) the LLM-extras lint pass in pipeline.py
    runs, applied here to the whole compiled file rather than just the
    LLM-authored fragment. Returns [] if the arity table isn't available or
    the file can't be read; never raises."""
    from .gta_allowlists import load_native_arity
    from .micro.code_fields import find_function_call_arity_mismatches

    arity = load_native_arity()
    if not arity:
        return []
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return []

    out = []
    for line_no, native, expected, actual in find_function_call_arity_mismatches(source, arity=arity):
        out.append({
            "line": line_no + 1,
            "col": 1,
            "sev": "error",
            "code": "ARITY",
            "msg": f"Function.Call(Hash.{native}, ...) passes {actual} argument(s); "
                   f"every real decompiled call site mines to {expected}",
        })
    return out


# ── Compile ───────────────────────────────────────────────────────────────────

def compile_check(filepath: str, include_warnings: bool = False):
    """Compile `filepath` against the real ScriptHookVDotNet3.dll.

    Returns (ok, findings, info):
      ok        - True if no compiler errors (warnings don't fail); None if
                  the toolchain isn't available on this machine.
      findings  - list of enriched diagnostic dicts (errors, + warnings if asked).
      info      - toolchain paths, or with an 'unavailable' key naming what's missing.
    """
    available, info = gate_available()
    if not available:
        missing = [k for k in ("csc", "netfx", "dll") if not info.get(k)]
        info["unavailable"] = "missing toolchain: " + ", ".join(missing)
        return None, [], info

    csc, fx, dll = info["csc"], info["netfx"], info["dll"]
    refs = [os.path.join(fx, r + ".dll") for r in _NETFX_REFS]
    refs = [r for r in refs if os.path.isfile(r)]
    refs.append(dll)

    tmpdll = os.path.join(tempfile.gettempdir(), "gta5_compile_probe_%d.dll" % os.getpid())
    # langversion:6 matches the SHVDN3 3.9 toolchain (C# 6 = string
    # interpolation, null-conditional, etc.) - the actual game runtime's ceiling.
    cmd = [_find_dotnet(), csc, "-nologo", "-nostdlib", "-target:library",
           "-langversion:6", "-out:" + tmpdll]
    cmd += ["-reference:" + r for r in refs]
    cmd.append(filepath)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as ex:
        info["unavailable"] = "compiler invocation failed: %s" % ex
        return None, [], info
    finally:
        try:
            if os.path.isfile(tmpdll):
                os.remove(tmpdll)
        except OSError:
            pass

    diags = _parse_diagnostics(proc.stdout + "\n" + proc.stderr)
    diags += _find_arity_findings(filepath)
    hash_sug = _load_hash_suggestions()
    errors = [d for d in diags if d["sev"] == "error"]
    warns = [d for d in diags if d["sev"] == "warning"]
    picked = errors + (warns if include_warnings else [])
    findings = [_enrich(d, hash_sug) for d in picked]
    return len(errors) == 0, findings, info


def format_findings(findings: list[dict]) -> str:
    lines = []
    for d in findings:
        lines.append("  {sev:>7} [{code}] @ line {line}: {msg}".format(**d))
        if d.get("fix"):
            lines.append("          FIX: " + d["fix"])
        elif d.get("guidance"):
            lines.append("          " + d["guidance"])
    return "\n".join(lines)
