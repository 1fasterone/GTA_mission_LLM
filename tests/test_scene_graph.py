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
