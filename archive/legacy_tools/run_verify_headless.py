#!/usr/bin/env python3
"""
Run the Phase 3 verification matrix against REAL Blender (headless --background)
and emit a pass/fail report. Same emitted script as the live MCP backend, so this
validates the reuse + rig substrate identically.

Steps:
  1. run each matrix script (cold → reuse_* → cold_softbody), reading its manifest;
  2. inspect the pre-rig mesh.blend + every produced FBX (verts/UVs/groups/bones);
  3. assert: geo_hash(cold) == geo_hash(every reuse)   [append is identity-preserving]
            reuse_humanoid == reuse_humanoid2          [reuse determinism]
            rig micro produced the right armature/groups [static→none, mech→root+body,
                                                          humanoid→rigify]
            FBX/Alembic exports exist + non-empty.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.blender.locator import find_blender

ROOT = Path("output/blender/_verify").resolve()
SCRIPTS = ROOT / "_scripts"
_blender = find_blender(None)
if _blender is None:
    raise SystemExit("No Blender install found — cannot run headless verification.")
BL = str(_blender.path)
INDEX = json.loads((ROOT / "index.json").read_text(encoding="utf-8"))


def run_blender(script_path: str) -> int:
    cmd = [BL, "--background", "--factory-startup", "--python", script_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"    [rc={r.returncode}] tail: {r.stdout[-400:]}\n{r.stderr[-400:]}")
    return r.returncode


# Inspection script: opens the pre-rig mesh.blend, then imports each FBX in a
# clean scene and reports mesh/armature stats. Reads jobs from index.json.
_INSPECT = r'''
import bpy, json, os
ROOT = r"{root}"
INDEX = json.load(open(os.path.join(ROOT, "index.json"), encoding="utf-8"))
out = {{"mesh_blend": None, "fbx": {{}}}}

def mesh_stats(o):
    return {{"name": o.name, "verts": len(o.data.vertices), "polys": len(o.data.polygons),
             "uv_layers": [u.name for u in o.data.uv_layers],
             "smooth_polys": sum(1 for p in o.data.polygons if p.use_smooth),
             "vertex_groups": [g.name for g in o.vertex_groups],
             "loc": [round(c, 4) for c in o.location],
             "dims": [round(c, 4) for c in o.dimensions]}}

# 1) pre-rig mesh.blend (the reusable unit)
cold = next((it for it in INDEX if it["label"] == "cold"), None)
mb = cold and cold.get("mesh_blend")
if mb and os.path.exists(mb):
    try:
        bpy.ops.wm.open_mainfile(filepath=mb)
        ms = [mesh_stats(o) for o in bpy.data.objects if o.type == "MESH"]
        arms = [a.name for a in bpy.data.objects if a.type == "ARMATURE"]
        out["mesh_blend"] = {{"path": mb, "meshes": ms, "armatures_present": arms}}
    except Exception as ex:
        out["mesh_blend"] = {{"path": mb, "error": str(ex)}}

# 2) every produced FBX
try:
    bpy.ops.preferences.addon_enable(module="io_scene_fbx")
except Exception:
    pass
for it in INDEX:
    fbx = os.path.join(it["out_dir"], it["name"] + ".fbx")
    rec = {{"exists": os.path.exists(fbx),
            "bytes": (os.path.getsize(fbx) if os.path.exists(fbx) else 0)}}
    if rec["exists"]:
        try:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            bpy.ops.preferences.addon_enable(module="io_scene_fbx")
            bpy.ops.import_scene.fbx(filepath=fbx)
            rec["meshes"] = [mesh_stats(o) for o in bpy.data.objects if o.type == "MESH"]
            rec["armatures"] = [{{"name": a.name, "bones": len(a.data.bones),
                                 "bone_names": [b.name for b in a.data.bones][:8]}}
                                for a in bpy.data.objects if a.type == "ARMATURE"]
        except Exception as ex:
            rec["import_error"] = str(ex)
    out["fbx"][it["label"]] = rec

json.dump(out, open(os.path.join(ROOT, "inspect.json"), "w", encoding="utf-8"), indent=2)
print("[inspect] wrote inspect.json")
'''


def main():
    print(f"Blender: {BL}")
    # 1) run the matrix
    manifests = {}
    for it in INDEX:
        print(f"  run {it['label']} ...")
        rc = run_blender(it["script"])
        man = Path(it["manifest"])
        manifests[it["label"]] = json.loads(man.read_text(encoding="utf-8")) if man.exists() else None

    # 2) inspect
    insp_path = SCRIPTS / "_inspect.py"
    insp_path.write_text(_INSPECT.format(root=str(ROOT).replace("\\", "/")), encoding="utf-8")
    print("  inspect ...")
    run_blender(str(insp_path))
    inspect = json.loads((ROOT / "inspect.json").read_text(encoding="utf-8"))

    # 3) assertions
    checks = []

    def chk(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    cold_m = manifests.get("cold")
    cold_hash = (cold_m or {}).get("geo_hash")
    chk("cold built + geo_hash", bool(cold_hash) and not (cold_m or {}).get("error"),
        f"geo_hash={cold_hash} err={(cold_m or {}).get('error')}")
    chk("cold saved pre-rig mesh.blend", bool((cold_m or {}).get("mesh_blend")),
        str((cold_m or {}).get("mesh_blend")))

    reuse_labels = ["reuse_humanoid", "reuse_humanoid2", "reuse_mechanical",
                    "reuse_static", "reuse_ragdoll"]
    for lbl in reuse_labels:
        m = manifests.get(lbl) or {}
        h = m.get("geo_hash")
        chk(f"{lbl}: geo_hash == cold (append identity)", h == cold_hash and cold_hash,
            f"{h} vs {cold_hash}  warnings={m.get('warnings')}")
        chk(f"{lbl}: geo_reuse ran", "geo_reuse" in (m.get("operations") or []),
            str(m.get("operations")))

    chk("reuse determinism (h == h2)",
        (manifests.get("reuse_humanoid") or {}).get("geo_hash")
        == (manifests.get("reuse_humanoid2") or {}).get("geo_hash"))

    # rig micro shapes from FBX inspection
    fbx = inspect.get("fbx", {})

    def arms(lbl):
        return (fbx.get(lbl) or {}).get("armatures", [])

    def groups(lbl):
        ms = (fbx.get(lbl) or {}).get("meshes", [])
        return [g for mm in ms for g in mm.get("vertex_groups", [])]

    chk("static: no armature", len(arms("reuse_static")) == 0, str(arms("reuse_static")))
    mech_bones = arms("reuse_mechanical")[0]["bone_names"] if arms("reuse_mechanical") else []
    chk("mechanical: root+body armature", set(["root", "body"]).issubset(set(mech_bones)),
        str(mech_bones))
    chk("mechanical: mesh bound (vertex groups)", len(groups("reuse_mechanical")) > 0,
        str(groups("reuse_mechanical"))[:120])
    chk("ragdoll: mechanical armature", len(arms("reuse_ragdoll")) >= 1, str(arms("reuse_ragdoll")))
    chk("humanoid: rigify armature (>1 bone)",
        bool(arms("reuse_humanoid")) and arms("reuse_humanoid")[0]["bones"] > 1,
        str([(a["name"], a["bones"]) for a in arms("reuse_humanoid")]))

    # pre-rig mesh has UVs and no armature
    mbi = inspect.get("mesh_blend") or {}
    mbmesh = (mbi.get("meshes") or [{}])[0] if mbi.get("meshes") else {}
    chk("pre-rig mesh has UVs", bool(mbmesh.get("uv_layers")), str(mbmesh.get("uv_layers")))
    chk("pre-rig mesh has no armature", not mbi.get("armatures_present"),
        str(mbi.get("armatures_present")))

    # exports
    for lbl in ["cold"] + reuse_labels:
        rec = fbx.get(lbl) or {}
        chk(f"{lbl}: FBX exported (non-empty)", rec.get("exists") and rec.get("bytes", 0) > 0,
            f"{rec.get('bytes')} bytes")
    sb = manifests.get("cold_softbody") or {}
    abc = sb.get("alembic")
    chk("softbody: Alembic (.abc) exported",
        bool(abc) and Path(abc).exists() and Path(abc).stat().st_size > 0,
        f"{abc} ({Path(abc).stat().st_size if abc and Path(abc).exists() else 0} bytes)")

    # report
    report = {"blender": BL, "cold_geo_hash": cold_hash,
              "manifests": manifests, "inspect": inspect,
              "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]}
    (ROOT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    npass = sum(1 for _, ok, _ in checks if ok)
    print("\n================ PHASE 3 VERIFICATION ================")
    for n, ok, d in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"   — {d}" if not ok or d else ""))
    print(f"\n  {npass}/{len(checks)} checks passed   (report -> {ROOT / 'report.json'})")
    return 0 if npass == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
