"""Per-call completion budgets for the Pipeline LLM's small structured calls.

`lm_studio.max_tokens` in config.yaml (88,000 today) is the Step 3 schema-fill
budget. A small Step 3.5 call that inherits it has no ceiling worth the name.
On 2026-11-14 one NPC-dialog call (12 lines, about 700 tokens when healthy)
generated about 76,000 tokens over 53 minutes at temperature 0 before it was
stopped by hand. The micro-code path learned the same lesson earlier (1h54m
burned on one subsystem), which is why it already had a cap of its own.

Every small, structured Step 3.5 call takes its `max_tokens` from here, so the
caps live in one place. A `Budget` is a fixed overhead plus a per-item allowance
times the item count the caller asks for, clamped to [floor, ceiling]. The
sizes are roughly 3-6x the completions measured in the LM Studio server logs of
2026-11-12..14 (n = 3-23 replies per call type; none was cut off), so a healthy
reply is never truncated and a degenerate one fails in minutes, not an hour.

A reply the server cut off at its cap (`finish_reason == "length"`) is never
parsed as complete: `capped_chat` logs it loudly and returns None, and each
caller already treats None/empty as "this attempt failed".

Budgets bound generation length only. They never change what a healthy reply
says, so Step 3.5 output and the deterministic Steps 4-7 are unaffected.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Budget:
    """`overhead + per_item * items`, clamped to `[floor, ceiling]`."""

    overhead: int
    per_item: int
    floor: int
    ceiling: int

    def tokens(self, items: int) -> int:
        count = max(int(items or 0), 0)
        return max(self.floor, min(self.ceiling, self.overhead + self.per_item * count))


# Measured completion tokens (median / max at the usual count) -> budget there.
# NPC dialog, 12 lines: 702 / 784 -> 2,320.
DIALOG_LINES = Budget(overhead=400, per_item=160, floor=1200, ceiling=6000)
# Diegetic audio cues, 10 cues: 315 / 389 -> 1,300.
AUDIO_CUES = Budget(overhead=300, per_item=100, floor=800, ceiling=4000)
# One SA3 prompt sentence per cue (no live samples yet; sized like a cue).
AUDIO_PROMPTS = Budget(overhead=300, per_item=120, floor=800, ceiling=6000)
# Concept Sketchfab collection, 12 briefs: 194 / 217 -> 1,260.
MODEL_BRIEFS = Budget(overhead=300, per_item=80, floor=800, ceiling=4000)
# Per-entity Sketchfab queries, ~15-20 entities: 228 / 274 -> 1,200-1,500.
ENTITY_QUERIES = Budget(overhead=300, per_item=60, floor=800, ceiling=6000)

# Vision candidate pick, {"pick": n, "reason": "..."}: 39 / 48.
VISION_PICK_TOKENS = 600
# Micro-code / custom-script C# bodies (src/unity_micro_code.py): a JSON of a few
# fields and methods. Unchanged from that module's original cap.
MICRO_CODE_TOKENS = 6000

# `extract_dialog_lines(count=0)` asks for the system prompt's default 3-6 lines.
DIALOG_DEFAULT_LINES = 6


def reply_truncated(llm: Any) -> bool:
    """True when the client reports the last reply was cut off at max_tokens."""
    return getattr(llm, "last_finish_reason", None) == "length"


def capped_chat(llm: Any, *, purpose: str, max_tokens: int, **kwargs: Any) -> Optional[str]:
    """`llm.chat(..., max_tokens=max_tokens)`, or None when the reply was cut off.

    A cut-off reply is discarded loudly rather than handed to a parser that
    might salvage part of it. Exceptions from `llm.chat` propagate unchanged,
    so every caller keeps its own failure handling.
    """
    raw = llm.chat(max_tokens=max_tokens, **kwargs)
    if reply_truncated(llm):
        log.warning(
            "[llm.budget] %s: reply cut off at its %d-token cap "
            "(finish_reason=length) and discarded, never parsed as complete. "
            "A healthy reply is several times smaller, so this is usually a "
            "runaway generation.",
            purpose, max_tokens,
        )
        return None
    return raw
