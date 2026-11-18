"""Step 5 entity-name disambiguation (src/unity_entity_names.py).

Live subject: run 04 of the 2026-11-14 neon_fugitive E2E batch
(tests/fixtures/neon_fugitive_run04_failed_llm.schema.json, a byte-for-byte
copy of the failed-run dump; the Pipeline LLM wrote its Step 3 content). It
died ``[graph.strict] duplicate entity names make graph endpoints ambiguous:
debris``: two different groups, both named ``Debris``, one per scene.

The unit cases below are engineer-written test data, not pipeline output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.atoms.base import AtomInvocation
from src.scene_graph import SceneGraphError, build_scene_graph
from src.unity_entity_names import (
    ENTITY_NAMES_FORMAT,
    ENTITY_NAMES_META_KEY,
    disambiguate_entity_names,
    entity_name_abstention,
    entity_name_warnings,
)
from src.unity_failed_run import classify_failure
from src.unity_pipeline import (
    _check_consistency,
    _infer_mission_scenes,
    _programmatic_fix,
    _reconcile_scene_specs,
)
from src.unity_scene_membership import hosting_scenes
from src.unity_schema import (
    EntitySpec,
    MissionObjective,
    MissionSpec,
    SceneSpec,
    UnitySchema,
    WorldRegion,
    WorldSpec,
)
from src.unity_spatial_roles import derive_spatial_roles

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> UnitySchema:
    return UnitySchema.model_validate(
        json.loads((_FIXTURES / name).read_text(encoding="utf-8")))


def _step5(schema: UnitySchema) -> tuple[UnitySchema, list[str]]:
    """The live Step 5 order the scene graph depends on."""
    schema = _programmatic_fix(schema)
    schema, fixes = _check_consistency(schema, reconcile_scenes=False)
    resolved, _ = _infer_mission_scenes(schema)
    schema.meta["mission_scenes"] = resolved
    schema, more = _reconcile_scene_specs(schema)
    return schema, fixes + more


def _graph(schema: UnitySchema) -> dict:
    return build_scene_graph(schema, derive_spatial_roles(schema))


def _spawns(graph: dict) -> dict[str, list[str]]:
    names = {node["id"]: node["name"] for node in graph["nodes"]}
    out: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        if edge["rel"] == "spawns":
            out.setdefault(names[edge["dst"]], []).append(names[edge["src"]])
    return out


def _schema(entities: list[EntitySpec], specs: list[SceneSpec], **extra) -> UnitySchema:
    scenes = ["Main"] + [s.name for s in specs if s.name != "Main"]
    return UnitySchema(
        project_name="NameTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        scenes=scenes, scene_specs=specs, entities=entities, **extra)


def _player() -> EntitySpec:
    return EntitySpec(name="Player", kind="player", tag="Player")


def _debris_pair(second: str = "Debris") -> list[EntitySpec]:
    return [
        EntitySpec(name="Debris", kind="prop", count=20, model_hint="scrap pile"),
        EntitySpec(name=second, kind="prop", count=15, model_hint="flooded trash"),
    ]


def _two_scenes(first: list[str], second: list[str]) -> list[SceneSpec]:
    return [SceneSpec(name="Alley", entities=first),
            SceneSpec(name="Slums", entities=second)]


# ══════════════════════════════════════════════════════════════════════════════
# Live run 04
# ══════════════════════════════════════════════════════════════════════════════

def test_run04_fixture_still_carries_the_collision():
    """Guards the regression subject: two DIFFERENT groups, one name."""
    schema = _load("neon_fugitive_run04_failed_llm.schema.json")
    debris = [(i, e) for i, e in enumerate(schema.entities) if e.name == "Debris"]
    assert [i for i, _ in debris] == [1, 15]
    assert [e.count for _, e in debris] == [20, 15]
    listed = [s.name for s in schema.scene_specs if "Debris" in (s.entities or [])]
    assert listed == ["ActivationAlley", "SlumFlooded"]
    with pytest.raises(SceneGraphError, match="duplicate entity names"):
        _graph(schema)


def test_run04_copies_are_renamed_by_the_scene_that_lists_each():
    schema, fixes = _step5(_load("neon_fugitive_run04_failed_llm.schema.json"))
    assert schema.entities[1].name == "ActivationAlleyDebris"
    assert schema.entities[1].count == 20
    assert schema.entities[15].name == "SlumFloodedDebris"
    assert schema.entities[15].count == 15
    assert not [e for e in schema.entities if e.name.casefold() == "debris"]

    specs = {s.name: s.entities for s in schema.scene_specs}
    assert "ActivationAlleyDebris" in specs["ActivationAlley"]
    assert "SlumFloodedDebris" in specs["SlumFlooded"]
    assert not any("Debris" in (names or []) for names in specs.values())

    record = schema.meta[ENTITY_NAMES_META_KEY]
    assert record["format"] == ENTITY_NAMES_FORMAT
    assert record["stage"] == "5_entity_names"
    assert [(r["from"], r["to"], r["index"], r["scene"]) for r in record["renames"]] == [
        ("Debris", "ActivationAlleyDebris", 1, "ActivationAlley"),
        ("Debris", "SlumFloodedDebris", 15, "SlumFlooded"),
    ]
    assert all("copy k in schema order" in r["evidence"] for r in record["renames"])
    assert record["abstentions"] == [] and record["collapsed"] == []
    assert sum(m.startswith("[entity.names] 2 distinct groups named 'Debris'")
               for m in fixes) == 1


def test_run04_layout_regions_are_rederived_per_copy():
    """The dump was taken after layout: one region row per group, named after
    it. Those rows are compiler output, re-derived for each renamed copy."""
    schema, _ = _step5(_load("neon_fugitive_run04_failed_llm.schema.json"))
    regions = schema.world_spec.regions
    assert not [r for r in regions if r.name.casefold() == "debris"]
    for name, index in (("ActivationAlleyDebris", 1), ("SlumFloodedDebris", 15)):
        rows = [r for r in regions if r.name == name]
        assert len(rows) == 1 and rows[0].entities == [name]
        e = schema.entities[index]
        assert rows[0].bounds == pytest.approx([
            e.origin_offset[0] - e.group_radius, e.origin_offset[1] - e.group_radius,
            e.origin_offset[0] + e.group_radius, e.origin_offset[1] + e.group_radius])


def test_run04_graph_builds_with_each_copy_in_its_own_scene():
    schema, _ = _step5(_load("neon_fugitive_run04_failed_llm.schema.json"))
    graph = _graph(schema)
    spawns = _spawns(graph)
    # "Main" has no spec, so it hosts every group; each district hosts its own.
    assert spawns["ActivationAlleyDebris"] == ["Main", "ActivationAlley"]
    assert spawns["SlumFloodedDebris"] == ["Main", "SlumFlooded"]
    assert graph["omitted_entities"] == []
    assert schema.meta["scene_membership"]["abstentions"] == []


def test_run04_pass_is_idempotent_and_keeps_its_audit_trail():
    schema, _ = _step5(_load("neon_fugitive_run04_failed_llm.schema.json"))
    before = [e.name for e in schema.entities]
    assert disambiguate_entity_names(schema) == []
    assert [e.name for e in schema.entities] == before
    assert len(schema.meta[ENTITY_NAMES_META_KEY]["renames"]) == 2


def test_run10_raw_fill_fixture_is_the_verbatim_pipeline_response():
    """Declared transformation: extracted from the capture, no edits. The digest
    is over LF text, so a CRLF checkout still proves it."""
    text = (_FIXTURES / "neon_fugitive_run10_fill_raw.json").read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n")
    assert len(text) == 31266
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == (
        "c252432ba6391c2986ffe3197222e690e7f248aac051cb0fe8165ca6a3c00312")


# ══════════════════════════════════════════════════════════════════════════════
# Unit cases (engineer-written test data)
# ══════════════════════════════════════════════════════════════════════════════

def test_identical_copies_collapse_to_the_first():
    rocks = EntitySpec(name="Rocks", kind="prop", count=6, model_hint="boulder")
    repeat = rocks.model_copy(deep=True)
    repeat.instance_positions = [[1.0, 0.0, 2.0]] * 6     # layout-owned
    schema = _schema([_player(), rocks, repeat],
                     [SceneSpec(name="Canyon", entities=["Player", "Rocks"])])
    msgs = disambiguate_entity_names(schema)
    assert [e.name for e in schema.entities] == ["Player", "Rocks"]
    assert schema.entities[1].instance_positions == []    # the first one wins
    row = schema.meta[ENTITY_NAMES_META_KEY]["collapsed"][0]
    assert (row["kept_index"], row["dropped_index"]) == (1, 2)
    assert "was a repeat of entities[1]" in msgs[0]
    _graph(schema)


def test_names_are_one_identity_across_case():
    """`Debris` and `debris` are one graph endpoint (scene_graph casefolds)."""
    schema = _schema([_player(), *_debris_pair(second="debris")],
                     _two_scenes(["Player", "Debris"], ["Player", "debris"]))
    disambiguate_entity_names(schema)
    assert [e.name for e in schema.entities[1:]] == ["AlleyDebris", "SlumsDebris"]
    assert schema.scene_specs[1].entities == ["Player", "SlumsDebris"]
    assert hosting_scenes(schema, "SlumsDebris") == ["Main", "Slums"]   # Main: no spec
    assert hosting_scenes(schema, "AlleyDebris") == ["Main", "Alley"]
    _graph(schema)


def test_case_only_twins_are_identical_copies():
    twin = EntitySpec(name="rocks", kind="prop", count=6)
    schema = _schema([_player(), EntitySpec(name="Rocks", kind="prop", count=6), twin],
                     [SceneSpec(name="Canyon", entities=["Player", "Rocks"])])
    disambiguate_entity_names(schema)
    assert [e.name for e in schema.entities] == ["Player", "Rocks"]


def test_count_mismatch_abstains_and_the_graph_quotes_why():
    schema = _schema([_player(), *_debris_pair()],
                     _two_scenes(["Player", "Debris"], ["Player"]))
    assert disambiguate_entity_names(schema) == []       # nothing changed
    assert [e.name for e in schema.entities] == ["Player", "Debris", "Debris"]
    [warning] = entity_name_warnings(schema)
    assert warning.startswith("[entity.names] ABSTAINED on duplicate entity name 'Debris' "
                              "(2 distinct groups: entities[1] 'Debris'")
    assert "Names stay as authored" in warning and "Remedy: give each group" in warning
    row = entity_name_abstention(schema, "debris")
    assert row is not None
    assert "2 distinct copies but 1 declared scene_spec(s)" in row["reason"]
    assert [c["index"] for c in row["copies"]] == [1, 2]
    assert row["listings"] == ["Alley"]
    with pytest.raises(SceneGraphError) as err:
        _graph(schema)
    assert "[entity.names] abstained — 2 distinct copies" in str(err.value)


def test_a_bare_reference_outside_scene_specs_abstains():
    mission = MissionSpec(id="m1", title="Clear it", objectives=[
        MissionObjective(id="o1", description="clear the debris", target_ref="Debris")])
    schema = _schema([_player(), *_debris_pair()],
                     _two_scenes(["Player", "Debris"], ["Player", "Debris"]),
                     missions=[mission])
    disambiguate_entity_names(schema)
    assert [e.name for e in schema.entities[1:]] == ["Debris", "Debris"]
    row = entity_name_abstention(schema, "Debris")
    assert "missions[0].objectives[0].target_ref refer to the bare name" in row["reason"]


def test_an_atom_parameter_naming_the_bare_name_abstains():
    pair = _debris_pair()
    pair[0].atoms = [AtomInvocation(atom="Despawn", args={"entity": "Debris"})]
    schema = _schema([_player(), *pair],
                     _two_scenes(["Player", "Debris"], ["Player", "Debris"]))
    disambiguate_entity_names(schema)
    row = entity_name_abstention(schema, "Debris")
    assert row is not None and "entities[1].atoms[0].args.entity" in row["reason"]


def test_a_new_name_that_collides_abstains():
    schema = _schema([_player(), *_debris_pair(), EntitySpec(name="AlleyDebris")],
                     _two_scenes(["Player", "Debris", "AlleyDebris"], ["Player", "Debris"]))
    disambiguate_entity_names(schema)
    row = entity_name_abstention(schema, "Debris")
    assert "['AlleyDebris'] would collide" in row["reason"]
    assert [e.name for e in schema.entities] == ["Player", "Debris", "Debris", "AlleyDebris"]


def test_a_listing_repeated_inside_one_spec_abstains():
    schema = _schema([_player(), *_debris_pair()],
                     _two_scenes(["Player", "Debris", "Debris"], ["Player"]))
    disambiguate_entity_names(schema)
    assert "list the name more than once" in entity_name_abstention(schema, "Debris")["reason"]


def test_an_authored_region_naming_it_is_a_reference():
    """Only the layout's own per-group rows are re-derived. A region with its
    own biome is authored content that names the bare group."""
    schema = _schema([_player(), *_debris_pair()],
                     _two_scenes(["Player", "Debris"], ["Player", "Debris"]),
                     world_spec=WorldSpec(regions=[WorldRegion(
                         name="Junkyard", biome="urban", entities=["Debris"])]))
    disambiguate_entity_names(schema)
    row = entity_name_abstention(schema, "Debris")
    assert row is not None and "world_spec.regions[0]" in row["reason"]


def test_unique_names_write_no_record():
    schema = _schema([_player(), EntitySpec(name="Rocks")],
                     [SceneSpec(name="Canyon", entities=["Player", "Rocks"])])
    assert disambiguate_entity_names(schema) == []
    assert ENTITY_NAMES_META_KEY not in schema.meta


def test_graph_error_without_a_record_says_the_pass_did_not_run():
    schema = _schema([_player(), *_debris_pair()],
                     _two_scenes(["Player", "Debris"], ["Player", "Debris"]))
    with pytest.raises(SceneGraphError, match="did not run on this schema"):
        _graph(schema)


def test_failed_run_remedy_covers_duplicate_names():
    category, _, remedy = classify_failure(SceneGraphError(
        "[graph.strict] duplicate entity names make graph endpoints ambiguous: x"))
    assert category == "scene_graph"
    assert "meta.entity_names.abstentions" in remedy


def test_step3_prompt_asks_for_unique_entity_names():
    from src.unity_prompts import UNITY_SCHEMA_FILL_USER

    prompt = UNITY_SCHEMA_FILL_USER.format(concept="c", schema_hints="",
                                           multimodal_block="", brief_block="")
    assert 'Every entity "name" must be unique across the whole project' in prompt
    assert "written ONCE and" in prompt


def test_a_resolved_schema_warns_nothing():
    schema, _ = _step5(_load("neon_fugitive_run04_failed_llm.schema.json"))
    assert entity_name_warnings(schema) == []
