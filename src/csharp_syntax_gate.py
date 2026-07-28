"""
Syntax gate for generated C# — a real parser, run before a project ships.

Why a parser and not a counter
------------------------------
The two live defects that motivated this gate defeat character counting:

  [Tooltip("Current active quest identifier (e.g., "RocBiomeFreefall", ...")]
      → CS1003. Quotes are balanced; the literal just ends in the wrong place.

  if (_isDirty) { int loc = _idx; // Validate state before save }
      → the closing brace sits inside a `//` comment, so it never closes
        Update(). Brace CHARACTERS are balanced, so a counter sees nothing.
        Every following member parses as a local function and the first
        `public` one raises CS0106.

Catching either requires tokenizing, and once you are tokenizing a real parser
is both cheaper and complete. This module shells out to
tools/csharp_syntax_gate (Roslyn `CSharpSyntaxTree.ParseText`, syntax only).

Syntax-only, deliberately
-------------------------
`csc`/`dotnet build` against a stub project also BINDS, so every generated file
returns CS0246 for `UnityEngine` and the real errors drown. Parse diagnostics
alone are the right severity band: verified 0 findings across 138 known-good
generated scripts, while flagging both defects above.

What this gate does NOT catch
-----------------------------
Anything that only binding can decide: undefined types (`SaveSystemData`),
undefined methods (`SetDialogueBranch`), and — measured, not assumed —
language-version violations. Roslyn parses a C# 12 collection expression
(`= [12000f, 4500f, 300f]`) and a C# 10 file-scoped namespace without
complaint under `LanguageVersion.CSharp9`; the "feature is not available"
error is raised during binding. Those remain Unity-console findings.
See tests/test_csharp_syntax_gate.py for the locked-in boundary.

Toolchain policy
----------------
A parse failure RAISES (`CSharpSyntaxError`) — same doctrine as
`[assets.strict]`: report file, line, column and the compiler's own message,
never silently repair. A MISSING toolchain is a different thing: the user never
installed the .NET SDK, which is not a defect in the generated project, so the
gate logs a loud warning and reports `skipped`. Set `unity_pipeline.syntax_gate`
to false in config.yaml to disable it entirely.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

console = Console()

_TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "csharp_syntax_gate"

# `path(line,col): CSxxxx: message` — the tool's own output format.
_DIAG_RE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+"
                      r"(?P<code>CS\d+):\s+(?P<message>.*)$")


@dataclass(frozen=True)
class SyntaxDiagnostic:
    file: str
    line: int
    column: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}({self.line},{self.column}): {self.code}: {self.message}"


@dataclass
class GateResult:
    checked: int = 0
    diagnostics: list[SyntaxDiagnostic] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    @property
    def skipped(self) -> bool:
        return bool(self.skipped_reason)


class CSharpSyntaxError(RuntimeError):
    """Generated C# failed to parse. Carries every diagnostic."""

    def __init__(self, diagnostics: list[SyntaxDiagnostic]):
        self.diagnostics = diagnostics
        detail = "\n".join(f"  {d}" for d in diagnostics)
        super().__init__(
            f"[syntax] {len(diagnostics)} C# syntax error(s) in generated "
            f"scripts:\n{detail}\n"
            "These files will not compile in Unity. This is a generator defect, "
            "not a user error — fix the injection path rather than editing the "
            "output. Set unity_pipeline.syntax_gate: false to bypass."
        )


