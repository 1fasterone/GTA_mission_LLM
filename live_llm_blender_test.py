#!/usr/bin/env python3
"""
Live LLM -> Blender procedural-model test harness.

Prompts the LOCAL LLM (LM Studio, OpenAI-compatible) to author a self-contained
bpy script for each test description, then saves the script for the agent to
execute in the connected (live) Blender via the MCP `execute_blender_code` tool.

This exercises the LLM->Blender bridge with LLM-authored geometry — distinct from
the deterministic recipe pipeline (which can only select fixed recipes and can't
make an airplane/jeep). Every generated script is safety-scanned (it runs in the
user's live scene) AND must be reviewed before execution.

Usage:
  python live_llm_blender_test.py generate              # prompt LLM for all 3
  python live_llm_blender_test.py repair <name> <errfile>   # re-prompt with a traceback
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

LLM_URL = "http://localhost:1234/v1/chat/completions"
MODEL = "phi-4-reasoning-plus"   # reasoning model — emits <think>…</think> then the code
OUT = Path("output/blender/_llm_tests").resolve()
OUT.mkdir(parents=True, exist_ok=True)

# Each test: a recognisable object + an isolated collection + an X offset so the
# three models (and the user's scene) never overlap.
CASES = [
    {"name": "airplane", "coll": "LLM_airplane", "ox": 0.0,
     "desc": "a small airplane: a long cylindrical fuselage, a nose cone, two main "
             "wings (left/right), a horizontal tail and a vertical tail fin, and a "
             "propeller or engine at the front"},
    {"name": "jeep", "coll": "LLM_jeep", "ox": 12.0,
     "desc": "an off-road jeep: a boxy car body with a cabin, and FOUR BIG oversized "
             "chunky tires (large-radius cylinders) at the four corners, clearly "
             "bigger than a normal car's wheels"},
    {"name": "humanoid", "coll": "LLM_humanoid", "ox": 24.0,
     "desc": "a simple humanoid figure: a head (sphere), a torso, two arms and two "
             "legs, roughly human proportions, standing upright"},
]

SYSTEM = """You are an expert Blender Python (bpy) technical artist. Target Blender 5.1.
Given a description, output ONE self-contained Python script that procedurally builds
that 3D model from bpy primitives and transforms.

OUTPUT: only Python code. No markdown fences, no commentary.

USE EXACTLY THESE APIS (anything else, e.g. bpy.data.primitives or
bpy.data.objects.new(name, None) for a mesh, DOES NOT EXIST and will crash):

# create the collection ONCE:
coll = bpy.data.collections.new("{coll}")
bpy.context.scene.collection.children.link(coll)

# helper: add a primitive, move it into our collection, return the object
def part(kind, location=(0,0,0), **kw):
    getattr(bpy.ops.mesh, "primitive_" + kind + "_add")(location=location, **kw)
    obj = bpy.context.active_object
    for c in list(obj.users_collection):
        c.objects.unlink(obj)
    coll.objects.link(obj)
    return obj

# valid kinds + their kwargs:
#   part("cube", size=1.0)
#   part("uv_sphere", radius=0.5)
#   part("cylinder", radius=0.3, depth=2.0)
#   part("cone", radius1=0.3, depth=1.0)
# after creating, set obj.scale=(x,y,z) and obj.rotation_euler=(rx,ry,rz) as needed.

# coloured material:
def colour(obj, rgb):
    m = bpy.data.materials.new("M"); m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (rgb[0], rgb[1], rgb[2], 1.0)
    obj.data.materials.append(m)

COORDINATE SYSTEM — Blender is Z-UP (this is where most attempts fail):
- +Z is UP. The ground is the plane z=0. -Y is FORWARD (toward the viewer/camera). +X is right.
- Build the model STANDING ON THE GROUND: its lowest point near z=0, never floating or lying down.
- Use realistic relative proportions (a wheel is a fraction of the body; a head is ~1/7 of a human).

PRIMITIVE ORIENTATION — a cylinder/cone's length runs along its LOCAL Z by default:
- vertical pillar/leg/torso: leave rotation at 0 (stands up along Z).
- horizontal tube along X (e.g. an axle): obj.rotation_euler = (0, radians(90), 0)
- horizontal tube along Y (e.g. a fuselage pointing forward): obj.rotation_euler = (radians(90), 0, 0)
- a WHEEL is a short wide cylinder whose flat faces point sideways (axle along X):
  obj.rotation_euler = (0, radians(90), 0); obj.scale=(depth, radius, radius)
- For flat slabs (wings, car body panels, limbs) use a CUBE scaled non-uniformly, NOT a thin cylinder.

