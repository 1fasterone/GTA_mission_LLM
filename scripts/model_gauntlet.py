"""
Model robustness gauntlet for the Unity micro-generators.

Runs a representative spread of micro-generators (the surfaces that have actually
broken on real models: enum fields, nested object lists, LLM-authored code
fields, chatty/refusing schema steps) against ONE model, and reports per-case
pass/fail plus output cleanliness. Use it to vet any model a user might bring
before trusting it with the pipeline.

Usage:
    python scripts/model_gauntlet.py --model "Meta-Llama-3.1-8B-Instruct"
    python scripts/model_gauntlet.py --model qwen3.5-9b --kinds hud,spawner,saveload
    python scripts/model_gauntlet.py --model X --retries 1   # retry no-JSON/validation once

It overrides only `lm_studio.model` from your base config, so the LM Studio
server must already have that model loaded (the id must match `/v1/models`).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.micro import REGISTRY  # noqa: E402

# (kind, prompt, variant) — chosen to exercise the breakage-prone surfaces.
DEFAULT_CASES = [
    ("hud",        "oxygen meter that drains while underwater",        "health_bar"),
    ("hud",        "full game hud with health, ammo and a minimap",    "full_hud_bundle"),
    ("spawner",    "zombie wave every 20s in a ring, ramping",         "wave_spawner"),
    ("spawner",    "pooled bullet emitter for a boss",                 "object_pool"),
    ("saveload",   "checkpoint that saves position and respawns",      "checkpoint"),
    ("saveload",   "three save slots for an RPG with level and gold",  "save_slots"),
    ("inventory",  "5x4 grid backpack, potions stack",                 "grid"),
    ("inventory",  "equipment slots: head, chest, weapon, offhand",    "equipment"),
    ("dialogue",   "guard challenges the player with branching replies", "branching"),
    ("menu",       "main menu with play, settings, quit",              "main_menu"),
    ("interactable", "a chest you open with E",                        "pickup"),
    ("transition", "fade to the next level",                           "fade"),
    ("health",     "player health with regenerating shields",          "shielded"),
    ("ai",         "guard that patrols then chases the player",        "fsm_guard"),
    ("controller", "third person with double jump and dash",           "third_person"),
]

_BAD_OUTPUT = re.compile(r"\{\{|\{%|UNRESOLVED")


def _scan_outputs(out_dir: Path) -> list[str]:
    issues = []
    for cs in out_dir.rglob("*.cs"):
        text = cs.read_text(encoding="utf-8", errors="replace")
        if _BAD_OUTPUT.search(text):
            issues.append(cs.name)
        ob, cb = text.count("{"), text.count("}")
        if ob != cb:
            issues.append(f"{cs.name} (braces {ob}/{cb})")
    return issues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="Model id as loaded in LM Studio")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--kinds", default="", help="Comma list to filter (default: all in the suite)")
    ap.add_argument("--retries", type=int, default=0, help="Retry a failing case N times")
    args = ap.parse_args()

    cfg = yaml.safe_load((REPO / args.config).read_text(encoding="utf-8"))
    cfg.setdefault("lm_studio", {})["model"] = args.model

    cases = DEFAULT_CASES
    if args.kinds:
        keep = {k.strip() for k in args.kinds.split(",")}
        cases = [c for c in cases if c[0] in keep]

    print(f"\n=== GAUNTLET: model={args.model}  cases={len(cases)} ===\n")
    results = []
    for kind, prompt, variant in cases:
        label = f"{kind}/{variant}"
        pipeline = REGISTRY[kind](cfg)
        last_err = None
        for attempt in range(args.retries + 1):
            try:
                out_dir = pipeline.run(prompt, variant=variant)
                issues = _scan_outputs(Path(out_dir))
                if issues:
                    results.append((label, "DIRTY", ", ".join(issues)))
                else:
                    results.append((label, "OK", ""))
                last_err = None
                break
            except Exception as e:  # noqa: BLE001 — the whole point is to catalogue failures
                last_err = f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
        if last_err is not None:
            results.append((label, "FAIL", last_err))

    print(f"\n\n=== SUMMARY: {args.model} ===")
    width = max(len(r[0]) for r in results)
    ok = 0
    for label, status, detail in results:
        if status == "OK":
            ok += 1
        print(f"  {label:<{width}}  {status:<5} {detail}")
    print(f"\n  {ok}/{len(results)} clean\n")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
