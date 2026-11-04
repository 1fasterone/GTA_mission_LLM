"""Materialize one deterministic eval project for CI compile validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.unity_eval import load_cases, replay
from src.unity_generator import UnityProjectGenerator
from src.unity_schema import UnitySchema


COMPILE_CHECK = """using System;
using UnityEditor;
using UnityEngine;

public static class CiCompileCheck
{
    public static void Perform()
    {
        if (EditorUtility.scriptCompilationFailed)
            throw new Exception("Generated project contains C# compilation errors.");
        Debug.Log("Generated project compiled successfully; CI compile entry point reached.");
    }
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", default="dropzone_arcade")
    parser.add_argument("--github-output", type=Path, help="Actions output file receiving project_path")
    args = parser.parse_args()

    case = load_cases(only=[args.case])[0]
    schema = UnitySchema.model_validate_json(
        case.schema_path.read_text(encoding="utf-8")
    )
    schema, files, _log = replay(schema)
    manifest = json.loads(files[f"{schema.project_name}/Packages/manifest.json"])
    generator = UnityProjectGenerator(template_dir=str(REPO_ROOT / "templates" / "unity"))
    # write() fulfils vendor sources, profiles, shaders and binary models alongside
    # generated text. Materializing generate() alone leaves referenced types out.
    output, _count = generator.write(
        schema, manifest["dependencies"], output_dir=str(args.output.resolve()),
        lookups_dir=str(REPO_ROOT / "lookups"), strict_assets=False,
        sketchfab_cfg={"enabled": False, "auto_fetch": False, "vision_select": False},
    )
    (output / "ci-schema.json").write_text(
        json.dumps(schema.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    compile_check = output / "Assets" / "Editor" / "CiCompileCheck.cs"
    compile_check.parent.mkdir(parents=True, exist_ok=True)
    compile_check.write_text(COMPILE_CHECK, encoding="utf-8")
    if args.github_output is not None:
        project_path = output.relative_to(Path.cwd().resolve()).as_posix()
        with args.github_output.open("a", encoding="utf-8") as outputs:
            outputs.write(f"project_path={project_path}\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
