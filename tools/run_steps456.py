"""
Run Steps 4-6 on the three OOD schemas captured by tools/capture_step3_schemas.py.

Rather than re-implementing the 4-6 stretch of unity_pipeline.run() (resolver,
fixer, entity rescue, racing scaffold, path normalization, layout, capability/
microgen resolution, generator), this drives the REAL run() and intercepts only
the two LLM steps: the Step-2 chat returns the captured concept, the Step-3 chat
returns the captured schema. Everything from Step 4 on executes verbatim.

The interception is an instance-attribute shadow on the client object. Nothing
in src/ is edited.

Flags are run() defaults: compose=False (so the micro_code LLM authoring path is
not entered), blender_models=False, no route. seed=0, matching the captures.
Step 6's translate_concept() LLM call is left live — it is genuine Step-6
behaviour.

Writes tools/out/steps456_results.json.

Run:  python tools/run_steps456.py
Needs LM Studio up (Step 6 makes a live call) and step3_schemas.json present.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.unity_pipeline import UnityPipeline  # noqa: E402
from src.unity_prompts import (  # noqa: E402
    UNITY_CONCEPT_SYSTEM,
    UNITY_SCHEMA_FILL_SYSTEM,
)

STEP2_PATH = _REPO_ROOT / "tools" / "out" / "step2_concepts.json"
STEP3_PATH = _REPO_ROOT / "tools" / "out" / "step3_schemas.json"
OUT_PATH = _REPO_ROOT / "tools" / "out" / "steps456_results.json"
SEED = 0


def install_replay(pipeline: UnityPipeline, concept: str, schema_json: str) -> dict:
    """Shadow .chat on the client instance: replay steps 2-3, pass everything else through."""
    client = pipeline.llm
    real_chat = client.chat
    calls = {"concept": 0, "schema": 0, "passthrough": 0}

    def chat(system: str, user: str, temperature=None, thinking: bool = True, seed=None) -> str:
        if system == UNITY_CONCEPT_SYSTEM:
            calls["concept"] += 1
            return concept
        if system == UNITY_SCHEMA_FILL_SYSTEM:
            calls["schema"] += 1
            return schema_json
        calls["passthrough"] += 1
        return real_chat(system=system, user=user, temperature=temperature,
                         thinking=thinking, seed=seed)

    client.chat = chat  # instance attribute shadows the bound method
    return calls


def tree_stats(root: Path) -> dict:
    files = [p for p in root.rglob("*") if p.is_file()]
    by_ext: dict[str, int] = {}
    for p in files:
        by_ext[p.suffix.lower() or "<none>"] = by_ext.get(p.suffix.lower() or "<none>", 0) + 1
    return {
        "n_files": len(files),
        "total_kb": round(sum(p.stat().st_size for p in files) / 1024, 1),
        "by_ext": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])),
        "cs_scripts": sorted(p.name for p in files if p.suffix.lower() == ".cs"),
        "scenes": sorted(p.name for p in files if p.suffix.lower() == ".unity"),
        "models": sorted(p.name for p in files if p.suffix.lower() in (".fbx", ".obj", ".gltf", ".glb")),
    }


def main() -> int:
    for p in (STEP2_PATH, STEP3_PATH):
        if not p.exists():
            print(f"missing {p}")
            return 1
    concepts = json.loads(STEP2_PATH.read_text(encoding="utf-8"))["concepts"]
    schemas = json.loads(STEP3_PATH.read_text(encoding="utf-8"))["schemas"]
    config = yaml.safe_load((_REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))

    targets = [pid for pid, r in schemas.items() if r.get("validated")]
    print(f"running steps 4-6 for: {', '.join(targets)}\n")

    payload: dict = {
        "_provenance": {
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seed": SEED,
            "run_flags": {"compose": False, "blender_models": False, "route": None,
                          "theme": None, "micro_code": "unused (compose=False)"},
            "unity_output_dir": config["unity_pipeline"]["unity_output_dir"],
            "note": "Real unity_pipeline.run(); steps 2-3 replayed from captures, "
                    "steps 4-6 executed verbatim.",
        },
        "runs": {},
    }

    for pid in targets:
        prompt = schemas[pid]["prompt"]
        concept = concepts[pid]["concept"]
        schema_json = json.dumps(schemas[pid]["schema"])

        print(f"\n{'=' * 70}\n[{pid}] {prompt}\n{'=' * 70}", flush=True)
        entry: dict = {"prompt": prompt}
        t0 = time.time()
        try:
            pipeline = UnityPipeline(config)
            calls = install_replay(pipeline, concept, schema_json)
            out_path = pipeline.run(user_prompt=prompt, seed=SEED)
            entry["ok"] = True
            entry["project_dir"] = str(out_path)
            entry["llm_calls"] = dict(calls)
            entry["tree"] = tree_stats(Path(out_path)) if Path(out_path).exists() else None
            print(f"[{pid}] OK -> {out_path}", flush=True)
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()
            print(f"[{pid}] FAILED {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()

        entry["elapsed_s"] = round(time.time() - t0, 1)
        payload["runs"][pid] = entry
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True, default=str) + "\n",
            encoding="utf-8", newline="\n",
        )

    print(f"\nwrote {OUT_PATH.relative_to(_REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
