"""Contract tests for the Step-3 visual dictionary seam."""

from pathlib import Path

import pytest

from src.unity_schema import EntitySpec, UnitySchema
from src.visual_compiler_dictionary import (
    FORMAT,
    VisualDictionaryError,
    apply_visual_dictionary,
    build_visual_dictionary,
    concept_file_hash,
    load_visual_dictionary,
    write_visual_dictionary,
)


class _FakeLLM:
    model = "test-vision-model"
    seed = 17

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="VisualDictionaryTest",
        company_name="TestCo",
        game_type="3D",
        platform="PC",
        render_pipeline="URP",
        unity_version="6000.0",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerCar", kind="vehicle", count=1),
            EntitySpec(name="RivalCars", kind="vehicle", count=3),
        ],
    )


def test_dictionary_is_sidecar_and_does_not_mutate_entities_or_concept(tmp_path: Path):
    concept = tmp_path / "Concept.txt"
    concept.write_bytes(b"Player drives one car; three rivals race.\r\n")
    board = tmp_path / "board.png"
    board.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cfc000000301010018dd8db40000000049454e44ae426082"
    ))
    entities = [
        EntitySpec(name="PlayerCar", kind="vehicle", count=1),
        EntitySpec(name="RivalCars", kind="vehicle", count=3),
    ]
    before = concept.read_bytes()
    llm = _FakeLLM(
        '{"style":"low poly","entities":{'
        '"PlayerCar":{"model_hint":"chunky red toy racer",'
        '"sketchfab_query":"toy race car","visual_reference":null},'
        '"RivalCars":{"model_hint":"small blue toy racers",'
        '"sketchfab_query":"toy race car rival","visual_reference":null}}}'
    )

    dictionary = build_visual_dictionary(
        concept.read_text(encoding="utf-8"),
        entities,
        llm,
        board_image=board,
        concept_file=concept,
        board_file=board,
        seed=17,
        model=llm.model,
        repo_root=tmp_path,
    )

    assert dictionary["format"] == FORMAT
    assert dictionary["concept"]["hash"] == concept_file_hash(concept)
    assert dictionary["board"]["hash"]
    assert concept.read_bytes() == before
    assert all(not entity.sketchfab_query for entity in entities)
    assert all(entry["status"] == "query_present" for entry in dictionary["entries"])
    assert llm.calls[0]["images"]

    output = tmp_path / "visual_dictionary.json"
    write_visual_dictionary(dictionary, output)
    loaded = load_visual_dictionary(output)
    updated = apply_visual_dictionary(
        schema := _schema(),
        loaded,
        expected_concept_hash=concept_file_hash(concept),
        expected_board_hash=loaded["board"]["hash"],
    )
    assert updated == 2
    assert schema.entities[0].sketchfab_query == "low poly toy race car"
    assert schema.entities[1].model_hint == "small blue toy racers"
    with pytest.raises(VisualDictionaryError, match="overwrite"):
        write_visual_dictionary(dictionary, output)


def test_unknown_llm_entity_is_rejected_without_creating_schema_entity():
    llm = _FakeLLM(
        '{"entities":{"PlayerCar":{"model_hint":"car",'
        '"sketchfab_query":"toy car"},"Duck":{"model_hint":"duck",'
        '"sketchfab_query":"rubber duck"}}}'
    )
    entities = [EntitySpec(name="PlayerCar", kind="vehicle")]
    with pytest.raises(VisualDictionaryError, match="invented undeclared entities"):
        build_visual_dictionary("The player drives a car.", entities, llm)


def test_explicit_concept_entity_index_rejects_schema_invention():
    llm = _FakeLLM('{"entities":{"PlayerCar":{"model_hint":"car",'
                         '"sketchfab_query":"toy car"}}}')
    entities = [
        EntitySpec(name="PlayerCar", kind="vehicle"),
        EntitySpec(name="Duck", kind="prop"),
    ]
    concept = "The race game.\n\nENTITIES\n- PlayerCar: player vehicle.\n"
    with pytest.raises(VisualDictionaryError, match="absent from Concept.txt"):
        build_visual_dictionary(concept, entities, llm)


def test_generated_concept_numbered_entity_index_rejects_schema_invention():
    llm = _FakeLLM('{"entities":{"PlayerCar":{"model_hint":"car",'
                         '"sketchfab_query":"toy car"}}}')
    entities = [
        EntitySpec(name="PlayerCar", kind="vehicle"),
        EntitySpec(name="Duck", kind="prop"),
    ]
    concept = (
        "TITLE: Toy Race\n\n"
        "ENTITIES (Count: 1 Total)\n"
        "1. **PlayerCar**: the player vehicle (kind: vehicle).\n\n"
        "AI BEHAVIOR\n"
        "- follows the track.\n"
    )
    with pytest.raises(VisualDictionaryError, match="absent from Concept.txt"):
        build_visual_dictionary(concept, entities, llm)


def test_generated_concept_display_name_matches_schema_pascal_case():
    llm = _FakeLLM('{"entities":{"SparkyThePitChief":{"model_hint":"pit chief",'
                         '"sketchfab_query":"pit chief npc"}}}')
    concept = (
        "ENTITIES (Count: 1 Total)\n"
        "1. **Sparky the Pit Chief**: garage NPC (kind: npc).\n"
        "\nAI BEHAVIOR\n- talks.\n"
    )
    dictionary = build_visual_dictionary(
        concept, [EntitySpec(name="SparkyThePitChief", kind="npc")], llm
    )
    assert dictionary["contract"]["concept_entity_index_checked"] is True


def test_missing_annotation_is_explicit_unfulfilled_entry():
    llm = _FakeLLM('{"entities":{"PlayerCar":{"model_hint":"toy car",'
                         '"sketchfab_query":"toy car"}}}')
    dictionary = build_visual_dictionary(
        "The player drives a car.",
        [EntitySpec(name="PlayerCar", kind="vehicle"),
         EntitySpec(name="RivalCar", kind="vehicle")],
        llm,
    )
    assert dictionary["entries"][1]["status"] == "unfulfilled_no_sketchfab_query"
    assert dictionary["entries"][1]["sketchfab_query"] is None


def test_hash_mismatch_is_rejected_before_entity_mutation(tmp_path: Path):
    concept = tmp_path / "Concept.txt"
    concept.write_text("semantic truth", encoding="utf-8")
    dictionary = {
        "format": FORMAT,
        "concept": {"hash": "wrong"},
        "board": None,
        "entries": [{
            "entity": "PlayerCar",
            "model_hint": "red car",
            "sketchfab_query": "toy car",
        }],
    }
    schema = _schema()
    with pytest.raises(VisualDictionaryError, match="Concept.txt hash mismatch"):
        apply_visual_dictionary(
            schema, dictionary, expected_concept_hash=concept_file_hash(concept)
        )
    assert schema.entities[0].sketchfab_query == ""


def test_sidecar_unknown_schema_entity_is_rejected():
    dictionary = {
        "format": FORMAT,
        "concept": {"hash": "same"},
        "board": None,
        "entries": [{
            "entity": "Duck",
            "model_hint": "rubber duck",
            "sketchfab_query": "rubber duck",
        }],
    }
    with pytest.raises(VisualDictionaryError, match="undeclared schema entities"):
        apply_visual_dictionary(_schema(), dictionary)
