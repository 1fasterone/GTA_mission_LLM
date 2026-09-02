"""
Tests for src/unity_schema_jsonschema.py — the derived response_format
payload used for real shape-constrained decoding (json_schema mode).
"""
from src.unity_schema import UnitySchema
from src.unity_schema_jsonschema import response_format_schema


class TestResponseFormatSchema:
    def test_top_level_shape(self):
        fmt = response_format_schema()
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["name"] == "unity_schema"
        assert fmt["json_schema"]["strict"] is False

    def test_memoized_same_object(self):
        # lru_cache — same call returns the identical object, not a fresh
        # re-derivation each time.
        assert response_format_schema() is response_format_schema()

    def test_every_top_level_field_present(self):
        schema = response_format_schema()["json_schema"]["schema"]
        props = schema["properties"]
        for name in UnitySchema.model_fields:
            assert name in props, f"UnitySchema field {name!r} missing from derived JSON Schema"

    def test_meta_stays_open(self):
        """The one untyped `dict` field on UnitySchema must stay a genuinely
        open object in the derived schema — no accidental additionalProperties
        lockdown that would reject arbitrary meta keys at grammar-compile time."""
        schema = response_format_schema()["json_schema"]["schema"]
        meta_schema = schema["properties"]["meta"]
        assert meta_schema.get("type") == "object"
        assert meta_schema.get("additionalProperties") is True

    def test_uses_refs_not_forced_strict_required(self):
        """Confirms this targets the permissive llama.cpp grammar converter,
        not OpenAI strict mode: $defs/$ref present, and not every field is
        forced required (UnitySchema has genuinely optional fields)."""
        schema = response_format_schema()["json_schema"]["schema"]
        assert "$defs" in schema
        assert len(schema["required"]) < len(schema["properties"])

    def test_entities_forced_required_and_nonempty(self):
        """Root-cause fix (docs/COMPILER_AUDIT.md Gap A, 2026-09-02): without
        this, entities is skippable (all-optional legacy field, 20th of 37 in
        declaration order) and live-verified 3/3 to come back missing entirely
        under grammar-constrained decoding with the real Step 3 prompt.
        `required` alone isn't enough either — a required-but-empty array
        still satisfies the grammar — so both must hold together."""
        schema = response_format_schema()["json_schema"]["schema"]
        assert "entities" in schema["required"]
        assert schema["properties"]["entities"]["minItems"] >= 1

    def test_hardening_does_not_mutate_pydantics_own_schema(self):
        """Guards the deep-copy: a fresh, unhardened call to
        UnitySchema.model_json_schema() must still show entities as
        optional — proves response_format_schema() didn't mutate something
        Pydantic shares across calls."""
        fresh = UnitySchema.model_json_schema()
        assert "entities" not in fresh.get("required", [])
        assert "minItems" not in fresh["properties"]["entities"]
