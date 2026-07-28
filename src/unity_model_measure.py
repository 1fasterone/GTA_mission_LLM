"""Measure real imported model bounds using Unity itself.

Why Unity and not a Python FBX parser
-------------------------------------
`src/unity_fbx_bounds.py` was the first attempt and it cannot be made correct
for this library. Measured against Unity ground truth, two models that both
declare `UnitScaleFactor: 1.0` still import differently:

    big-head-cromulon.fbx   raw units are METRES,      Z-up
    blaster-d.fbx           raw units are CENTIMETRES, Y-up

No header field distinguishes them — the difference lives in each exporter's
conventions and in how Unity's own importer resolves them. On top of that most
Kenney packs ship ASCII FBX, which Blender cannot even import. So the only
authority on "how big will this be in Unity" is Unity.

This module builds a throwaway project containing the model roots, imports them
once in batchmode, and records each asset's combined renderer bounds at
localScale 1. The result is cached into the asset catalog, so the cost is paid
once per library change rather than per build.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

_EDITOR_CS = r'''
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEngine;

public static class ModelBoundsMeasure
{
    public static void Run()
    {
        var sb = new StringBuilder();
        sb.AppendLine("{");
        bool first = true;
        foreach (var guid in AssetDatabase.FindAssets("t:Model", new[] { "Assets/Models" }))
        {
            var path = AssetDatabase.GUIDToAssetPath(guid);
            var go   = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (go == null) continue;
            var inst = Object.Instantiate(go);
            inst.transform.localScale = Vector3.one;
            inst.transform.position   = Vector3.zero;
            inst.transform.rotation   = Quaternion.identity;
            var rends = inst.GetComponentsInChildren<Renderer>();
            if (rends.Length > 0)
            {
                var b = rends[0].bounds;
                for (int i = 1; i < rends.Length; i++) b.Encapsulate(rends[i].bounds);
                // Key on the path RELATIVE to Assets/Models — filenames repeat
                // across packs (many kits ship their own "floor.fbx").
                var rel = path.Substring("Assets/Models/".Length).Replace("\\", "/");
                if (!first) sb.AppendLine(",");
                first = false;
                sb.Append($"  \"{rel}\": [{b.size.x:F4}, {b.size.y:F4}, {b.size.z:F4}]");
            }
            Object.DestroyImmediate(inst);
        }
        sb.AppendLine();
        sb.AppendLine("}");
        File.WriteAllText(System.Environment.GetEnvironmentVariable("BOUNDS_OUT"), sb.ToString());
        EditorApplication.Exit(0);
    }
}
'''


def _find_editor() -> Optional[Path]:
    from run_verify_unity import _find_editor as f  # reuse the existing probe
    return f(None)


def measure_roots(roots: Iterable[str | Path],
                  work_dir: Path,
                  timeout: int = 3600) -> dict[str, list[float]]:
    """Import every model under `roots` in a scratch Unity project and return
    {relpath: [x, y, z]} sizes in metres, as Unity itself imports them."""
    editor = _find_editor()
    if editor is None:
        raise RuntimeError("no Unity editor found — cannot measure model bounds")

    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    (work_dir / "Assets" / "Editor").mkdir(parents=True, exist_ok=True)
    (work_dir / "Packages").mkdir(parents=True, exist_ok=True)
    (work_dir / "ProjectSettings").mkdir(parents=True, exist_ok=True)

    ver = editor.parent.parent.name
    (work_dir / "Assets" / "Editor" / "ModelBoundsMeasure.cs").write_text(_EDITOR_CS, encoding="utf-8")
    (work_dir / "Packages" / "manifest.json").write_text(
        json.dumps({"dependencies": {}}, indent=2), encoding="utf-8")
    (work_dir / "ProjectSettings" / "ProjectVersion.txt").write_text(
        f"m_EditorVersion: {ver}\n", encoding="utf-8")

    dest_root = work_dir / "Assets" / "Models"
    for root in roots:
        src = Path(root)
        if not src.is_dir():
            continue
        # Mirror the catalog's own relpath layout so keys line up exactly.
        shutil.copytree(src, dest_root, dirs_exist_ok=True)

    out_json = work_dir / "bounds.json"
    log      = work_dir / "measure.log"
    env = dict(os.environ, BOUNDS_OUT=str(out_json))
    subprocess.run([str(editor), "-batchmode", "-quit", "-nographics",
                    "-projectPath", str(work_dir),
                    "-executeMethod", "ModelBoundsMeasure.Run",
                    "-logFile", str(log)],
                   timeout=timeout, env=env)

    if not out_json.exists():
        tail = ""
        if log.exists():
            tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
        raise RuntimeError(f"Unity produced no bounds.json — log tail:\n{tail}")
    return json.loads(out_json.read_text(encoding="utf-8"))
