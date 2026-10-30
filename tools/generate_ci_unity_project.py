"""Materialize one deterministic eval project for CI compile validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.unity_eval import load_cases, replay
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
    args = parser.parse_args()

    case = load_cases(only=[args.case])[0]
    schema = UnitySchema.model_validate_json(
        case.schema_path.read_text(encoding="utf-8")
    )
    schema, files, _log = replay(schema)
    output = args.output.resolve()
    for relative, content in files.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        else:
            destination.write_text(content, encoding="utf-8")
    (output / "ci-schema.json").write_text(
        json.dumps(schema.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    compile_check = output / "Assets" / "Editor" / "CiCompileCheck.cs"
    compile_check.parent.mkdir(parents=True, exist_ok=True)
    compile_check.write_text(COMPILE_CHECK, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
