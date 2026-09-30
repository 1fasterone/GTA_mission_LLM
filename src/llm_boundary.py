"""The freeze boundary: the point after which no Pipeline LLM call may happen.

CLAUDE.md's Pipeline Contract gives the Pipeline LLM Steps 2-3 only. Some
LLM-authored content is still wanted beyond the bare schema fill — script and
micro bodies, Sketchfab queries, NPC dialog lines, diegetic audio cues — so
it is authored in ONE declared phase, Step 3.5, and then written into the
schema (fields, `schema.microgens[].input`, `schema.meta["llm_enrichment"]`).
Steps 4-7 read those frozen results and never the model.

`FrozenLLM` enforces that at runtime instead of by convention. It wraps the
live client once Step 3.5 has finished; any inference call after that raises
`PostSchemaLLMError` naming the method, which is the loud abstain the contract
asks for — a post-schema call that silently succeeded would make the run
unreplayable, and one swallowed by a `try/except` would ship a silent
fallback. Lifecycle calls (`unload`) and pure helpers (`extract_json`) still
pass through: neither samples the model.
"""
from __future__ import annotations

from typing import Any

# The meta key Step 3.5 owns. Compiler-owned: anything the schema-fill LLM
# wrote under it is discarded before Step 3.5 writes its own record.
ENRICHMENT_META_KEY = "llm_enrichment"
ENRICHMENT_FORMAT = "game-o-matic/llm-enrichment/v1"

# Methods that sample the model. Everything else on the client is either
# lifecycle (unload) or pure text handling (extract_json).
_INFERENCE_METHODS = frozenset({"chat"})


class PostSchemaLLMError(RuntimeError):
    """An LLM call was attempted after the Step 3.5 freeze boundary."""


class FrozenLLM:
    """Pass-through wrapper that refuses inference after the freeze boundary."""

    def __init__(self, inner: Any, *, boundary: str = "Step 3.5") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_boundary", boundary)

    @property
    def inner(self) -> Any:
        return object.__getattribute__(self, "_inner")

    def __getattr__(self, name: str) -> Any:
        if name in _INFERENCE_METHODS:
            boundary = object.__getattribute__(self, "_boundary")

            def _refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise PostSchemaLLMError(
                    f"LLM.{name}() called after the {boundary} freeze boundary. "
                    "Steps 4-7 must be deterministic: author this content in "
                    "Step 3.5 (src/unity_pipeline.py _run_llm_enrichment) and "
                    "persist it into the schema instead."
                )
            return _refuse
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_inner"), name, value)


def freeze(llm: Any, *, boundary: str = "Step 3.5") -> Any:
    """Wrap `llm` so inference raises; idempotent, None stays None."""
    if llm is None or isinstance(llm, FrozenLLM):
        return llm
    return FrozenLLM(llm, boundary=boundary)


def thaw(llm: Any) -> Any:
    """Return the live client behind a `FrozenLLM` (or `llm` unchanged)."""
    return llm.inner if isinstance(llm, FrozenLLM) else llm
