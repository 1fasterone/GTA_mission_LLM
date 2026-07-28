"""
One-shot capture of REAL Step-2 concept output for the four OOD prompts, so
tools/diag_archetype_scores.py can score the 1x concept pass against genuine
pipeline text instead of hand-written stand-ins.

This reproduces unity_pipeline.run()'s Step 2 exactly — same theme resolution,
same intent concept_hint(), same system/user templates, same client and
concept_temperature. Nothing in src/ is imported for anything but calling it.

Deliberate deviation from a default run: seed is pinned (lm_studio.seed is a
supported config key, normally unset -> server picks at random) so the captured
text is re-derivable rather than a one-off sample.

Writes tools/out/step2_concepts.json incrementally, one prompt at a time.

Run:  python tools/capture_step2_concepts.py
Needs LM Studio up with the config.yaml model loaded.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.backends import active_chat_cfg, make_chat_client  # noqa: E402
from src.themes import render_concept_bias, resolve_theme  # noqa: E402
from src.unity_archetypes import classify  # noqa: E402
from src.unity_pipeline import _is_structured_brief  # noqa: E402
from src.unity_prompts import UNITY_CONCEPT_SYSTEM, UNITY_CONCEPT_USER  # noqa: E402

OUT_PATH = _REPO_ROOT / "tools" / "out" / "step2_concepts.json"
SEED = 0

OOD_PROMPTS = [
    ("ood_ring", "make a game where the player throws rings onto a box"),
    ("ood_stack", "a game about stacking plates without them falling"),
    ("ood_sort", "sort falling colored blocks into matching bins"),
    ("ood_balance", "balance a tray of drinks while walking across a room"),
]


def theme_block_for(user_prompt: str) -> tuple[str, str | None]:
    """unity_pipeline.run() lines ~783-809, verbatim in behaviour."""
    theme = "none" if _is_structured_brief(user_prompt) else None
    active_theme, rng, _meta = resolve_theme(
        user_prompt=user_prompt, theme_name=theme, mashup_with=None, seed=SEED,
    )
    block = ""
    if active_theme:
        block, _twist = render_concept_bias(active_theme, rng=rng)
    block += classify(user_prompt).concept_hint()
    return block, getattr(active_theme, "display", None)


def main() -> int:
    config = yaml.safe_load((_REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg = active_chat_cfg(config)
    llm = make_chat_client(config)
    concept_temp = cfg.get("concept_temperature", 0.85)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "_provenance": {
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "backend": cfg.get("backend", config.get("lm_studio", {}).get("backend")),
            "model": cfg["model"],
            "temperature": concept_temp,
            "max_tokens": cfg["max_tokens"],
            "seed": SEED,
            "thinking": True,
            "system_prompt": UNITY_CONCEPT_SYSTEM,
            "note": "Real Step-2 output. Reproduces unity_pipeline.run() Step 2; "
                    "seed pinned for re-derivability.",
        },
        "concepts": {},
    }

    for pid, prompt in OOD_PROMPTS:
        block, theme_name = theme_block_for(prompt)
        user = UNITY_CONCEPT_USER.format(user_prompt=prompt, theme_block=block)
        print(f"[{pid}] theme={theme_name!r} theme_block={len(block)} chars — calling LLM…",
              flush=True)
        t0 = time.time()
        concept = llm.chat(
            system=UNITY_CONCEPT_SYSTEM,
            user=user,
            temperature=concept_temp,
            thinking=True,
            seed=SEED,
        )
        dt = time.time() - t0
        print(f"[{pid}] {len(concept)} chars in {dt:.1f}s", flush=True)

        payload["concepts"][pid] = {
            "prompt": prompt,
            "theme": theme_name,
            "theme_block": block,
            "user_message": user,
            "concept": concept,
            "elapsed_s": round(dt, 1),
        }
        OUT_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8", newline="\n",
        )

    print(f"\nwrote {OUT_PATH.relative_to(_REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
