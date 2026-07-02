#!/usr/bin/env python3
"""
Procedural base-model pipeline: prompt -> {base, params} -> bpy script.

The LLM's ONLY job is to map a plain-English description to a base model + a
parameter dict (reliable even for a small 7B model). The geometry is built by the
authored parametric builders in src/blender/base_models.py, so it's recognisable by
construction. Falls back to deterministic keyword mapping when the LLM is
unavailable, so the pipeline works before the model is on disk.

Usage:
  python procedural_pipeline.py "a jeep with big tires"
  python procedural_pipeline.py "a tall thin robot" --no-llm
  python procedural_pipeline.py "a small biplane" --model qwen2.5-coder-7b-instruct

Emits output/blender/_proc/<slug>.py (a self-contained bpy script) for the agent to
run in the live MCP Blender, plus prints the chosen base + params.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

from src.blender.base_models import (
    BASE_PARAM_SCHEMA, build_base_code, clamp_params, keyword_base, keyword_hints,
)
from vehicle_generator import (
    PARAM_SCHEMA as VEH_PARAM_SCHEMA,
    PLANE_PARAM_SCHEMA as VEH_PLANE_SCHEMA,
    build_detailed_code,
    clamp_params as veh_clamp_params,
    clamp_plane_params as veh_clamp_plane_params,
    keyword_plane_params,
    keyword_vehicle_params,
)

# bases that have a high-detail single-mesh builder in vehicle_generator
DETAILED_BASES = {"car", "plane"}

LLM_URL = "http://localhost:1234/v1/chat/completions"
DEFAULT_MODEL = "qwen2.5-coder-7b-instruct"
OUT = Path("output/blender/_proc").resolve()
OUT.mkdir(parents=True, exist_ok=True)


def _schema_doc() -> str:
    lines = []
    for base, schema in BASE_PARAM_SCHEMA.items():
        knobs = ", ".join(f"{k}[{lo:g}..{hi:g}]" for k, (d, lo, hi) in schema.items())
        lines.append(f"  {base}: {knobs}")
    return "\n".join(lines)


SYSTEM = """Map the description to ONE base + parameters. Output ONLY one JSON object, no prose.
Bases and knobs (ranges):
{schema}
Set only the knobs the description implies; omit the rest. "color":[r,g,b] in 0..1.
Format: {{"base":"<name>","params":{{...}},"color":[r,g,b]}}
Examples:
{{"base":"car","params":{{"wheel_r":0.85,"ride":0.45,"body_h":0.95}},"color":[0.28,0.33,0.17]}}
{{"base":"car","params":{{"body_len":5.3,"cabin_len":0.78,"wheel_pairs":3}},"color":[0.03,0.03,0.03]}}
{{"base":"bike","params":{{"engine":1,"tank":1,"pedals":0}},"color":[0.05,0.05,0.05]}}
{{"base":"humanoid","params":{{"height":2.3,"limb_r":0.06}},"color":[0.6,0.62,0.65]}}
""".format(schema=_schema_doc())


def _resolve_model(requested: str) -> str:
    """Use `requested` if it's actually loaded; otherwise fall back to whatever
    LLM/VLM model LM Studio currently has loaded (quant suffixes like '-imat'
    change the id, so a hard-coded name often misses)."""
    try:
        with urllib.request.urlopen("http://localhost:1234/api/v0/models", timeout=4) as r:
            data = json.load(r)
        loaded = [m["id"] for m in data.get("data", [])
                  if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")]
        if requested in loaded:
            return requested
        if loaded:
            return loaded[0]
    except Exception:  # noqa: BLE001
        pass
    return requested


def llm_json(prompt: str, model: str, system: str | None = None) -> dict | None:
    body = json.dumps({"model": model, "temperature": 0.2, "max_tokens": 1200,
                       "messages": [{"role": "system", "content": system or SYSTEM},
                                    {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        txt = json.load(r)["choices"][0]["message"]["content"]
    if "</think>" in txt:
        txt = txt.rsplit("</think>", 1)[-1]
    s, e = txt.find("{"), txt.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(txt[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


def map_prompt(prompt: str, use_llm: bool = True, model: str = DEFAULT_MODEL) -> dict:
    """prompt -> {base, params(clamped, incl color), source}. LLM with deterministic
    keyword fallback."""
    if use_llm:
        try:
            j = llm_json(prompt, _resolve_model(model))
            if j and j.get("base") in BASE_PARAM_SCHEMA:
                raw = dict(j.get("params") or {})
                if "color" in j:
                    raw["color"] = j["color"]
                return {"base": j["base"], "params": clamp_params(j["base"], raw),
                        "source": "llm"}
        except Exception as exc:  # noqa: BLE001 - any LLM/transport failure -> fallback
            print(f"  [llm unavailable: {type(exc).__name__}: {exc}] -> keyword fallback")
    base = keyword_base(prompt) or "car"
    return {"base": base, "params": clamp_params(base, keyword_hints(prompt, base)),
            "source": "fallback"}


# ── detailed-vehicle backend (high-detail single mesh via vehicle_generator) ──

def _veh_schema_doc() -> str:
    return ", ".join(f"{k}[{lo:g}..{hi:g}]" for k, (d, lo, hi) in VEH_PARAM_SCHEMA.items())


VEH_SYSTEM = """Map the vehicle description to parameters for a detailed car mesh. Output ONLY one JSON object, no prose.
Knobs (ranges): {knobs}
Guidance: taller body_h + bigger wheel_r => SUV/truck/lifted; low body_h + low cabin_h + high rake => sports car; short cabin_len + forward cabin_pos => pickup truck. "matte" finish => metallic 0, roughness ~0.85, clearcoat 0; "chrome/glossy" => metallic 1, roughness ~0.12. "color":[r,g,b] in 0..1.
Format: {{"params":{{...}},"color":[r,g,b]}}
Examples:
{{"params":{{"body_h":1.05,"wheel_r":0.82,"cabin_len":0.4,"cabin_pos":0.18,"metallic":0,"roughness":0.85,"clearcoat":0}},"color":[0.02,0.02,0.02]}}
{{"params":{{"body_h":0.58,"cabin_h":0.4,"rake":0.42,"wheel_r":0.4,"wheel_w":0.5}},"color":[0.7,0.04,0.04]}}
""".format(knobs=_veh_schema_doc())


def map_vehicle_prompt(prompt: str, use_llm: bool = True, model: str = DEFAULT_MODEL) -> dict:
    """prompt -> {params(clamped, incl color/finish), source} for the detailed
    vehicle mesh. LLM with deterministic keyword fallback (same shape as map_prompt)."""
    if use_llm:
        try:
            j = llm_json(prompt, _resolve_model(model), system=VEH_SYSTEM)
            if j is not None:
                raw = dict(j.get("params") or {})
                if "color" in j:
                    raw["color"] = j["color"]
                return {"params": veh_clamp_params(raw), "source": "llm"}
        except Exception as exc:  # noqa: BLE001 - any LLM/transport failure -> fallback
            print(f"  [llm unavailable: {type(exc).__name__}: {exc}] -> keyword fallback")
    return {"params": veh_clamp_params(keyword_vehicle_params(prompt)), "source": "fallback"}


# ── detailed-aircraft mapping ──

def _plane_schema_doc() -> str:
    return ", ".join(f"{k}[{lo:g}..{hi:g}]" for k, (d, lo, hi) in VEH_PLANE_SCHEMA.items())


PLANE_SYSTEM = """Map the aircraft description to parameters for a detailed plane mesh. Output ONLY one JSON object, no prose.
Knobs (ranges): {knobs}
Guidance: "jet/fighter/airliner" => jets 1; "prop/light/cessna/glider" => jets 0; glider => big wing_span, gear 0; longer fus_len + wider fus_w + bigger wing_span => airliner. "color":[r,g,b] in 0..1.
Format: {{"params":{{...}},"color":[r,g,b]}}
Examples:
{{"params":{{"jets":1,"fus_len":8,"fus_w":1.45,"wing_span":10.5,"wing_chord":2.2}},"color":[0.9,0.9,0.9]}}
{{"params":{{"jets":0,"fus_len":3.6,"wing_span":5.0}},"color":[0.6,0.04,0.04]}}
""".format(knobs=_plane_schema_doc())


def map_plane_prompt(prompt: str, use_llm: bool = True, model: str = DEFAULT_MODEL) -> dict:
    """prompt -> {params(clamped, incl color/finish), source} for the detailed plane."""
    if use_llm:
        try:
            j = llm_json(prompt, _resolve_model(model), system=PLANE_SYSTEM)
            if j is not None:
                raw = dict(j.get("params") or {})
                if "color" in j:
                    raw["color"] = j["color"]
                return {"params": veh_clamp_plane_params(raw), "source": "llm"}
        except Exception as exc:  # noqa: BLE001 - any LLM/transport failure -> fallback
            print(f"  [llm unavailable: {type(exc).__name__}: {exc}] -> keyword fallback")
    return {"params": veh_clamp_plane_params(keyword_plane_params(prompt)), "source": "fallback"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:32] or "model"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="+")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--ox", type=float, default=0.0)
    ap.add_argument("--rig", action="store_true", help="build the skeleton + skin-bind (game-functional)")
    ap.add_argument("--demo", action="store_true", help="pose the rig (spin wheels / steer)")
    ap.add_argument("--detailed", action="store_true",
                    help="high-detail single-mesh vehicle backend (vehicle_generator); vehicles only")
    a = ap.parse_args()
    prompt = " ".join(a.prompt)

    m = map_prompt(prompt, use_llm=not a.no_llm, model=a.model)
    slug = _slug(prompt)
    coll = "PROC_" + slug

    # Pick the backend. --detailed routes cars/planes to the high-detail bmesh
    # builder; bases without a detailed builder (humanoid/bike) gracefully fall
    # back to the standard parametric/rigged base_models path.
    use_detailed = a.detailed and m["base"] in DETAILED_BASES
    if a.detailed and not use_detailed:
        print(f"  [--detailed has no builder for base '{m['base']}' -> standard backend]")

    if use_detailed:
        kind = m["base"]   # "car" or "plane"
        if kind == "plane":
            vm = map_plane_prompt(prompt, use_llm=not a.no_llm, model=a.model)
        else:
            vm = map_vehicle_prompt(prompt, use_llm=not a.no_llm, model=a.model)
        code = build_detailed_code(vm["params"], coll=coll, ox=a.ox, out_dir=str(OUT),
                                   name=coll, seed_str=slug, rig=a.rig, demo=a.demo, kind=kind)
        backend, source, params_out, base_out = "detailed_" + kind, vm["source"], vm["params"], kind
    else:
        code = build_base_code(m["base"], m["params"], coll=coll, ox=a.ox, out_dir=str(OUT),
                               rig=a.rig, demo=a.demo)
        backend, source, params_out, base_out = "base_model", m["source"], m["params"], m["base"]

    script = OUT / f"{slug}.py"
    script.write_text(code, encoding="utf-8")

    print(f"prompt : {prompt}")
    print(f"backend: {backend}")
    print(f"source : {source}")
    print(f"base   : {base_out}")
    print(f"params : {json.dumps(params_out)}")
    print(f"script : {script}")
    print(f"coll   : {coll}")
    (OUT / f"{slug}.plan.json").write_text(
        json.dumps({"prompt": prompt, "backend": backend, "base": base_out,
                    "source": source, "params": params_out,
                    "script": str(script), "coll": coll}, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    main()
