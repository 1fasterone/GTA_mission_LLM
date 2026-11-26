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
Step 3.5 caller already treats None/empty as "this attempt failed". The three
Step 3 schema fills (Unity, GTA5 mission, micro) each have a `FillBudget` and
raise `ReplyTruncatedError` instead, so the cut-off reply is a failed attempt
that never reaches `extract_json`'s bracket-closing repair. That repair is not
hypothetical: a GTA5 fill and an NPC-dialog micro fill cut off in July 2026
both validate as schemas with whole sections missing.

The Step 2 concept is the one call per pipeline that reasons, so a reasoning
loop there also ran to the context wall. The GTA5 and micro concepts each have
a `ConceptBudget`, and a concept cut off at its cap raises
`ReplyTruncatedError` too: a brief that stops mid-sentence, or one left empty
because the cap landed inside the reasoning, is never filled as if it were
whole.

Budgets bound generation length only. They never change what a healthy reply
says, so Step 2/3/3.5 output and the deterministic Steps 4-7 are unaffected.
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


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


@dataclass(frozen=True)
class _CallBudget:
    """One Pipeline LLM call's completion cap, sized from its healthy replies.

    `purpose` names the call in a `ReplyTruncatedError`. `healthy_max` is the
    largest completion measured for the call in the LM Studio server logs.
    `tokens` is the cap sized from it.
    """

    purpose: str
    tokens: int
    healthy_max: int

    def cap(self, *, configured: Any = None, extra: tuple[int, ...] = ()) -> int:
        """The call's `max_tokens`: the smallest cap that applies.

        `tokens` always applies, and so does each value in `extra` (the visual
        compile's own fill cap). `configured` is the client-wide
        `lm_studio.max_tokens`, so a config that asks for less keeps it. A
        measured budget lowers the client default and never raises it. A
        `configured` value that is not a positive int (absent, or a test
        double's attribute) adds no cap.
        """
        caps = [self.tokens, *extra]
        client_cap = _positive_int(configured)
        if client_cap is not None:
            caps.append(client_cap)
        return min(caps)


@dataclass(frozen=True)
class FillBudget(_CallBudget):
    """One Step 3 schema fill's completion cap, sized from its healthy fills.

    `healthy_max` is the largest completion measured for this fill with
    reasoning off. `tokens` is about 3x that, so a healthy fill is never cut
    and a runaway stops below the context wall.
    """

    def truncated(self, max_tokens: int, chars: int) -> ReplyTruncatedError:
        """The error for this fill's reply cut off at `max_tokens`.

        The remedy depends on where the cap came from. A cap below the
        measured budget came from the config, and the remedy is to raise the
        config to the budget, which the fill never exceeds. At the budget, the
        reply was about three times longer than any healthy fill measured. That
        is a runaway, or a model that ignored `reasoning_effort=none` and
        spent the cap reasoning (both July 2026 cut-offs were that). Raising
        the cap helps neither.
        """
        if max_tokens < self.tokens:
            remedy = (
                f"The cap came from lm_studio.max_tokens, which is below the "
                f"measured budget of {self.tokens:,} for this fill (healthy "
                f"fills measure up to {self.healthy_max:,} tokens). Raise "
                f"lm_studio.max_tokens to {self.tokens:,}. The fill never uses "
                f"more than that."
            )
        else:
            remedy = (
                f"Healthy fills measure at most {self.healthy_max:,} tokens, so "
                f"this is likely a runaway generation (usually a list repeating "
                f"itself). If the client also warned that the model ignored "
                f"reasoning_effort=none, the cap went on reasoning instead: "
                f"pick a model whose reasoning can be disabled. Raising the cap "
                f"only lengthens the loop. Re-run, or tighten the brief."
            )
        return ReplyTruncatedError(self.purpose, max_tokens, chars, remedy)


@dataclass(frozen=True)
class ConceptBudget(_CallBudget):
    """One Step 2 concept's completion cap, sized from its healthy concepts.

    The concept reasons (`thinking=True`), so most of each reply is reasoning.
    `healthy_max` is the largest completion measured for this concept,
    reasoning included. `tokens` is about 1.5x that and at least 2x the median.
    The fills get 3x, but qwen3.5-9b generates about 22 tok/s, so 3x of a
    concept would let a runaway run for a quarter of an hour.
    """

    def truncated(self, max_tokens: int, chars: int) -> ReplyTruncatedError:
        """The error for this concept's reply cut off at `max_tokens`.

        `chars` is 0 when the cap landed inside the reasoning and no brief was
        written. As with the fills, the remedy depends on where the cap came
        from. A cap below the measured budget came from the config: all eight
        GTA5 concepts ever cut off (2026-05) were cut by a config cap of 2,000
        or 4,096, below what a healthy concept needs. At the budget, the reply
        outran every healthy concept measured, so it is a runaway, or a
        different model that reasons longer than the one the budget was sized
        for.
        """
        if max_tokens < self.tokens:
            remedy = (
                f"The cap came from lm_studio.max_tokens, which is below the "
                f"measured budget of {self.tokens:,} for this concept (healthy "
                f"concepts measure up to {self.healthy_max:,} tokens, reasoning "
                f"included). Raise lm_studio.max_tokens to {self.tokens:,}. The "
                f"concept never uses more than that."
            )
        else:
            remedy = (
                f"Healthy concepts measure at most {self.healthy_max:,} tokens, "
                f"reasoning included, so this is likely a runaway (usually "
                f"reasoning that loops). Raising the cap only lengthens the "
                f"loop. Re-run. A different model that reasons longer needs "
                f"this budget re-measured in src/llm_budgets.py."
            )
        return ReplyTruncatedError(self.purpose, max_tokens, chars, remedy)


