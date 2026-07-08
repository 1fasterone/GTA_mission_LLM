"""Tests for the concept -> Sketchfab-query translator
(src/unity_sketchfab_translator.py). No real LLM — a fake `llm.chat()` client
returns canned JSON, mirroring tests/test_audio conventions for
concept_translator.py's shape.
"""

from src.unity_schema import EntitySpec
from src.unity_sketchfab_translator import translate_concept


class _FakeLLM:
    def __init__(self, response: str):
        self._response = response
        self.calls = []

    def chat(self, system, user, temperature=None, thinking=True, seed=None):
        self.calls.append({"system": system, "user": user})
        return self._response


def test_no_concept_or_entities_is_noop():
    assert translate_concept("", [EntitySpec(name="Tree")], object()) == 0
    assert translate_concept("a game", [], object()) == 0


def test_fills_query_with_shared_style_prefix():
    llm = _FakeLLM('{"style": "grimdark low-fantasy, weathered steel", '
                  '"entities": {"Knight": "castle guard knight", '
                  '"Sword": "longsword"}}')
    entities = [EntitySpec(name="Knight", kind="npc"),
               EntitySpec(name="Sword", kind="prop")]
    n = translate_concept("A grim castle siege.", entities, llm)
    assert n == 2
    assert entities[0].sketchfab_query == \
        "grimdark low-fantasy, weathered steel castle guard knight"
    assert entities[1].sketchfab_query == \
        "grimdark low-fantasy, weathered steel longsword"


def test_strips_think_block_and_markdown_fence():
    llm = _FakeLLM(
        "<think>reasoning...</think>```json\n"
        '{"style": "arcade neon", "entities": {"Car": "sports car"}}\n```')
    entities = [EntitySpec(name="Car", kind="vehicle")]
    n = translate_concept("Neon racer.", entities, llm)
    assert n == 1
    assert entities[0].sketchfab_query == "arcade neon sports car"


def test_only_fills_empty_sketchfab_query():
    llm = _FakeLLM('{"style": "s", "entities": {"Tree": "pine tree"}}')
    entities = [EntitySpec(name="Tree", kind="prop", sketchfab_query="already set")]
    n = translate_concept("A forest.", entities, llm)
    assert n == 0
    assert entities[0].sketchfab_query == "already set"


def test_missing_entity_in_llm_response_is_skipped():
    llm = _FakeLLM('{"style": "s", "entities": {"Other": "x"}}')
    entities = [EntitySpec(name="Tree", kind="prop")]
    n = translate_concept("A forest.", entities, llm)
    assert n == 0
    assert entities[0].sketchfab_query == ""


def test_malformed_json_degrades_to_zero():
    llm = _FakeLLM("not json at all")
    entities = [EntitySpec(name="Tree", kind="prop")]
    assert translate_concept("A forest.", entities, llm) == 0


def test_llm_exception_degrades_to_zero():
    class _Explodes:
        def chat(self, **kw):
            raise RuntimeError("LM Studio down")

    entities = [EntitySpec(name="Tree", kind="prop")]
    assert translate_concept("A forest.", entities, _Explodes()) == 0


def test_no_style_key_still_fills_bare_query():
    llm = _FakeLLM('{"entities": {"Tree": "pine tree"}}')
    entities = [EntitySpec(name="Tree", kind="prop")]
    n = translate_concept("A forest.", entities, llm)
    assert n == 1
    assert entities[0].sketchfab_query == "pine tree"


def test_dict_shaped_entities_supported():
    """The resolver also duck-types plain dicts in a couple of places — make
    sure translate_concept doesn't assume a pydantic model."""
    llm = _FakeLLM('{"style": "s", "entities": {"Tree": "pine tree"}}')
    entities = [{"name": "Tree", "kind": "prop", "model_hint": "", "sketchfab_query": ""}]
    n = translate_concept("A forest.", entities, llm)
    assert n == 1
    assert entities[0]["sketchfab_query"] == "s pine tree"
