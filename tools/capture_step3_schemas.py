"""
One-shot capture of REAL Step-3 schema-fill output for the four OOD prompts,
using the concepts already captured by tools/capture_step2_concepts.py.

Reproduces unity_pipeline.run()'s Step 3 exactly — same schema_hints assembly
(theme hints + intent.structure_hint() + asset vocabulary), same
render_multimodal_block gating, same system/user templates, same
schema_temperature and thinking=False, same extract_json -> UnitySchema
validation, then the same apply_intent() override pass.

Stops at the end of Step 3. Steps 4-6 are not run.

Deliberate deviation from a default run: seed pinned (see capture_step2_concepts).

Writes tools/out/step3_schemas.json incrementally.

Run:  python tools/capture_step3_schemas.py
Needs LM Studio up with the config.yaml model loaded, and step2_concepts.json present.
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

from src.backends import active_chat_cfg, make_chat_client  # noqa: E402
from src.unity_archetypes import apply_intent, classify  # noqa: E402
from src.unity_asset_catalog import asset_vocabulary  # noqa: E402
from src.unity_prompts import (  # noqa: E402
    UNITY_SCHEMA_FILL_SYSTEM,
    UNITY_SCHEMA_FILL_USER,
    render_multimodal_block,
    brief_block,
)
from src.unity_schema import UnitySchema  # noqa: E402

CONCEPTS_PATH = _REPO_ROOT / "tools" / "out" / "step2_concepts.json"
OUT_PATH = _REPO_ROOT / "tools" / "out" / "step3_schemas.json"
SEED = 0


def summarize(schema) -> dict:
    """The fields that decide what actually gets built."""
    entities = []
    for e in getattr(schema, "entities", []) or []:
        entities.append({
            "name": getattr(e, "name", None),
            "kind": getattr(e, "kind", None),
            "model_hint": getattr(e, "model_hint", None),
            "count": getattr(e, "count", None),
        })
    kinds: dict[str, int] = {}
    for e in entities:
        kinds[str(e["kind"])] = kinds.get(str(e["kind"]), 0) + 1
    return {
        "game_type": schema.game_type,
        "locomotion": schema.locomotion,
        "meta_genre": (schema.meta or {}).get("genre"),
        "render_pipeline": schema.render_pipeline,
        "platform": schema.platform,
        "unity_version": schema.unity_version,
        "n_packages": len(schema.packages or []),
        "n_scripts": len(schema.scripts or []),
        "scenes": list(schema.scenes or []),
        "features": list(schema.features or []),
        "entity_kind_counts": kinds,
        "entities": entities,
    }


def main() -> int:
    if not CONCEPTS_PATH.exists():
        print(f"missing {CONCEPTS_PATH} — run tools/capture_step2_concepts.py first")
        return 1
    captured = json.loads(CONCEPTS_PATH.read_text(encoding="utf-8"))

    config = yaml.safe_load((_REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg = active_chat_cfg(config)
    llm = make_chat_client(config)
    schema_temp = cfg.get("schema_temperature", 0.2)

    pipe_cfg = config.get("unity_pipeline", config.get("pipeline", {})) or {}
    lookups_dir = pipe_cfg.get("lookups_dir", "lookups")
    max_optional = int(pipe_cfg.get("multimodal_max_sections", 3))
    vocab = asset_vocabulary(lookups_dir)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "_provenance": {
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": cfg["model"],
            "temperature": schema_temp,
            "max_tokens": cfg["max_tokens"],
            "seed": SEED,
            "thinking": False,
            "multimodal_max_sections": max_optional,
            "lookups_dir": lookups_dir,
            "asset_vocabulary_chars": len(vocab),
            "concepts_from": CONCEPTS_PATH.name,
            "note": "Real Step-3 output. Reproduces unity_pipeline.run() Step 3 "
                    "through apply_intent(); steps 4-6 not run.",
        },
        "schemas": {},
    }

    for pid, rec in captured["concepts"].items():
        prompt, concept = rec["prompt"], rec["concept"]
        intent = classify(prompt)  # pipeline line 808: prompt only

        schema_hints = ""              # theme resolved to none for all four
        schema_hints += intent.structure_hint()
        if vocab:
            schema_hints += "\n" + vocab + "\n"

        mm = render_multimodal_block(concept, prompt, max_optional=max_optional)
        schema_user = UNITY_SCHEMA_FILL_USER.format(
            concept=concept, schema_hints=schema_hints, multimodal_block=mm.text,
            brief_block=brief_block(prompt),
        )

        print(f"[{pid}] sections={','.join(mm.sections)} user={len(schema_user)} chars — "
              f"calling LLM…", flush=True)
        t0 = time.time()
        entry: dict = {
            "prompt": prompt,
            "multimodal_sections": list(mm.sections),
            "schema_hints": schema_hints,
            "user_message_chars": len(schema_user),
        }
        try:
            raw = llm.chat(
                system=UNITY_SCHEMA_FILL_SYSTEM,
                user=schema_user,
                temperature=schema_temp,
                thinking=False,
                seed=SEED,
            )
            entry["raw_chars"] = len(raw)
            schema_dict = llm.extract_json(raw)
            schema = UnitySchema.model_validate(schema_dict)
            # pipeline line 871 — deterministic override pass
            fix_msgs = apply_intent(schema, intent)
            entry["validated"] = True
            entry["apply_intent_msgs"] = fix_msgs
            entry["summary"] = summarize(schema)
            entry["schema"] = schema.model_dump(mode="json")
            print(f"[{pid}] OK  game_type={schema.game_type} locomotion={schema.locomotion} "
                  f"genre={(schema.meta or {}).get('genre')} "
                  f"kinds={entry['summary']['entity_kind_counts']}  "
                  f"({time.time() - t0:.1f}s)", flush=True)
        except Exception as exc:
            entry["validated"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()
            entry["raw"] = locals().get("raw", "")
            print(f"[{pid}] FAILED {type(exc).__name__}: {exc}  "
                  f"({time.time() - t0:.1f}s)", flush=True)

        entry["elapsed_s"] = round(time.time() - t0, 1)
        payload["schemas"][pid] = entry
        OUT_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True, default=str) + "\n",
            encoding="utf-8", newline="\n",
        )

    print(f"\nwrote {OUT_PATH.relative_to(_REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
