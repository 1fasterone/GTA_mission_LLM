#!/usr/bin/env python3
"""
Phase 3 live-verification harness — emits the bpy scripts that prove the geometry
reuse + rig-router substrate is correct, BEFORE the armature library is built.

It does NOT touch Blender itself (the host process can't call MCP). It writes a
matrix of self-contained scripts to `output/blender/_verify/_scripts/` plus an
`index.json`. The agent then execs each script inside the connected Blender via
`mcp__blender__execute_blender_code` (one-liner: exec the file), and reads the
`<name>.manifest.json` each one drops — robust regardless of the MCP return value.

Matrix:
  cold            full build of a humanoid mesh → saves <name>.mesh.blend + FBX,
                  records geo_hash. This is the source of truth for reuse.
  reuse_humanoid  append the cached mesh, RigAutoHumanoidV1, export. geo_hash MUST
                  equal cold's (append round-trip is identity-preserving).
  reuse_humanoid2 same again → proves reuse determinism (identical geo_hash twice).
  reuse_mechanical append cached mesh, RigAutoMechanicalV1 (root+body bones).
  reuse_static    append cached mesh, no rig step (static export).
  reuse_ragdoll   append cached mesh, RigAutoRagdollV1 (→ mechanical fallback).
  cold_softbody   soft-body cold build → Alembic export correctness.

Each reuse job uses a UNIQUE name (so one Blender session doesn't collide on
object names) but loads the SAME cold mesh.blend — geo_hash is vertex-based, so it
is name-independent and must match.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.blender import build_job
from src.blender.mcp_backend import build_job_code, steps_for
from src.blender.rigging import rig_strategy

ROOT = Path("output/blender/_verify").resolve()
SCRIPTS = ROOT / "_scripts"
SCRIPTS.mkdir(parents=True, exist_ok=True)

GEO_STEPS = ("generate", "cleanup", "unwrap")


def _mk(name, asset_type, *, rig=False, anims=(), soft_body=False, seed=7):
    j = build_job(name=name, asset_type=asset_type, style="low_poly")
    j.rig = rig
    j.animations = list(anims)
    j.soft_body = soft_body
    j.detail = 3
    j.seed = seed
    return j.default_output(str(ROOT))


def _write(label, code):
    p = SCRIPTS / f"{label}.py"
    p.write_text(code, encoding="utf-8")
    return p


def main():
    index = []

    # 1) COLD humanoid build — the reuse source of truth.
    cold = _mk("vh_cold", "character", rig=True, anims=("walk", "run"))
    cold_out = ROOT / cold.name
    cold_code = build_job_code(cold, str(cold_out),
                               rig_strategy_id=rig_strategy(cold).id,
                               write_manifest=True, save_mesh=True)
    p = _write("cold", cold_code)
    mesh_blend = str((cold_out / f"{cold.name}.mesh.blend"))
    index.append({"label": "cold", "script": str(p), "out_dir": str(cold_out),
                  "name": cold.name, "manifest": str(cold_out / f"{cold.name}.manifest.json"),
                  "mesh_blend": mesh_blend, "rig_strategy": rig_strategy(cold).id,
                  "expect": "saves mesh.blend + fbx; record geo_hash (reference)"})

    # 2..N) REUSE jobs — all load the cold mesh, vary the rig micro.
    def reuse(label, name, rig_id, with_rig=True):
        j = _mk(name, "character", rig=with_rig, anims=("walk",))
        steps = [s for s in steps_for(j) if s not in GEO_STEPS]
        if not with_rig:
            steps = [s for s in steps if s not in ("rig", "animate")]
        out = ROOT / name
        code = build_job_code(j, str(out), steps=steps, reuse_blend=mesh_blend,
                              rig_strategy_id=rig_id, write_manifest=True, save_mesh=False)
        pp = _write(label, code)
        index.append({"label": label, "script": str(pp), "out_dir": str(out),
                      "name": name, "manifest": str(out / f"{name}.manifest.json"),
                      "reuse_blend": mesh_blend, "rig_strategy": rig_id,
                      "expect": "geo_hash == cold; armature per micro; fbx exported"})

    reuse("reuse_humanoid",  "vh_reuse_h",   "RigAutoHumanoidV1")
    reuse("reuse_humanoid2", "vh_reuse_h2",  "RigAutoHumanoidV1")   # determinism
    reuse("reuse_mechanical","vh_reuse_mech", "RigAutoMechanicalV1")
    reuse("reuse_static",    "vh_static",    "RigAutoStaticV1", with_rig=False)
    reuse("reuse_ragdoll",   "vh_ragdoll",   "RigAutoRagdollV1")

    # N+1) COLD soft-body → Alembic export correctness.
    sb = _mk("jelly_sb", "prop", soft_body=True)
    sb_out = ROOT / sb.name
    sb_code = build_job_code(sb, str(sb_out), write_manifest=True, save_mesh=True)
    p = _write("cold_softbody", sb_code)
    index.append({"label": "cold_softbody", "script": str(p), "out_dir": str(sb_out),
                  "name": sb.name, "manifest": str(sb_out / f"{sb.name}.manifest.json"),
                  "expect": "fbx + alembic (.abc) exported"})

    (ROOT / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"wrote {len(index)} scripts -> {SCRIPTS}")
    print(f"index -> {ROOT / 'index.json'}")
    for it in index:
        print(f"  {it['label']:16} name={it['name']:12} rig={it.get('rig_strategy','-')}")


if __name__ == "__main__":
    main()
