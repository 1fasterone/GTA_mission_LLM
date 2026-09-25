"""Deterministic Slice 6.4 scene and relation graph compiler.

The graph is a derived Step-5 sidecar.  It is built from declared schema
content and the already-closed spatial-role table; it never calls the pipeline
LLM, invents an entity from prose or image evidence, or emits world
coordinates.  Scene membership, mission objectives, route adjacency and
declared wiring are represented in one graph dialect so the later hybrid
layout stages do not have to reconcile parallel adjacency/anchor artifacts.

The graph deliberately keeps the existing flat ``UnitySchema`` untouched.
``write_scene_graph`` freezes the graph and a hash-linked manifest for the
standalone CLI; a later consumer can attach the same graph to the pipeline
without changing the production schema contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .ident import HashRef, chash
from .ident.tags import NODE, SCENE
from .unity_layout_archetype import spatial_relationship_of
from .unity_schema import UnitySchema
from .unity_spatial_roles import (
    ROLE_AIRBORNE_HAZARD,
    ROLE_CHECKPOINT,
    ROLE_HAZARD,
    ROLE_NAVIGATION_MARKER,
    ROLE_PLAYER_PATH,
    ROLE_SCENERY,
    derive_spatial_roles,
)
from .unity_spatial_identity import semantic_schema_hash
from .visual_reasoning_compiler import sha256_bytes


SCENE_GRAPH_FORMAT = "visual-reasoning-compiler/scene-relation-graph/v1"
SCENE_GRAPH_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/scene-relation-graph-manifest/v1"
)
SCENE_GRAPH_STAGE = "6.4_scene_relation_graph"

RELATION_PARENT_OF = "parent_of"
RELATION_SPAWNS = "spawns"
RELATION_TARGETS = "targets"
RELATION_TRIGGERS = "triggers"
RELATION_REQUIRES = "requires"
RELATION_NEAR = "near"
RELATION_ON_SURFACE = "on_surface"
RELATION_BINDS = "binds"
RELATION_REACHES = "reaches"

RELATIONS = frozenset({
    RELATION_PARENT_OF,
    RELATION_SPAWNS,
    RELATION_TARGETS,
    RELATION_TRIGGERS,
    RELATION_REQUIRES,
    RELATION_NEAR,
    RELATION_ON_SURFACE,
    RELATION_BINDS,
    RELATION_REACHES,
})

_WIRING_RELATIONS = {
    "contains": RELATION_PARENT_OF,
    "references": RELATION_BINDS,
    "uses": RELATION_BINDS,
    "triggers": RELATION_TRIGGERS,
}


class SceneGraphError(ValueError):
    """Raised for an unresolvable or structurally invalid derived graph."""


def _semantic_schema_hash(schema: UnitySchema) -> str:
    return semantic_schema_hash(schema)


def _clean_name(value: Any) -> str:
    return str(value or "").strip()


def _casefold(value: Any) -> str:
    return _clean_name(value).casefold()


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _scene_names(schema: UnitySchema) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    names = [_clean_name(value) for value in (schema.scenes or [])]
    names = _unique(name for name in names if name)
    if not names:
        raise SceneGraphError(
            "[graph.strict] schema declares no scenes; add at least one scene "
            "before compiling the scene graph"
        )
    declared = set(names)
    for spec in schema.scene_specs or []:
        name = _clean_name(spec.name)
        if name and name not in declared:
            warnings.append(
                f"[graph.strict] scene_spec {name!r} is not in schema.scenes; "
                "it is not represented"
            )
    return names, warnings


def _duplicate_names(values: Iterable[str]) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        folded = _casefold(value)
        counts[folded] = counts.get(folded, 0) + 1
    return sorted(key for key, count in counts.items() if count > 1)


def _scene_membership(schema: UnitySchema, scene_names: list[str], entity_name: str) -> list[str]:
    specs = {
        _clean_name(spec.name): spec
        for spec in (schema.scene_specs or [])
        if _clean_name(spec.name) in scene_names
    }
    active: list[str] = []
    for scene in scene_names:
        spec = specs.get(scene)
        if spec is None or spec.entities is None or entity_name in spec.entities:
            active.append(scene)
    return active


def _role_data(spatial_roles: Optional[Mapping[str, Any]], schema: UnitySchema) -> dict[str, Any]:
    if spatial_roles is None:
        return derive_spatial_roles(schema)
    if not isinstance(spatial_roles, Mapping):
        raise SceneGraphError("[graph.strict] spatial-role input must be a JSON object")
    expected = _semantic_schema_hash(schema)
    actual = ((spatial_roles.get("inputs") or {}).get("schema_sha256"))
    if actual and actual != expected:
        raise SceneGraphError(
            "[graph.strict] spatial-role sidecar belongs to a different schema; "
            "re-run unity-vizconcept-spatial-roles"
        )
    rows = spatial_roles.get("role_table")
    if not isinstance(rows, list):
        raise SceneGraphError("[graph.strict] spatial-role sidecar has no role_table")
    return dict(spatial_roles)


def _role_rows(table: Mapping[str, Any], entity_names: list[str]) -> dict[str, dict[str, Any]]:
    rows = table.get("role_table")
    if not isinstance(rows, list):
        raise SceneGraphError("[graph.strict] spatial-role table is malformed")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SceneGraphError("[graph.strict] spatial-role table contains a non-object row")
        name = _clean_name(row.get("entity"))
        if not name or name not in entity_names or name in out:
            raise SceneGraphError(
                f"[graph.strict] spatial-role table does not map one unique declared "
                f"entity: {name or '<blank>'!r}"
            )
        out[name] = dict(row)
    missing = [name for name in entity_names if name not in out]
    if missing:
        raise SceneGraphError(
            "[graph.strict] spatial-role table is missing declared entity(s): "
            + ", ".join(missing)
        )
    return out


def _node_payload(
    *,
    kind: str,
    name: str,
    parent: Optional[str],
    source: str,
    tag: str,
    asset_ref: Optional[str],
) -> dict[str, Any]:
    # ``transform_q`` stays null in 6.4.  World realization owns transforms in
    # 6.5; including positions here would make the relation stage geometry-first.
    return {
        "kind": kind,
        "name": name,
        "parent": parent,
        "transform_q": None,
        "asset_ref": asset_ref,
        "tag": tag,
        "source": source,
    }


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self._nodes: dict[str, dict[str, Any]] = {}
        self._edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_node(
        self,
        key: str,
        *,
        kind: str,
        name: str,
        parent_key: Optional[str],
        source: str,
        tag: str = "",
        asset_ref: Optional[str] = None,
    ) -> str:
        if key in self._nodes:
            raise SceneGraphError(f"[graph.strict] duplicate graph node key {key!r}")
        parent = self._nodes.get(parent_key) if parent_key is not None else None
        if parent_key is not None and parent is None:
            raise SceneGraphError(
                f"[graph.strict] node {key!r} names missing parent {parent_key!r}"
            )
        parent_id = parent["id"] if parent is not None else None
        payload = _node_payload(
            kind=kind,
            name=name,
            parent=parent_id,
            source=source,
            tag=tag,
            asset_ref=asset_ref,
        )
        node_id = str(HashRef.of(NODE, payload))
        node = {"id": node_id, **payload}
        self._nodes[key] = node
        self.nodes.append(node)
        return node_id

    def node(self, key: str) -> dict[str, Any]:
        try:
            return self._nodes[key]
        except KeyError as exc:
            raise SceneGraphError(f"[graph.strict] unknown graph node {key!r}") from exc

    def add_edge(
        self,
        src_key: str,
        dst_key: str,
        rel: str,
        *,
        required: bool = False,
        evidence: Optional[Iterable[str]] = None,
        source: Optional[str] = None,
    ) -> None:
        if rel not in RELATIONS:
            raise SceneGraphError(
                f"[graph.strict] unregistered relation {rel!r}; "
                f"allowed: {', '.join(sorted(RELATIONS))}"
            )
        src = self.node(src_key)["id"]
        dst = self.node(dst_key)["id"]
        key = (src, dst, rel)
        row = self._edges.get(key)
        facts = _unique(str(item) for item in (evidence or []) if str(item))
        if row is None:
            row = {"src": src, "dst": dst, "rel": rel}
            if required:
                row["required"] = True
            if facts:
                row["evidence"] = facts
            if source:
                row["source"] = source
            self._edges[key] = row
            self.edges.append(row)
            return
        if required:
            row["required"] = True
        row["evidence"] = _unique(
            list(row.get("evidence") or []) + facts
        )
        if source and not row.get("source"):
            row["source"] = source


def _target_lookup(schema: UnitySchema) -> dict[str, str]:
    return {_casefold(entity.name): entity.name for entity in schema.entities}


def _mission_scene_names(
    schema: UnitySchema,
    mission: Any,
    scene_names: list[str],
    active_by_entity: Mapping[str, list[str]],
    warnings: list[str],
) -> list[str]:
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    declared = meta.get("mission_scenes") if isinstance(meta, dict) else None
    values = declared.get(mission.id) if isinstance(declared, dict) else None
    if isinstance(values, str):
        values = [values]
    if isinstance(values, list):
        selected = [scene for scene in scene_names if scene in values]
        if selected:
            return selected
        warnings.append(
            f"[graph.strict] mission {mission.id!r} names no declared scene in "
            "schema.meta.mission_scenes; falling back to target membership"
        )

    target_scenes: Optional[set[str]] = None
    for objective in mission.objectives or []:
        target = _target_lookup(schema).get(_casefold(objective.target_ref))
        if not target:
            continue
        scenes = set(active_by_entity.get(target) or [])
        target_scenes = scenes if target_scenes is None else target_scenes & scenes
    if target_scenes:
        selected = [scene for scene in scene_names if scene in target_scenes]
        if len(selected) > 1:
            warnings.append(
                f"mission {mission.id!r} is active in multiple target scenes; "
                f"using {selected[0]!r} as its primary parent and retaining all "
                "scene triggers"
            )
        return selected

    gameplay = [
        scene for scene in scene_names
        if not re.search(r"menu|loading|splash", scene, flags=re.IGNORECASE)
    ]
    selected = gameplay or scene_names[:1]
    warnings.append(
        f"mission {mission.id!r} has no uniquely declared scene; using "
        f"{selected[0]!r} as deterministic fallback"
    )
    return selected


def _path_anchors(schema: UnitySchema, role_rows: Mapping[str, Mapping[str, Any]]) -> list[str]:
    explicit = [
        name for name, row in role_rows.items()
        if row.get("role") == ROLE_PLAYER_PATH
        and bool((row.get("declared") or {}).get("has_path_points"))
    ]
    if explicit:
        return explicit
    player = next(
        (entity.name for entity in schema.entities if str(entity.tag).casefold() == "player"),
        None,
    )
    if player and role_rows.get(player, {}).get("role") == ROLE_PLAYER_PATH:
        return [player]
    return [name for name, row in role_rows.items() if row.get("role") == ROLE_PLAYER_PATH]


def _spatial_relations(
    schema: UnitySchema,
    builder: _GraphBuilder,
    role_rows: Mapping[str, Mapping[str, Any]],
    table: Mapping[str, Any],
) -> None:
    anchors = _path_anchors(schema, role_rows)
    if not anchors:
        return
    anchor_set = set(anchors)
    for name, row in role_rows.items():
        if name in anchor_set:
            continue
        role = row.get("role")
        if role not in {
            ROLE_AIRBORNE_HAZARD,
            ROLE_CHECKPOINT,
            ROLE_HAZARD,
            ROLE_NAVIGATION_MARKER,
            ROLE_SCENERY,
        }:
            continue
        entity = next(entity for entity in schema.entities if entity.name == name)
        relationship = spatial_relationship_of(entity)
        rel = (
            RELATION_ON_SURFACE
            if role == ROLE_CHECKPOINT or relationship == "route_gate"
            else RELATION_NEAR
        )
        required = role == ROLE_CHECKPOINT
        for anchor in anchors:
            builder.add_edge(
                f"entity:{name}",
                f"entity:{anchor}",
                rel,
                required=required,
                evidence=[
                    f"role={role}",
                    f"spatial_relationship_of={relationship}",
                ],
                source="schema spatial roles + route relation classifier",
            )

    # The role compiler owns the stronger objective-derived adjacency claim.
    # Merge it into the same edge rather than inventing a second graph dialect.
    for requirement in table.get("adjacency_requirements") or []:
        name = _clean_name(requirement.get("entity"))
        rel = (
            RELATION_ON_SURFACE
            if requirement.get("kind") == "on_player_path"
            else RELATION_NEAR
        )
        for anchor in requirement.get("player_path_entities") or []:
            if name not in role_rows or anchor not in role_rows or name == anchor:
                continue
            builder.add_edge(
                f"entity:{name}",
                f"entity:{anchor}",
                rel,
                required=bool(requirement.get("required")),
                evidence=requirement.get("evidence") or [],
                source="schema spatial-role adjacency requirement",
            )


def _relation_view(edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    view: dict[str, list[dict[str, Any]]] = {rel: [] for rel in sorted(RELATIONS)}
    for edge in edges:
        view[edge["rel"]].append(dict(edge))
    for rel in view:
        view[rel].sort(key=lambda edge: (
            edge["src"], edge["dst"], bool(edge.get("required")),
        ))
    return view


def _cycle(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> bool:
    graph: dict[str, list[str]] = {node: [] for node in nodes}
    for src, dst in edges:
        graph.setdefault(src, []).append(dst)
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for child in graph.get(node, []):
            if state.get(child) == 1 or (state.get(child, 0) == 0 and visit(child)):
                return True
        state[node] = 2
        return False

    return any(state.get(node, 0) == 0 and visit(node) for node in graph)


def validate_scene_graph(graph: Mapping[str, Any]) -> None:
    """Validate graph closure and the strict structural guarantees."""
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise SceneGraphError("[graph.strict] graph requires nodes and edges lists")
    by_id = {node.get("id"): node for node in nodes if isinstance(node, Mapping)}
    if len(by_id) != len(nodes) or None in by_id:
        raise SceneGraphError("[graph.strict] graph node ids are missing or duplicated")
    for node in nodes:
        parent = node.get("parent")
        if parent is not None and parent not in by_id:
            raise SceneGraphError(
                f"[graph.strict] node {node.get('name')!r} has missing parent {parent!r}"
            )
    roots = [node["id"] for node in nodes if node.get("parent") is None]
    if len(roots) != 1:
        raise SceneGraphError(
            f"[graph.strict] scene graph must have one root, found {len(roots)}"
        )

    seen_edges: set[tuple[str, str, str]] = set()
    parent_edges: list[tuple[str, str]] = []
    requires_edges: list[tuple[str, str]] = []
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise SceneGraphError("[graph.strict] graph contains a non-object edge")
        src_value, dst_value, rel_value = (
            edge.get("src"), edge.get("dst"), edge.get("rel")
        )
        if not isinstance(src_value, str) or not isinstance(dst_value, str):
            raise SceneGraphError(
                f"[graph.strict] edge endpoint is unresolved: "
                f"{src_value!r} -> {dst_value!r}"
            )
        if not isinstance(rel_value, str):
            raise SceneGraphError("[graph.strict] graph edge relation is not a string")
        src, dst, rel = src_value, dst_value, rel_value
        if src not in by_id or dst not in by_id:
            raise SceneGraphError(
                f"[graph.strict] edge endpoint is unresolved: {src!r} -> {dst!r}"
            )
        if rel not in RELATIONS:
            raise SceneGraphError(f"[graph.strict] graph contains unknown relation {rel!r}")
        key = (src, dst, rel)
        if key in seen_edges:
            raise SceneGraphError(f"[graph.strict] duplicate graph edge {key!r}")
        seen_edges.add(key)
        if rel == RELATION_PARENT_OF:
            parent_edges.append((src, dst))
        elif rel == RELATION_REQUIRES:
            requires_edges.append((src, dst))

    if _cycle((node["id"] for node in nodes), parent_edges):
        raise SceneGraphError("[graph.strict] parent_of edges contain a cycle")
    if _cycle((node["id"] for node in nodes), requires_edges):
        raise SceneGraphError("[graph.strict] requires edges contain a cycle")

    parent_edge_pairs = set(parent_edges)
    for node in nodes:
        parent = node.get("parent")
        if parent is not None and (parent, node["id"]) not in parent_edge_pairs:
            raise SceneGraphError(
                f"[graph.strict] node {node['id']} parent field has no parent_of edge"
            )

    for node in nodes:
        if node.get("kind") != "entity":
            continue
        current = node
        ancestors: set[str] = set()
        while current.get("parent") is not None:
            parent_id = current["parent"]
            if parent_id in ancestors:
                raise SceneGraphError("[graph.strict] entity parent chain contains a cycle")
            ancestors.add(parent_id)
            current = by_id[parent_id]
        if not any(by_id[ancestor].get("kind") == "scene" for ancestor in ancestors):
            raise SceneGraphError(
                f"[graph.strict] entity {node.get('name')!r} has no scene ancestor"
            )


def build_scene_graph(
    schema: UnitySchema,
    spatial_roles: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the closed, coordinate-free Slice-6.4 graph from a schema."""
    scene_names, warnings = _scene_names(schema)
    entity_names = [_clean_name(entity.name) for entity in schema.entities]
    duplicates = _duplicate_names(entity_names)
    if duplicates:
        raise SceneGraphError(
            "[graph.strict] duplicate entity names make graph endpoints ambiguous: "
            + ", ".join(duplicates)
        )
    if any(not name for name in entity_names):
        raise SceneGraphError("[graph.strict] every declared entity needs a non-empty name")

    table = _role_data(spatial_roles, schema)
    roles = _role_rows(table, entity_names)
    warnings.extend(
        str(warning) for warning in (table.get("warnings") or []) if str(warning)
    )
    builder = _GraphBuilder()
    builder.add_node(
        "root",
        kind="project",
        name=_clean_name(schema.project_name) or "UnityProject",
        parent_key=None,
        source="schema.project_name",
    )
    for scene in scene_names:
        key = f"scene:{scene}"
        builder.add_node(
            key,
            kind="scene",
            name=scene,
            parent_key="root",
            source="schema.scenes",
            tag="Scene",
        )
        builder.add_edge("root", key, RELATION_PARENT_OF, source="schema.scenes")

    active_by_entity = {
        entity.name: _scene_membership(schema, scene_names, entity.name)
        for entity in schema.entities
    }
    entity_keys: dict[str, str] = {}
    for index, entity in enumerate(schema.entities):
        active = active_by_entity[entity.name]
        if not active:
            raise SceneGraphError(
                f"[graph.strict] entity {entity.name!r} is excluded from every "
                "declared scene; add it to a SceneSpec or remove it from entities"
            )
        key = f"entity:{entity.name}"
        entity_keys[entity.name] = key
        builder.add_node(
            key,
            kind="entity",
            name=entity.name,
            parent_key=f"scene:{active[0]}",
            source=f"schema.entities[{index}]",
            tag=_clean_name(entity.tag),
        )
        builder.add_edge(
            f"scene:{active[0]}", key, RELATION_PARENT_OF,
            evidence=["primary scene ancestor"], source="schema.scene_specs",
        )
        for scene in active:
            builder.add_edge(
                f"scene:{scene}", key, RELATION_SPAWNS,
                evidence=["entity is active in scene"], source="schema.scene_specs",
            )

    aliases: dict[str, str] = {
        _casefold(schema.project_name): "root",
        **{_casefold(scene): f"scene:{scene}" for scene in scene_names},
        **{_casefold(name): key for name, key in entity_keys.items()},
    }

    mission_keys: dict[str, str] = {}
    objective_keys: dict[tuple[str, str], str] = {}
    for mission_index, mission in enumerate(schema.missions or []):
        mission_id = _clean_name(mission.id)
        if not mission_id or _casefold(mission_id) in mission_keys:
            raise SceneGraphError(
                f"[graph.strict] mission ids must be unique and non-empty: {mission_id!r}"
            )
        mission_scenes = _mission_scene_names(
            schema, mission, scene_names, active_by_entity, warnings
        )
        primary_scene = mission_scenes[0]
        mission_key = f"mission:{mission_id}"
        mission_keys[_casefold(mission_id)] = mission_key
        builder.add_node(
            mission_key,
            kind="mission",
            name=mission_id,
            parent_key=f"scene:{primary_scene}",
            source=f"schema.missions[{mission_index}]",
        )
        builder.add_edge(
            f"scene:{primary_scene}", mission_key, RELATION_PARENT_OF,
            evidence=["mission primary scene"], source="schema.missions",
        )
        for scene in mission_scenes:
            builder.add_edge(
                f"scene:{scene}", mission_key, RELATION_TRIGGERS,
                evidence=["mission is active in scene"], source="schema.missions",
            )
        previous_key: Optional[str] = None
        for objective_index, objective in enumerate(mission.objectives or []):
            objective_id = _clean_name(objective.id)
            if not objective_id or (mission_id.casefold(), objective_id.casefold()) in objective_keys:
                raise SceneGraphError(
                    f"[graph.strict] objective ids must be unique within mission "
                    f"{mission_id!r}: {objective_id!r}"
                )
            objective_key = f"objective:{mission_id}:{objective_id}"
            objective_keys[(mission_id.casefold(), objective_id.casefold())] = objective_key
            builder.add_node(
                objective_key,
                kind="objective",
                name=objective_id,
                parent_key=mission_key,
                source=f"schema.missions[{mission_index}].objectives[{objective_index}]",
            )
            builder.add_edge(
                mission_key, objective_key, RELATION_PARENT_OF,
                evidence=["objective belongs to mission"], source="schema.missions",
            )
            if previous_key is not None:
                builder.add_edge(
                    objective_key, previous_key, RELATION_REQUIRES,
                    required=True,
                    evidence=["objectives are ordered in schema.missions"],
                    source="schema.missions",
                )
            previous_key = objective_key
            target_entity_name = _target_lookup(schema).get(
                _casefold(objective.target_ref)
            )
            if target_entity_name is not None:
                builder.add_edge(
                    objective_key, f"entity:{target_entity_name}", RELATION_TARGETS,
                    required=not bool(objective.optional),
                    evidence=[f"objective.target_ref={objective.target_ref!r}"],
                    source="schema.missions.objectives",
                )
            else:
                warnings.append(
                    f"[graph.strict] objective {mission_id}/{objective_id} has no "
                    f"unique declared entity target for target_ref="
                    f"{objective.target_ref!r}; no target edge was invented"
                )
            aliases[_casefold(objective_id)] = objective_key

    if schema.wiring is not None:
        for dependency in schema.wiring.dependencies or []:
            src_key = aliases.get(_casefold(dependency.source))
            dst_key = aliases.get(_casefold(dependency.target))
            if not src_key or not dst_key:
                warnings.append(
                    f"[graph.strict] wiring dependency {dependency.source!r} -> "
                    f"{dependency.target!r} has an unresolved graph endpoint; skipped"
                )
                continue
            rel = _WIRING_RELATIONS.get(str(dependency.type).casefold())
            if rel is None:
                warnings.append(
                    f"[graph.strict] wiring type {dependency.type!r} has no closed "
                    "graph relation; skipped"
                )
                continue
            builder.add_edge(
                src_key, dst_key, rel,
                evidence=[f"wiring.type={dependency.type!r}"],
                source="schema.wiring",
            )

    _spatial_relations(schema, builder, roles, table)
    graph_hash = chash(SCENE, {"nodes": builder.nodes, "edges": builder.edges})
    graph = {
        "format": SCENE_GRAPH_FORMAT,
        "stage": SCENE_GRAPH_STAGE,
        "status": "closed",
        "authority": {
            "llm_called_here": False,
            "schema_semantics_authority": True,
            "image_spatial_evidence_used_here": False,
            "asset_bindings_used_here": False,
            "world_coordinates_emitted_here": False,
            "entities_invented_here": False,
        },
        "inputs": {
            "schema_sha256": _semantic_schema_hash(schema),
            "project_name": schema.project_name,
            "spatial_roles_sha256": sha256_bytes(
                json.dumps(table, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
            ),
        },
        "relation_vocabulary": sorted(RELATIONS),
        "nodes": builder.nodes,
        "edges": builder.edges,
        "relation_view": _relation_view(builder.edges),
        "graph_hash": graph_hash,
        "warnings": warnings,
        "abstention_count": sum("abstain" in warning for warning in warnings),
    }
    validate_scene_graph(graph)
    return graph


def _dot_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def render_dot(graph: Mapping[str, Any]) -> str:
    """Render the same canonical graph as a small inspection-only DOT view."""
    validate_scene_graph(graph)
    lines = ["digraph SceneGraph {", "  rankdir=LR;"]
    for node in graph["nodes"]:
        label = f"{node['kind']}\\n{node['name']}"
        lines.append(f'  "{_dot_escape(node["id"])}" [label="{_dot_escape(label)}"];')
    for edge in graph["edges"]:
        label = edge["rel"] + ("*" if edge.get("required") else "")
        lines.append(
            f'  "{_dot_escape(edge["src"])}" -> "{_dot_escape(edge["dst"])}" '
            f'[label="{_dot_escape(label)}"];'
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_scene_graph(
    schema_path: Path,
    output: Path,
    manifest: Optional[Path] = None,
    spatial_roles_path: Optional[Path] = None,
    dot_output: Optional[Path] = None,
) -> Path:
    """Freeze a schema-derived graph and its provenance manifest."""
    if not schema_path.is_file():
        raise SceneGraphError(f"schema not found: {schema_path}")
    raw = schema_path.read_bytes()
    try:
        schema = UnitySchema.model_validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SceneGraphError(f"schema is not valid UnitySchema JSON: {schema_path}") from exc

    roles: Optional[Mapping[str, Any]] = None
    role_raw: Optional[bytes] = None
    if spatial_roles_path is not None:
        if not spatial_roles_path.is_file():
            raise SceneGraphError(f"spatial-role sidecar not found: {spatial_roles_path}")
        role_raw = spatial_roles_path.read_bytes()
        try:
            roles = json.loads(role_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SceneGraphError(
                f"spatial-role sidecar is not valid JSON: {spatial_roles_path}"
            ) from exc

    graph = build_scene_graph(schema, roles)
    output_bytes = (json.dumps(graph, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path = manifest or output.with_name(output.stem + ".manifest.json")
    manifest_data: dict[str, Any] = {
        "format": SCENE_GRAPH_MANIFEST_FORMAT,
        "stage": SCENE_GRAPH_STAGE,
        "status": "complete",
        "authority": graph["authority"],
        "inputs": {
            "schema": {"path": str(schema_path), "sha256": sha256_bytes(raw)},
            "schema_semantic_sha256": graph["inputs"]["schema_sha256"],
        },
        "outputs": {
            "scene_graph": {
                "path": str(output),
                "sha256": sha256_bytes(output_bytes),
                "graph_hash": graph["graph_hash"],
            }
        },
        "node_count": len(graph["nodes"]),
        "edge_count": len(graph["edges"]),
        "warnings": graph["warnings"],
    }
    if role_raw is not None and spatial_roles_path is not None:
        manifest_data["inputs"]["spatial_roles"] = {
            "path": str(spatial_roles_path),
            "sha256": sha256_bytes(role_raw),
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(output_bytes)
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if dot_output is not None:
        dot_output.parent.mkdir(parents=True, exist_ok=True)
        dot_output.write_text(render_dot(graph), encoding="utf-8")
    return manifest_path


__all__ = [
    "RELATIONS",
    "SCENE_GRAPH_FORMAT",
    "SCENE_GRAPH_MANIFEST_FORMAT",
    "SCENE_GRAPH_STAGE",
    "SceneGraphError",
    "build_scene_graph",
    "render_dot",
    "validate_scene_graph",
    "write_scene_graph",
]
