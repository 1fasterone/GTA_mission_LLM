"""Per-call completion budgets for the Pipeline LLM's structured calls.

`lm_studio.max_tokens` in config.yaml (88,000 today) is the client-wide
default: a `chat()` that passes no `max_tokens` inherits it. That is no ceiling
worth the name. 88,000 is above LM Studio's loaded context (47,360, see
`lms ps`), so a runaway reply stops only at the context wall.

Two live runaways set the caps here:

- 2026-11-14, Step 3.5: one NPC-dialog call (12 lines, about 700 tokens when
  healthy) generated about 76,000 tokens over 53 minutes at temperature 0
  before it was stopped by hand. The micro-code path learned the same lesson
  earlier (1h54m burned on one subsystem), which is why it already had a cap.
- 2026-11-17, Step 3: the Unity schema fill of neon E2E run 03 looped for 21
  minutes before it was killed by hand. It wrote 118 KB and 235 `scripts`
  entries. 193 of them repeated earlier entries, almost all from a cycle of 9
  `...Manager` scripts, and the reply never reached `missions`.

Every structured call takes its `max_tokens` from here, so the caps live in
one place. A `Budget` is a fixed overhead plus a per-item allowance times the
item count the caller asks for, clamped to [floor, ceiling]. The sizes are
roughly 3-6x the completions measured in the LM Studio server logs (none of
the measured replies was cut off), so a healthy reply is never truncated and a
degenerate one fails within a bounded time instead of running to the context
wall.

A reply the server cut off at its cap (`finish_reason == "length"`) is never
parsed as complete. `capped_chat` logs it loudly and returns None, and each
Step 3.5 caller already treats None/empty as "this attempt failed". The Unity
schema fill raises `ReplyTruncatedError` instead, so the cut-off reply is
recorded as a failed attempt and never reaches `extract_json`'s
bracket-closing repair.

Budgets bound generation length only. They never change what a healthy reply
says, so Step 3/3.5 output and the deterministic Steps 4-7 are unaffected.
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

# Step 3 Unity schema fill (src/unity_pipeline.py). Measured: 13 healthy fills in
# the LM Studio logs of 2026-11-14 and 2026-11-17 (qwen3.5-9b, all
# finish_reason=stop, 4.4-6.6 min each): median 7,993 / max 10,067 completion
# tokens. Every November log gives n=21, median 7,123, the same max of 10,067.
# About 3x that max -> 30,000. The largest measured prompt was 9,948 tokens, so
# prompt plus cap (39,948) still fits the 47,360-token loaded context, and the
# cap is reached before the context wall.
SCHEMA_FILL_HEALTHY_MAX_TOKENS = 10_067
SCHEMA_FILL_TOKENS = 30_000
# The visual compile's own fill cap, older than SCHEMA_FILL_TOKENS. Its
# cardinality is closed, so it is never allowed to exceed this.
VISUAL_COMPILE_FILL_TOKENS = 32_000

# Step 2 Unity concept (src/unity_pipeline.py), the one prose call and the one
# that reasons. Until this cap it passed no `max_tokens`, so it inherited the
# client-wide lm_studio.max_tokens (88,000 in the E2E config): a reasoning loop
# there stops only at the context wall. Measured: every Unity concept reply in
# the LM Studio server logs of 2026-10-04 .. 2026-11-19 (qwen3.5-9b, n=64, all
# finish_reason=stop): median 3,300 / max 8,302 completion tokens, reasoning
# included (reasoning is most of each reply). The largest prompt was 2,348
# tokens. 12,000 is 1.45x that max and 3.6x the median; at about 25 tok/s a
# runaway ends after roughly 8 minutes instead of running to the wall.
CONCEPT_HEALTHY_MAX_TOKENS = 8_302
CONCEPT_TOKENS = 12_000


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def schema_fill_tokens(*, visual_compile: bool, configured: Any = None) -> int:
    """The Step 3 fill's `max_tokens`: the smallest cap that applies.

    `SCHEMA_FILL_TOKENS` always applies. A visual compile also gets
    `VISUAL_COMPILE_FILL_TOKENS`. `configured` is the client-wide
    `lm_studio.max_tokens`, so a config that asks for less keeps it. A
    measured budget lowers the client default and never raises it. A
    `configured` value that is not a positive int (absent, or a test double's
    attribute) adds no cap.
    """
    caps = [SCHEMA_FILL_TOKENS]
    if visual_compile:
        caps.append(VISUAL_COMPILE_FILL_TOKENS)
    client_cap = _positive_int(configured)
    if client_cap is not None:
        caps.append(client_cap)
    return min(caps)


class ReplyTruncatedError(ValueError):
    """The server cut a reply off at its cap (`finish_reason == "length"`).

    A `ValueError`, so a caller's existing "this attempt failed" handling
    catches it, and that caller retries. The reply is never parsed or
    repaired. A cut-off reply that `extract_json` bracket-closes can still
    validate as a schema with whole sections missing.
    """

    def __init__(self, purpose: str, max_tokens: int, chars: int, remedy: str):
        self.purpose = purpose
        self.max_tokens = max_tokens
        self.chars = chars
        super().__init__(
            f"{purpose} reply cut off at its {max_tokens:,}-token cap "
            f"(finish_reason=length) after {chars:,} chars; discarded unparsed. "
            f"{remedy}"
        )


def schema_fill_truncated(max_tokens: int, chars: int) -> ReplyTruncatedError:
    """The error for a Step 3 fill reply cut off at `max_tokens`.

    The remedy depends on where the cap came from. A cap below the measured
    budget came from the config, and the remedy is to raise the config to the
    budget, which the fill never exceeds. At the budget, the reply was about
    three times longer than any healthy fill measured, so it is a runaway.
    Raising the cap only makes the loop longer.
    """
    if max_tokens < SCHEMA_FILL_TOKENS:
        remedy = (
            f"The cap came from lm_studio.max_tokens, which is below the "
            f"measured Step 3 budget of {SCHEMA_FILL_TOKENS:,} (healthy fills "
            f"measure up to {SCHEMA_FILL_HEALTHY_MAX_TOKENS:,} tokens). Raise "
            f"lm_studio.max_tokens to {SCHEMA_FILL_TOKENS:,}. The fill never "
            f"uses more than that."
        )
    else:
        remedy = (
            f"Healthy fills measure at most {SCHEMA_FILL_HEALTHY_MAX_TOKENS:,} "
            f"tokens, so this is likely a runaway generation (usually a list "
            f"repeating itself). Raising the cap only lengthens the loop. "
            f"Re-run, or tighten the brief."
        )
    return ReplyTruncatedError("Step 3 schema fill", max_tokens, chars, remedy)


def concept_tokens(*, configured: Any = None) -> int:
    """The Step 2 concept's `max_tokens`: `CONCEPT_TOKENS`, or less if the config asks.

    Same rule as `schema_fill_tokens`: a measured budget lowers the
    client-wide `lm_studio.max_tokens` and never raises it, and a `configured`
    value that is not a positive int adds no cap.
    """
    caps = [CONCEPT_TOKENS]
    client_cap = _positive_int(configured)
    if client_cap is not None:
        caps.append(client_cap)
    return min(caps)


def concept_truncated(max_tokens: int, chars: int) -> ReplyTruncatedError:
    """The error for a Step 2 concept reply cut off at `max_tokens`.

    A cut-off concept is never handed to Step 3: a brief that stops
    mid-sentence, or one that is empty because the cap landed inside the
    reasoning, would be filled as if it were whole. As with the schema fill,
    the remedy depends on where the cap came from.
    """
    if max_tokens < CONCEPT_TOKENS:
        remedy = (
            f"The cap came from lm_studio.max_tokens, which is below the "
            f"measured Step 2 budget of {CONCEPT_TOKENS:,} (healthy concepts "
            f"measure up to {CONCEPT_HEALTHY_MAX_TOKENS:,} tokens, reasoning "
            f"included). Raise lm_studio.max_tokens to {CONCEPT_TOKENS:,}."
        )
    else:
        remedy = (
            f"Healthy concepts measure at most {CONCEPT_HEALTHY_MAX_TOKENS:,} "
            f"tokens, reasoning included, so this is likely a runaway "
            f"(usually reasoning that loops). Raising the cap only lengthens "
            f"the loop. Re-run, or pass --no-think to skip Step 2 reasoning."
        )
    return ReplyTruncatedError("Step 2 concept", max_tokens, chars, remedy)


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
