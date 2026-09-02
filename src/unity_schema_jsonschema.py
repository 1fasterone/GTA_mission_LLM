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

ROOT CAUSE, entities coming back empty (docs/COMPILER_AUDIT.md Gap A,
2026-09-02 finding): live-verified against the real Step 3 prompt, at
max_tokens=50000, that llama.cpp's grammar for an all-optional object
property lets the model omit the KEY ENTIRELY, not just emit an empty
array — `entities` sat 20th of 37 properties (Pydantic's declaration
order, which the grammar enforces as a single monotonic pass), inside an
unbroken run of 16 consecutive skippable "legacy" fields, and the model
consistently jumped straight past all of them to the multimodal sections
the prompt's own "sections requested" list names explicitly (characters,
missions, audio_specs, ...) — reproduced 3/3 with `finish_reason: stop`
each time (not truncation). `minItems` alone did NOT fix it: an optional
property's minItems only constrains the array's contents IF the model
chooses to include the key, and it kept choosing not to. Only forcing the
KEY unskippable — adding `entities` to the top-level `required` list —
plus `minItems: 1` so the now-mandatory array can't satisfy the grammar
with `[]`, produced real on-brief entities 3/3 (a crystal pickup group
matching a brief's "five crystals", an enemy group matching "two
sentries", etc.) with no other change. `scripts` shows the same
skip-the-whole-legacy-run pattern and was NOT fixed here — it never
caused a playability-gate failure (later deterministic pipeline stages
backfill scripts regardless of what Step 3 wrote), so this fix stays
scoped to the one field with live-confirmed, gate-blocking impact rather
than guessing at a broader fix un-evidenced by a live trial.

Still unverified against OpenRouter/Ollama, and against every model this
repo has run — see docs/COMPILER_AUDIT.md Gap A. Wired in as
`lm_studio.structured_output_mode: json_schema` (src/unity_pipeline.py),
opt-in, off unless set.
"""

from __future__ import annotations
import copy
from functools import lru_cache

from .unity_schema import UnitySchema

# Fields forced non-skippable in the GENERATION-time schema only — this never
# touches UnitySchema.model_json_schema() itself (Pydantic validation stays
# exactly as permissive as it always was; a hand-authored or --no-llm fill
# with zero entities still validates fine). See the root-cause note above for
# why entities specifically needs this and json_object mode does not.
_FORCE_NONEMPTY = ("entities",)


def _harden_for_generation(schema: dict) -> dict:
    """Force the fields in _FORCE_NONEMPTY to be present AND non-empty.
    Both halves are load-bearing: `required` stops the model skipping the
    KEY, `minItems` stops it satisfying a now-mandatory key with `[]` — see
    this module's docstring for the live trial that showed neither alone
    is sufficient.

    Deep-copies first: Pydantic does not document `model_json_schema()`'s
    dicts as safe to mutate in place, and this repo has other, unrelated
    callers of it (e.g. anything introspecting UnitySchema for docs/tests) —
    mutating a dict Pydantic might share across calls would be a genuinely
    hard-to-trace action-at-a-distance bug. The schema is ~26KB; the copy
    costs nothing worth avoiding for that guarantee."""
    schema = copy.deepcopy(schema)
    required = list(schema.get("required", []))
    for name in _FORCE_NONEMPTY:
        prop = schema["properties"][name]
        if prop.get("type") != "array":
            raise ValueError(
                f"_FORCE_NONEMPTY names {name!r}, but its derived schema is "
                f"not an array ({prop.get('type')!r}) — minItems does not "
                f"apply. Fix this table, don't silently skip it."
            )
        prop["minItems"] = max(1, prop.get("minItems", 0))
        if name not in required:
            required.append(name)
    schema["required"] = required
    return schema


@lru_cache(maxsize=1)
def response_format_schema() -> dict:
    """The full `response_format` payload fragment for LMStudioClient.chat's
    `json_schema=` parameter. Memoized — UnitySchema's shape is static for
    the life of the process, so there is no reason to re-derive it per call."""
    schema = _harden_for_generation(UnitySchema.model_json_schema())
    return {
        "type": "json_schema",
        "json_schema": {
            "name":   "unity_schema",
            "schema": schema,
            "strict": False,
        },
    }