# The three Step 3 schema fills. Measured in every LM Studio server log from
# 2025-07 to 2026-11-17 by one rule. Each "Generated prediction" is paired with
# its "Running chat completion". A reply counts for a fill by its top-level keys:
# Unity has project_name + render_pipeline, GTA5 has mission_name + steps, and a
# micro has variant. A reply counts as healthy when finish_reason is stop and it
# spent at most 1 reasoning token. Fills have sent reasoning_effort=none since
# #109 (2026-07-29). 100 fills reasoned anyway (66 Unity, 28 GTA5, 6 micro;
# median 3,388 reasoning tokens). All but one of them ran before #109, and the
# one after was qwopus3.5-9b-coder-exp, a model that ignores the setting. That
# is not what a fill costs today, so those fills are excluded. All models count.
#
# Unity (src/unity_pipeline.py): n=330, median 3,666.5, max 10,661, largest
# prompt 11,484. The 13 fills of 2026-11-14/17 alone gave median 7,993 and max
# 10,067. 30,000 is 2.81x the max, and the largest prompt plus the cap (41,484)
# fits the 47,360-token loaded context. Every one of the 39 Unity fills ever cut
# off was a runaway. Some repeated whole entries. Others counted up names, for
# example `HealthContextManager11`, writing 155-178 `scripts` against a cap of
# 24.
UNITY_FILL = FillBudget("Step 3 schema fill", tokens=30_000, healthy_max=10_661)
# GTA5 mission fill (src/pipeline.py): n=53, median 608, max 1,212, largest
# prompt 4,876. 3,600 is 2.97x the max. The two GTA5 fills ever cut off were
# reasoning, not mission: 7,708 of 8,192 tokens on 2026-07-12.
GTA5_FILL = FillBudget("GTA5 Step 3 mission fill", tokens=3_600, healthy_max=1_212)
# Micro fill (src/micro_pipeline.py, inherited by every Unity and GTA5 micro):
# n=142, median 373, max 1,481, largest prompt 2,402. 4,500 is 3.04x the max.
# The one micro fill ever cut off (NPC dialog, 2026-07-08) spent 7,866 of its
# 8,192 tokens reasoning.
MICRO_FILL = FillBudget("micro Step 3 fill", tokens=4_500, healthy_max=1_481)

# The GTA5 and micro Step 2 concepts. Until these caps both passed no
# `max_tokens`, so they inherited lm_studio.max_tokens (88,000 in the E2E
# config) and a reasoning loop stopped only at the context wall. Measured in
# every LM Studio server log from 2025-07 to 2026-11-25 by one rule. A reply
# counts as a concept when it spent more than 1 reasoning token
# (`reasoning_effort=none` reports exactly 1) and answered in prose, not JSON
# or C#. It counts for a pipeline by the role the model restates in its
# reasoning: "GTA5 mission writer" (src/prompts.py), or "Senior Unity
# Engineer" / "Senior GTA5 ScriptHookVDotNet (SHVDN3) scripter"
# (src/micro/unity_base.py, src/micro/gta5_base.py). Healthy means
# finish_reason is stop. Sized for qwen3.5-9b, the configured model. The logs of
# 2026-10/11 alone hold no GTA5 concept and only 12 micro concepts, so the whole
# history is used.
#
# GTA5 mission concept (src/pipeline.py): n=60 (2026-05-14 .. 2026-09-13),
# median 4,299, max 6,389, largest prompt 948. 9,500 is 1.49x the max and 2.2x
# the median. At the 22.4 tok/s the server logged for a GTA5 concept on
# 2026-05-14, a runaway ends after about 7 minutes. The 8
# GTA5 concepts ever cut off were cut inside their reasoning by config caps of
# 2,000 and 4,096 (2026-05-14/15), while healthy concepts those two days ran
# to 5,211. A different model, qwythos-9b (n=8, 2026-09), once wrote 11,201.
GTA5_CONCEPT = ConceptBudget("GTA5 Step 2 mission concept", tokens=9_500, healthy_max=6_389)
# Micro concept (src/micro_pipeline.py, every standalone Unity and GTA5 micro):
# n=52 (2026-05-28 .. 2026-10-26), median 2,765.5, max 5,299, largest prompt
# 602.
# 8,000 is 1.51x the max and 2.9x the median, so a runaway ends after about 6
# minutes. No micro concept was ever cut off. phi-4-reasoning-plus (n=5,
# 2026-06) peaked at 2,503.
MICRO_CONCEPT = ConceptBudget("micro Step 2 concept", tokens=8_000, healthy_max=5_299)

# Names the Unity fill and its tests import.
SCHEMA_FILL_TOKENS = UNITY_FILL.tokens
SCHEMA_FILL_HEALTHY_MAX_TOKENS = UNITY_FILL.healthy_max
# The visual compile's own fill cap, older than SCHEMA_FILL_TOKENS. Its
# cardinality is closed, so it is never allowed to exceed this.
VISUAL_COMPILE_FILL_TOKENS = 32_000


def schema_fill_tokens(*, visual_compile: bool, configured: Any = None) -> int:
    """The Unity Step 3 fill's `max_tokens` (see `FillBudget.cap`)."""
    extra = (VISUAL_COMPILE_FILL_TOKENS,) if visual_compile else ()
    return UNITY_FILL.cap(configured=configured, extra=extra)


def schema_fill_truncated(max_tokens: int, chars: int) -> ReplyTruncatedError:
    """The error for a Unity Step 3 fill reply cut off at `max_tokens`."""
    return UNITY_FILL.truncated(max_tokens, chars)


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
