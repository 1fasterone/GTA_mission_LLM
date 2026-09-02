"""
Derives an OpenAI-compatible `response_format={"type":"json_schema",...}`
payload fragment from UnitySchema — real shape-constrained decoding for the
Step 3 schema fill, not just the syntax-only `json_object` mode already
shipped (see config.yaml's `lm_studio.structured_output`).

The schema is DERIVED, never hand-written: `UnitySchema.model_json_schema()`
is the single source of truth, so a field added/removed/retyped on the
Pydantic model automatically reaches the grammar with no second place to
edit. Confirmed live (2026-09-01): 37 top-level properties, 46 `$defs`,
`$ref`-based, ~25.8KB serialized. `meta` (the one untyped `dict` field on
UnitySchema) already renders permissively —
`{"type": "object", "additionalProperties": true}` — Pydantic does this on
its own, so no post-processing is needed to keep it open.

`strict: False` is deliberate, not an oversight. OpenAI's `strict` mode
demands every field be required and forbids open objects/dicts; this
repo's actual runtime is LM Studio's local llama.cpp grammar converter
(json-schema-to-GBNF), not OpenAI's validator, and it accepts the looser,
more expressive real-JSON-Schema shape this model produces (`$ref`/`$defs`,
Optional/nullable fields, `meta`'s open `additionalProperties`). Forcing
OpenAI-strict shape here would mean hand-mangling a schema for a validator
that never actually runs it.

Still unverified against a live server — see docs/COMPILER_AUDIT.md Gap A.
A 46-`$def` grammar has a real per-token compilation/evaluation cost on
llama.cpp that needs measuring, not assuming, before this becomes anyone's
default. Wired in as `lm_studio.structured_output_mode: json_schema`
(src/unity_pipeline.py), opt-in, off unless set.
"""

from __future__ import annotations
from functools import lru_cache

from .unity_schema import UnitySchema


@lru_cache(maxsize=1)
def response_format_schema() -> dict:
    """The full `response_format` payload fragment for LMStudioClient.chat's
    `json_schema=` parameter. Memoized — UnitySchema's shape is static for
    the life of the process, so there is no reason to re-derive it per call."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name":   "unity_schema",
            "schema": UnitySchema.model_json_schema(),
            "strict": False,
        },
    }
