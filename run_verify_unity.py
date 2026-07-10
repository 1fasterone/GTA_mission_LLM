#!/usr/bin/env python3
"""
Verify the Phase-3 FBX exports IMPORT correctly into Unity (the consumption side).

No running Editor instance is needed: this creates a throwaway Unity project, drops
the verified FBX + a small Editor inspector script, and runs Unity in batchmode to
report, per asset: mesh count, materials, skinned-vs-static, avatar, vertex count,
and any import errors. Asserts rigged FBX → SkinnedMeshRenderer, static → MeshFilter.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.argv = ["x"]
from unity_cli import _find_editor

EDITOR = _find_editor(None)
if EDITOR is None:
    raise SystemExit("no Unity editor found")
EDITOR = Path(EDITOR)
EDITOR_VER = EDITOR.parent.parent.name   # .../Hub/Editor/<ver>/Editor/Unity.exe

PROJ = Path("output/unity/_import_check").resolve()
VERIFY = Path("output/blender/_verify").resolve()
REPORT = PROJ / "import_report.json"

# FBX → whether we expect a skinned (rigged) import.
FBX = {
    "vh_cold":       (VERIFY / "vh_cold" / "vh_cold.fbx", True),        # rigify humanoid
    "vh_reuse_h":    (VERIFY / "vh_reuse_h" / "vh_reuse_h.fbx", True),  # reused + rigify
    "vh_reuse_mech": (VERIFY / "vh_reuse_mech" / "vh_reuse_mech.fbx", True),  # mechanical
    "vh_static":     (VERIFY / "vh_static" / "vh_static.fbx", False),   # no armature
}

EDITOR_CS = r'''
using System;
using System.IO;
using System.Linq;
using System.Text;
using UnityEditor;
using UnityEngine;

public static class AssetImportCheck
{
    public static void Run()
    {
        AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport | ImportAssetOptions.ForceUpdate);
        var guids = AssetDatabase.FindAssets("t:Model", new[] { "Assets/_Verify" });
        var sb = new StringBuilder();
        sb.Append("{\n  \"unity\": \"" + Application.unityVersion + "\",\n  \"assets\": [\n");
        bool first = true; bool allOk = true;
        foreach (var g in guids)
        {
            string path = AssetDatabase.GUIDToAssetPath(g);
            var go = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            var objs = AssetDatabase.LoadAllAssetsAtPath(path);
            int meshes = objs.Count(o => o is Mesh);
            int mats = objs.Count(o => o is Material);
            int avatars = objs.Count(o => o is Avatar);
            int verts = objs.OfType<Mesh>().Sum(m => m.vertexCount);
            int skinned = go ? go.GetComponentsInChildren<SkinnedMeshRenderer>(true).Length : 0;
            int filters = go ? go.GetComponentsInChildren<MeshFilter>(true).Length : 0;
            bool ok = meshes > 0 && go != null;
            // Per-material shader + texture binding. A material stuck on the
            // built-in Standard shader in a URP-active project renders
            // magenta/black — that combination is a hard failure.
            bool urpActive = UnityEngine.Rendering.GraphicsSettings.currentRenderPipeline != null;
            var matJson = string.Join(", ", objs.OfType<Material>().Select(m2 => {
                string shName = m2.shader ? m2.shader.name : "null";
                bool hasTex = (m2.HasProperty("_BaseMap") && m2.GetTexture("_BaseMap") != null)
                           || (m2.HasProperty("_MainTex") && m2.GetTexture("_MainTex") != null);
                if (urpActive && (shName == "Standard" || shName.StartsWith("Standard ("))) ok = false;
                return "{\"name\": \"" + m2.name + "\", \"shader\": \"" + shName +
                       "\", \"textured\": " + (hasTex ? "true" : "false") + "}";
            }));
            allOk &= ok;
            if (!first) sb.Append(",\n"); first = false;
            sb.Append("    {\"path\": \"" + path + "\", \"ok\": " + (ok?"true":"false") +
                ", \"meshes\": " + meshes + ", \"materials\": " + mats + ", \"avatars\": " + avatars +
                ", \"skinned\": " + skinned + ", \"meshFilters\": " + filters + ", \"verts\": " + verts +
                ", \"urpActive\": " + (urpActive?"true":"false") + ", \"mats\": [" + matJson + "]}");
        }
        sb.Append("\n  ],\n  \"count\": " + guids.Length + ",\n  \"allOk\": " +
            ((allOk && guids.Length > 0) ? "true" : "false") + "\n}\n");
        string outp = Environment.GetEnvironmentVariable("IMPORT_REPORT");
        if (string.IsNullOrEmpty(outp)) outp = "import_report.json";
        File.WriteAllText(outp, sb.ToString());
        Debug.Log("IMPORT_CHECK_DONE " + guids.Length + " assets, allOk=" + (allOk && guids.Length>0));
        EditorApplication.Exit((allOk && guids.Length > 0) ? 0 : 1);
    }
}
'''


def main():
    print(f"Unity editor: {EDITOR}  (project version pinned {EDITOR_VER})")
    if PROJ.exists():
        shutil.rmtree(PROJ, ignore_errors=True)
    (PROJ / "Assets" / "Editor").mkdir(parents=True, exist_ok=True)
    (PROJ / "Assets" / "_Verify").mkdir(parents=True, exist_ok=True)
    (PROJ / "Packages").mkdir(parents=True, exist_ok=True)
    (PROJ / "ProjectSettings").mkdir(parents=True, exist_ok=True)

    (PROJ / "Assets" / "Editor" / "AssetImportCheck.cs").write_text(EDITOR_CS, encoding="utf-8")
    (PROJ / "Packages" / "manifest.json").write_text(
        json.dumps({"dependencies": {}}, indent=2), encoding="utf-8")
    (PROJ / "ProjectSettings" / "ProjectVersion.txt").write_text(
        f"m_EditorVersion: {EDITOR_VER}\n", encoding="utf-8")

    present = {}
    for name, (src, rigged) in FBX.items():
        if src.is_file():
            shutil.copy2(src, PROJ / "Assets" / "_Verify" / f"{name}.fbx")
            present[name] = rigged
        else:
            print(f"  [skip] {name}: source FBX missing ({src})")

    log = PROJ / "unity_import.log"
    env = dict(os.environ, IMPORT_REPORT=str(REPORT))
    cmd = [str(EDITOR), "-batchmode", "-quit", "-nographics",
           "-projectPath", str(PROJ), "-executeMethod", "AssetImportCheck.Run",
           "-logFile", str(log)]
    print("  running Unity batchmode import check (first launch resolves packages — be patient)...")
    rc = subprocess.run(cmd, timeout=900, env=env).returncode
    print(f"  Unity exited rc={rc}")

    if not REPORT.exists():
        print("  [FAIL] no import_report.json — Unity didn't reach the check.")
        if log.exists():
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            print("  --- log tail ---\n   " + "\n   ".join(tail))
        return 1

    rep = json.loads(REPORT.read_text(encoding="utf-8"))
    by_path = {Path(a["path"]).stem: a for a in rep["assets"]}

    # import-error scan in the Unity log
    errs = []
    if log.exists():
        for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
            low = ln.lower()
            if ("error" in low) and any(k in low for k in ("import", "fbx", "_verify", "model")):
                errs.append(ln.strip())

    checks = []
    def chk(n, ok, d=""): checks.append((n, bool(ok), d))

    chk(f"Unity {rep.get('unity')} imported all assets", rep.get("allOk"), str(rep.get("count")))
    for name, rigged in present.items():
        a = by_path.get(name)
        if not a:
            chk(f"{name}: imported", False, "not found in report"); continue
        chk(f"{name}: valid mesh ({a['meshes']}m/{a['verts']}v, {a['materials']}mat)",
            a["ok"] and a["meshes"] > 0, str(a))
        if rigged:
            chk(f"{name}: skinned (rigged import)", a["skinned"] > 0,
                f"skinned={a['skinned']} avatars={a['avatars']}")
        else:
            chk(f"{name}: static MeshFilter (no skin)", a["meshFilters"] > 0 and a["skinned"] == 0,
                f"filters={a['meshFilters']} skinned={a['skinned']}")
    chk("no FBX import errors in log", not errs, ("; ".join(errs[:3]) if errs else ""))

    npass = sum(1 for _, ok, _ in checks if ok)
    print("\n================ UNITY IMPORT VERIFICATION ================")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"   — {d}" if (not ok and d) else ""))
    print(f"\n  {npass}/{len(checks)} checks passed   (report -> {REPORT})")
    return 0 if npass == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