WORKED EXAMPLE (a simple but believable car — follow this level of spatial care):
  body = part("cube", location=(0,0,0.5)); body.scale=(1.0, 2.0, 0.4); colour(body,(0.2,0.3,0.7))
  cabin= part("cube", location=(0,0.2,1.0)); cabin.scale=(0.8, 1.0, 0.35); colour(cabin,(0.2,0.3,0.7))
  for (wx,wy) in [(1.0,1.3),(-1.0,1.3),(1.0,-1.3),(-1.0,-1.3)]:
      w = part("cylinder", location=(wx,wy,0.35), radius=0.35, depth=0.3)
      w.rotation_euler=(0, radians(90), 0); colour(w,(0.05,0.05,0.05))   # wheels on the sides, on the ground

SAFETY (this runs in a LIVE scene with the user's work — violations are rejected):
- Put EVERY created object in the "{coll}" collection (use the part() helper).
- Offset the whole model so it sits around world location X={ox} (add {ox} to each part's X,
  or parent everything to the root empty and set root.location.x = {ox} once).
- NEVER delete/remove/reselect existing objects; no read_homefile, factory_settings,
  open_mainfile, save, quit, os, sys, subprocess, shutil, open(), eval, exec, .clear().

BUILD: think part-by-part about the real object's shape, then compose. Parent all parts to one empty:
  from math import radians
  root = bpy.data.objects.new("{coll}_root", None); coll.objects.link(root)
  ... obj.parent = root for each part.
Wrap everything in a function and call it once. Deterministic. Output ONLY the final Python script.
"""

FORBIDDEN = [
    "read_homefile", "factory_settings", "open_mainfile", "wm.save", "wm.quit",
    "ops.wm.quit", "import os", "import sys", "import subprocess", "import shutil",
    "subprocess", "shutil", "__import__", "eval(", "exec(", "open(",
    "objects.remove", "bpy.data.batch_remove", ".clear()",
    "select_all(action='SELECT')", 'select_all(action="SELECT")', "ops.object.delete",
]


def llm(messages, temperature=0.5, max_tokens=16000):
    body = json.dumps({"model": MODEL, "messages": messages,
                       "temperature": temperature, "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(LLM_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"]


def extract_code(text: str) -> str:
    # reasoning models wrap their thinking in <think>…</think>; keep what follows.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[-1]
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    code = (m.group(1) if m else text).strip()
    return code


def safety_scan(code: str):
    low = code.lower()
    return [tok for tok in FORBIDDEN if tok.lower() in low]


def gen_one(case):
    sys_prompt = SYSTEM.format(coll=case["coll"], ox=case["ox"])
    user = f'Build: {case["desc"]}.\nUse collection "{case["coll"]}" and offset X={case["ox"]}.'
    raw = llm([{"role": "system", "content": sys_prompt},
               {"role": "user", "content": user}])
    code = extract_code(raw)
    viol = safety_scan(code)
    path = OUT / f"{case['name']}.py"
    path.write_text(code, encoding="utf-8")
    return {"name": case["name"], "coll": case["coll"], "script": str(path),
            "chars": len(code), "violations": viol, "has_code": "bpy" in code}


def generate():
    index = []
    for c in CASES:
        print(f"  prompting {MODEL} for '{c['name']}' ...")
        try:
            r = gen_one(c)
        except Exception as e:  # noqa: BLE001
            r = {"name": c["name"], "error": f"{type(e).__name__}: {e}"}
        index.append(r)
        tag = ("ERR " + r["error"]) if "error" in r else \
              (f"{r['chars']} chars, " + ("SAFE" if not r["violations"] else f"VIOLATIONS {r['violations']}"))
        print(f"    -> {tag}")
    (OUT / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\n  scripts + index -> {OUT}")


def repair(name, errfile):
    case = next(c for c in CASES if c["name"] == name)
    code = (OUT / f"{name}.py").read_text(encoding="utf-8")
    err = Path(errfile).read_text(encoding="utf-8")
    sys_prompt = SYSTEM.format(coll=case["coll"], ox=case["ox"])
    user = (f'Build: {case["desc"]}.\nYour previous script errored in Blender 5.1. '
            f'Fix it and output the full corrected script.\n\n=== SCRIPT ===\n{code}\n\n'
            f'=== ERROR ===\n{err}')
    raw = llm([{"role": "system", "content": sys_prompt},
               {"role": "user", "content": user}])
    fixed = extract_code(raw)
    viol = safety_scan(fixed)
    (OUT / f"{name}.py").write_text(fixed, encoding="utf-8")
    print(f"  repaired {name}: {len(fixed)} chars, " +
          ("SAFE" if not viol else f"VIOLATIONS {viol}"))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "repair":
        repair(sys.argv[2], sys.argv[3])
    else:
        generate()
