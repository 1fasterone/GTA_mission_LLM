"""Generate a frozen-schema Unity project and add a compile-gate smoke test.

This is a compiler fixture, not a live Pipeline LLM evaluation. The caller
owns the fresh output directory; this script refuses to overwrite an existing
one and does not modify the captured schema.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "tests/fixtures/dropzone_arcade_llm.schema.json"
SMOKE_NAME = "GeneratedRuntimeAssemblyIsLoaded"
TEST_SOURCE = '''using System;
using System.Linq;
using NUnit.Framework;

public class GeneratedCompileSmoke
{
    [Test]
    public void GeneratedRuntimeAssemblyIsLoaded()
    {
        Assert.IsTrue(AppDomain.CurrentDomain.GetAssemblies().Any(
            assembly => assembly.GetName().Name == "Assembly-CSharp"),
            "The generated game assembly was not loaded.");
    }
}
'''


def prepare_fixture(output: Path, schema_file: Path = DEFAULT_SCHEMA) -> Path:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing fixture directory: {output}")
    schema_path = schema_file.resolve(strict=True)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    project_name = schema.get("project_name")
    if not isinstance(project_name, str) or not project_name.isidentifier():
        raise ValueError("fixture project_name must be a simple identifier")
    output.mkdir(parents=True)
    project_root = output / "projects"
    config = {
        "unity_pipeline": {
            "lookups_dir": str(ROOT / "lookups"),
            "unity_template_dir": str(ROOT / "templates/unity"),
            "unity_output_dir": str(project_root),
            "inject_mcp_bridge": False,
        },
        "assets": {"strict": False},
        "sketchfab": {"enabled": False, "auto_fetch": False, "vision_select": False},
    }
    config_path = output / "compile-config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "unity-validate", str(schema_path),
         "--config", str(config_path), "--compose"],
        cwd=ROOT, check=True, timeout=600,
    )
    project = project_root / project_name
    assets = project / "Assets"
    manifest = project / "Packages/manifest.json"
    version = project / "ProjectSettings/ProjectVersion.txt"
    if not version.is_file() or not manifest.is_file() or not any(assets.rglob("*.cs")):
        raise RuntimeError("generation produced an incomplete Unity project or no C# sources")
    package_data = json.loads(manifest.read_text(encoding="utf-8"))
    dependencies = package_data.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("generated Unity manifest has no dependencies object")
    dependencies.setdefault("com.unity.test-framework", "1.4.5")
    manifest.write_text(json.dumps(package_data, indent=2) + "\n", encoding="utf-8")
    test_dir = assets / "Tests/Editor"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "GeneratedCompileSmoke.cs").write_text(TEST_SOURCE, encoding="utf-8")
    (test_dir / "GeneratedCompileSmoke.asmdef").write_text(json.dumps({
        "name": "GeneratedCompileSmoke",
        "references": [],
        "includePlatforms": ["Editor"],
        "optionalUnityReferences": ["TestAssemblies"],
    }, indent=2) + "\n", encoding="utf-8")
    return project


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args()
    print(f"Unity compile fixture: {prepare_fixture(args.output, args.schema)}")


if __name__ == "__main__":
    main()