def _find_sdk() -> tuple[Path, str] | None:
    """Highest installed .NET SDK → (its Roslyn bincore dir, target framework).

    The Roslyn assemblies shipped with SDK N need runtime N, so the TFM is
    pinned to the same major version the references come from (a mismatch is
    CS1705 at build time).
    """
    if shutil.which("dotnet") is None:
        return None
    try:
        out = subprocess.run(["dotnet", "--list-sdks"], capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None

    best: tuple[tuple[int, ...], Path, str] | None = None
    for line in out.stdout.splitlines():
        # "9.0.312 [C:\Program Files\dotnet\sdk]"
        m = re.match(r"^(\S+)\s+\[(.+)\]\s*$", line.strip())
        if not m:
            continue
        version, root = m.group(1), m.group(2)
        bincore = Path(root) / version / "Roslyn" / "bincore"
        if not (bincore / "Microsoft.CodeAnalysis.CSharp.dll").is_file():
            continue
        parts = tuple(int(p) for p in re.findall(r"\d+", version)[:3])
        if best is None or parts > best[0]:
            best = (parts, bincore, f"net{parts[0]}.0")
    if best is None:
        return None
    return best[1], best[2]


def _tool_binary(build_dir: Path, tfm: str) -> Path:
    return build_dir / tfm / "SyntaxGate.dll"


def _ensure_built(roslyn: Path, tfm: str) -> Path | None:
    """Build the gate once and cache it. Returns the dll path, or None."""
    build_dir = _TOOL_DIR / "bin" / "gate"
    binary = _tool_binary(build_dir, tfm)
    if binary.is_file():
        return binary

    cmd = [
        "dotnet", "build", str(_TOOL_DIR / "SyntaxGate.csproj"),
        "-c", "Release", "--nologo", "-v", "quiet",
        f"-p:RoslynDir={roslyn.as_posix()}",
        f"-p:GateTfm={tfm}",
        f"-p:OutputPath={(build_dir / tfm).as_posix()}/",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"  [yellow]WARN[/] syntax gate build failed ({exc})")
        return None
    if proc.returncode != 0 or not binary.is_file():
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-5:]
        console.print("  [yellow]WARN[/] syntax gate build failed:\n    "
                      + "\n    ".join(tail))
        return None
    return binary


def check_files(paths, *, enabled: bool = True) -> GateResult:
    """Parse every .cs file in `paths`. Never raises — inspect the result."""
    files = [Path(p) for p in paths]
    files = [p for p in files if p.suffix == ".cs" and p.is_file()]
    result = GateResult(checked=len(files))
    if not enabled:
        result.skipped_reason = "disabled via unity_pipeline.syntax_gate"
        return result
    if not files:
        return result

    sdk = _find_sdk()
    if sdk is None:
        result.skipped_reason = (
            "no .NET SDK found (install from https://dotnet.microsoft.com/download "
            "to enable syntax checking of generated C#)")
        return result
    roslyn, tfm = sdk

    binary = _ensure_built(roslyn, tfm)
    if binary is None:
        result.skipped_reason = "syntax gate tool could not be built"
        return result

    # A response file, not argv: a composed project runs to hundreds of scripts
    # and Windows caps a command line at ~32k characters.
    fd, list_path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(str(p.resolve()) for p in files))
        proc = subprocess.run(["dotnet", str(binary), f"@{list_path}"],
                              capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        result.skipped_reason = f"syntax gate could not run ({exc})"
        return result
    finally:
        Path(list_path).unlink(missing_ok=True)

    if proc.returncode == 2:
        result.skipped_reason = f"syntax gate invocation error: {proc.stderr.strip()}"
        return result

    for line in proc.stdout.splitlines():
        m = _DIAG_RE.match(line.strip())
        if m:
            result.diagnostics.append(SyntaxDiagnostic(
                file=m.group("file"), line=int(m.group("line")),
                column=int(m.group("col")), code=m.group("code"),
                message=m.group("message")))
    return result


def enforce(paths, *, enabled: bool = True, label: str = "generated scripts") -> GateResult:
    """`check_files`, but a parse failure raises CSharpSyntaxError."""
    result = check_files(paths, enabled=enabled)
    if result.skipped:
        console.print(f"  [yellow]WARN[/] syntax gate skipped — {result.skipped_reason}")
        return result
    if result.diagnostics:
        for d in result.diagnostics:
            console.print(f"  [red]CS![/]  {d}")
        raise CSharpSyntaxError(result.diagnostics)
    if result.checked:
        console.print(f"  [green]OK[/]   syntax gate: {result.checked} {label} parse clean")
    return result
