"""Prove the installed .NET compiler detects both requested semantic errors.

The probe uses a minimal C# adapter, not Unity assemblies. It verifies the
compiler/check itself; the separate Unity job compiles real generated sources.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from src.csharp_syntax_gate import _ensure_built, _find_sdk
from src.unity_compose import MicroComposer, subsystems_for


STUBS = '''namespace UnityEngine {
    public class Transform { public int childCount; }
    public class MonoBehaviour { public Transform transform = new Transform(); }
}
'''
PROGRAMS = {
    "clean": "class Example : UnityEngine.MonoBehaviour { int Read() => transform.childCount; }",
    "CS0246": "class Example : UnityEngine.MonoBehaviour { MissingType_ForCompileGate field; }",
    "CS1061": "class Example : UnityEngine.MonoBehaviour { void Run() { transform.MissingMember_ForCompileGate(); } }",
}


def run() -> None:
    if not shutil.which("dotnet"):
        raise RuntimeError(".NET SDK is required for the CI semantic and syntax gates")
    sdk = _find_sdk()
    if sdk is None:
        raise RuntimeError("no usable .NET SDK with Roslyn was discovered")
    roslyn, tfm = sdk
    binary = _ensure_built(roslyn, tfm)
    if binary is None:
        raise RuntimeError("the Roslyn syntax gate could not be built")
    with tempfile.TemporaryDirectory(prefix="csharp-ci-") as temp:
        root = Path(temp)
        for expectation, code in PROGRAMS.items():
            case = root / expectation
            case.mkdir()
            (case / "NuGet.Config").write_text(
                '<configuration><packageSources><clear /></packageSources></configuration>', encoding="utf-8")
            (case / "Probe.csproj").write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
                f'<TargetFramework>{tfm}</TargetFramework>'
                '</PropertyGroup></Project>', encoding="utf-8")
            (case / "Probe.cs").write_text(STUBS + code, encoding="utf-8")
            result = subprocess.run(["dotnet", "build", str(case / "Probe.csproj"),
                                     "--nologo", "-v", "quiet"],
                                    capture_output=True, text=True, timeout=120)
            output = result.stdout + result.stderr
            if expectation == "clean":
                if result.returncode != 0:
                    raise RuntimeError(f"clean C# control did not build:\n{output}")
            elif result.returncode == 0 or expectation not in output:
                raise RuntimeError(f"expected {expectation} and a failing build; got:\n{output}")
            print(f"semantic control {expectation}: verified")

        assets = root / "Assets"
        composer = MicroComposer("CISyntaxFixture", project_config={"audio": {"backend": "dsp"}})
        composer.emit(subsystems_for("3D", [], locomotion="walk"), assets, has_game_manager=False)
        files = sorted(assets.rglob("*.cs"))
        if not files:
            raise RuntimeError("no generated C# was available for syntax checking")

        def syntax_check(paths: list[Path]) -> subprocess.CompletedProcess[str]:
            listing = root / "syntax-inputs.txt"
            listing.write_text("\n".join(str(p.resolve()) for p in paths), encoding="utf-8")
            return subprocess.run(["dotnet", str(binary), f"@{listing}"],
                                  capture_output=True, text=True, timeout=300)

        result = syntax_check(files)
        if result.returncode != 0:
            raise RuntimeError("generated C# syntax check failed:\n" + result.stdout + result.stderr)
        # Prove the gate itself reports syntax errors rather than returning a
        # false pass because diagnostics could not be parsed.
        broken = root / "Broken.cs"
        broken.write_text("public class Broken {", encoding="utf-8")
        negative = syntax_check([broken])
        if negative.returncode != 1 or "CS1513" not in negative.stdout:
            raise RuntimeError("syntax negative control did not report CS1513:\n"
                               + negative.stdout + negative.stderr)
        print(f"generated C# syntax: {len(files)} files checked; negative control verified")



if __name__ == "__main__":
    run()
