"""Focused tests for deterministic Slice 6.4 scene/relation graphs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.scene_graph import (
    RELATION_BINDS,
    RELATION_ON_SURFACE,
    RELATION_REQUIRES,
    RELATION_SPAWNS,
    RELATION_TARGETS,
    SCENE_GRAPH_FORMAT,
    SCENE_GRAPH_MANIFEST_FORMAT,
    SceneGraphError,
    build_scene_graph,
    write_scene_graph,
)
from src.unity_schema import (
    EntitySpec,
    MissionObjective,
    MissionSpec,
    SceneSpec,
    UnitySchema,
    WiringDependency,
    WiringMap,
)
from src.unity_spatial_roles import derive_spatial_roles


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="GraphRacer",
        company_name="Test",
        description="A deterministic route with a checkpoint and hazards.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main", "Cave"],
        scene_specs=[
            SceneSpec(
                name="Main",
                entities=["PlayerBike", "Track", "Checkpoint", "ScenicRock"],
            ),
            SceneSpec(name="Cave", entities=["PlayerBike", "Track", "Mines", "ScenicRock"]),
        ],
        entities=[
            EntitySpec(name="PlayerBike", kind="vehicle", tag="Player"),
            EntitySpec(
                name="Track",
                kind="track",
                placement="path",
                path_points=[[0, 0, 0], [0, 0, 40]],
                path_width=8,
            ),
            EntitySpec(name="Checkpoint", kind="checkpoint"),
            EntitySpec(name="Mines", kind="hazard", count=3),
            EntitySpec(name="ScenicRock", kind="prop", model_hint="large rock"),
        ],
        missions=[
            MissionSpec(
                id="race",
                title="Race",
                objectives=[
                    MissionObjective(
                        id="reach_checkpoint",
                        description="Reach the checkpoint.",
                        type="reach",
                        target_ref="Checkpoint",
                    ),
                    MissionObjective(
                        id="survive_mines",
                        description="Survive the mines.",
                        type="survive",
                        target_ref="Mines",
                    ),
                ],
            )
        ],
        wiring=WiringMap(dependencies=[
            WiringDependency(source="PlayerBike", target="Checkpoint", type="references"),
        ]),
        meta={"mission_scenes": {"race": ["Main"]}},
    )


def _edges(graph: dict, rel: str) -> list[dict]:
    return [edge for edge in graph["edges"] if edge["rel"] == rel]


def test_graph_is_coordinate_free_closed_and_deterministic() -> None:
    schema = _schema()
    before = schema.model_dump(mode="json")
    roles = derive_spatial_roles(schema)

    first = build_scene_graph(schema, roles)
    second = build_scene_graph(schema, roles)

    assert first == second
    assert first["format"] == SCENE_GRAPH_FORMAT
    assert first["authority"]["llm_called_here"] is False
    assert first["authority"]["world_coordinates_emitted_here"] is False
    assert all(node["transform_q"] is None for node in first["nodes"])
    assert all(edge["rel"] in first["relation_vocabulary"] for edge in first["edges"])
    assert schema.model_dump(mode="json") == before


def test_graph_preserves_scene_membership_and_objective_order() -> None:
    graph = build_scene_graph(_schema())
    by_name = {node["name"]: node for node in graph["nodes"]}

    assert by_name["PlayerBike"]["kind"] == "entity"
    main_id = by_name["Main"]["id"]
    cave_id = by_name["Cave"]["id"]
    rock_id = by_name["ScenicRock"]["id"]
    assert by_name["ScenicRock"]["parent"] == main_id
    assert {edge["src"] for edge in _edges(graph, RELATION_SPAWNS)
            if edge["dst"] == rock_id} == {main_id, cave_id}

    requires = _edges(graph, RELATION_REQUIRES)
    assert len(requires) == 1
    assert requires[0]["required"] is True
    assert any(edge["rel"] == RELATION_TARGETS for edge in graph["edges"])
    assert any(edge["rel"] == RELATION_ON_SURFACE for edge in graph["edges"])
    assert any(edge["rel"] == RELATION_BINDS for edge in graph["edges"])


def test_unresolved_objective_never_invents_a_target_node() -> None:
    schema = _schema()
    schema.missions[0].objectives[0].target_ref = "ImageOnlyArch"

    graph = build_scene_graph(schema)

    assert not any(node["name"] == "ImageOnlyArch" for node in graph["nodes"])
    target_edges = _edges(graph, RELATION_TARGETS)
    assert len(target_edges) == 1
    assert next(node for node in graph["nodes"] if node["id"] == target_edges[0]["dst"])["name"] == "Mines"
    assert any("ImageOnlyArch" in warning for warning in graph["warnings"])


def test_duplicate_entity_names_are_a_loud_abstention() -> None:
    schema = _schema()
    schema.entities.append(EntitySpec(name="scenicrock", kind="prop"))

    with pytest.raises(SceneGraphError, match="duplicate entity names"):
        build_scene_graph(schema)


def test_writer_hash_links_graph_manifest_and_optional_dot(tmp_path: Path) -> None:
    schema_path = tmp_path / "GraphRacer.schema.json"
    output = tmp_path / "GraphRacer.scene-graph.json"
    manifest = tmp_path / "GraphRacer.scene-graph.manifest.json"
    dot = tmp_path / "GraphRacer.scene-graph.dot"
    schema_path.write_text(_schema().model_dump_json(indent=2), encoding="utf-8")

    result = write_scene_graph(schema_path, output, manifest, dot_output=dot)

    assert result == manifest
    graph = json.loads(output.read_text(encoding="utf-8"))
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert graph["format"] == SCENE_GRAPH_FORMAT
    assert manifest_data["format"] == SCENE_GRAPH_MANIFEST_FORMAT
    assert manifest_data["outputs"]["scene_graph"]["sha256"]
    assert manifest_data["outputs"]["scene_graph"]["graph_hash"] == graph["graph_hash"]
    assert 'digraph SceneGraph' in dot.read_text(encoding="utf-8")


def test_graph_module_contains_no_pipeline_llm_client() -> None:
    source = Path("src/scene_graph.py").read_text(encoding="utf-8")
    assert "make_chat_client" not in source
    assert "llm.chat" not in source


# ── Orphans: a recorded membership abstention is omitted, anything else fails ──
# neon_fugitive E2E: an entity the Step 5 membership pass could find no scene
# for used to abort the whole run here. Now the graph leaves out ONLY the
# entities that pass recorded as abstained, naming each one. An orphan nobody
# explained is still a hard failure.

def _with_orphan() -> UnitySchema:
    schema = _schema()
    schema.entities.append(EntitySpec(name="Debris", kind="prop", count=4))
    return schema


def test_an_unexplained_orphan_still_fails_the_graph() -> None:
    with pytest.raises(SceneGraphError,
                       match="'Debris' is excluded from every declared scene.*"
                             "recorded no abstention"):
        build_scene_graph(_with_orphan())


def test_a_recorded_abstention_is_omitted_and_named_once() -> None:
    from src.unity_scene_membership import assign_scene_membership

    schema = _with_orphan()
    assign_scene_membership(schema)
    assert [row["entity"] for row in schema.meta["scene_membership"]["abstentions"]] == ["Debris"]

    graph = build_scene_graph(schema)

    assert graph["omitted_entities"] == ["Debris"]
    assert not any(node["name"] == "Debris" for node in graph["nodes"])
    named = [w for w in graph["warnings"] if "'Debris'" in w]
    assert len(named) == 1
    assert "spawns in no scene" in named[0]
    assert graph["abstention_count"] >= 1
    assert graph["authority"]["entities_invented_here"] is False


def test_an_abstention_that_no_longer_holds_is_not_omitted() -> None:
    # A later pass (visual scene coverage) gave the entity a scene after the
    # membership pass abstained. The entity is in the graph like any other.
    from src.unity_scene_membership import assign_scene_membership

    schema = _with_orphan()
    assign_scene_membership(schema)
    schema.scene_specs[0].entities.append("Debris")

    graph = build_scene_graph(schema)

    assert graph["omitted_entities"] == []
    assert any(node["name"] == "Debris" for node in graph["nodes"])


def test_an_objective_on_an_omitted_entity_invents_no_target_edge() -> None:
    from src.unity_scene_membership import assign_scene_membership

    schema = _with_orphan()
    schema.missions.append(MissionSpec(id="salvage", title="Salvage", objectives=[
        MissionObjective(id="grab", description="Grab the debris.",
                         type="collect", target_ref="Debris"),
    ]))
    assign_scene_membership(schema)   # "salvage" has no inferred scene
    assert "Debris" in {row["entity"] for row in schema.meta["scene_membership"]["abstentions"]}

    graph = build_scene_graph(schema)

    names = {node["id"]: node["name"] for node in graph["nodes"]}
    assert all(names[edge["dst"]] != "Debris" for edge in _edges(graph, RELATION_TARGETS))
    assert any("salvage/grab targets 'Debris'" in w for w in graph["warnings"])
