"""Deterministic Slice 6.2 concept/schema spatial-role compiler.

This module is the schema-side half of the hybrid spatial layout plan.  It
reads only the validated Unity schema, its declared missions, route/path
fields, the deterministic layout archetype, and the Concept lineage already
stored in ``schema.meta``.  It does not inspect image pixels, choose assets,
call an LLM, add entities, or emit Unity coordinates.

The output is a closed sidecar: every declared entity gets exactly one role
from the fixed vocabulary, including ``abstain`` when the schema does not
prove a role.  Gameplay adjacency requirements are recorded separately so a
later fusion/realization pass cannot mistake an image landmark for a declared
checkpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from .unity_layout_archetype import (
    LayoutStrictError,
    is_hazard_entity,
    is_route_gate_entity,
    pick_player_entity,
    select_archetype,
    ui_element_of,
    is_ui_backdrop,
)
from .unity_schema import UnitySchema
from .unity_spatial_identity import semantic_schema_hash
from .visual_reasoning_compiler import sha256_bytes


SPATIAL_ROLES_FORMAT = "visual-reasoning-compiler/schema-spatial-roles/v1"
SPATIAL_ROLES_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/schema-spatial-roles-manifest/v1"
)
SPATIAL_ROLES_STAGE = "6.2_schema_spatial_roles"

ROLE_PLAYER_PATH = "player_path"
ROLE_CHECKPOINT = "checkpoint"
ROLE_HAZARD = "hazard"
ROLE_NAVIGATION_MARKER = "navigation_marker"
ROLE_SCENERY = "scenery"
ROLE_AIRBORNE_HAZARD = "airborne_hazard"
ROLE_UI_ONLY = "ui_only"
ROLE_ABSTAIN = "abstain"

SPATIAL_ROLES = frozenset({
    ROLE_PLAYER_PATH,
    ROLE_CHECKPOINT,
    ROLE_HAZARD,
    ROLE_NAVIGATION_MARKER,
    ROLE_SCENERY,
    ROLE_AIRBORNE_HAZARD,
    ROLE_UI_ONLY,
    ROLE_ABSTAIN,
})

_ROUTE_KINDS = frozenset({
    "path", "route", "road", "track", "rail", "course", "circuit",
    "raceline", "race_line", "spline", "river", "channel", "trail",
})
_ROUTE_WORDS = (
    "path", "route", "road", "track", "rail", "course", "circuit",
    "race line", "raceline", "spline", "river", "channel", "trail",
)
_NAVIGATION_KINDS = frozenset({
    "marker", "waypoint", "beacon", "sign", "signpost", "pylon",
    "buoy", "navigation_marker",
})
_NAVIGATION_WORDS = (
    "waypoint", "waypoints", "navigation marker", "navigation_marker",
    "marker", "markers", "beacon", "beacons", "signpost", "signposts",
    "direction sign", "arrow sign", "pylon", "pylons", "buoy", "buoys",
)
_CHECKPOINT_WORDS = (
    "checkpoint", "checkpoints", "finish line", "finish arch", "finish gate",
    "start line", "start gate", "gate", "gates",
)
_HAZARD_WORDS = (
    "hazard", "hazards", "danger", "dangerous", "trap", "traps", "mine",
    "mines", "spike", "spikes", "laser", "lasers", "lava", "fire",
    "flame", "flames", "barrier", "barriers", "obstacle", "obstacles",
)
_AIRBORNE_WORDS = (
    "airborne", "aerial", "flying", "flyer", "sky", "air", "altitude",
    "hover", "hovering", "floating", "drifting", "midair", "mid-air",
)
_SCENERY_KINDS = frozenset({
    "prop", "decor", "decoration", "environment", "terrain", "ground",
    "floor", "road", "building", "tree", "rock", "plant", "background",
    "landscape", "water", "surface", "wall", "platform",
})
_SCENERY_TAGS = frozenset({
    "scenery", "decor", "decoration", "environment", "background", "terrain",
    "ground", "surface", "world",
})
_SCENERY_WORDS = (
    "scenery", "decoration", "decor", "background", "landscape", "terrain",
    "ground", "floor", "tree", "trees", "rock", "rocks", "boulder",
    "boulders", "building", "buildings", "house", "houses", "wall", "walls",
    "mountain", "mountains", "island", "islands", "water", "river", "lake",
    "ocean", "sea", "reef", "coral", "cloud", "clouds", "environment",
)
_ROLE_PHRASES = (
    (ROLE_CHECKPOINT, _CHECKPOINT_WORDS),
    (ROLE_HAZARD, _HAZARD_WORDS),
    (ROLE_NAVIGATION_MARKER, _NAVIGATION_WORDS),
    (ROLE_SCENERY, _SCENERY_WORDS),
)
_CONCEPT_STOPWORDS = frozenset({
    "a", "an", "and", "at", "for", "from", "game", "group", "in", "of",
    "on", "prop", "scene", "the", "this", "to", "world",
})


class SpatialRoleError(ValueError):
    """Raised when a role sidecar input cannot be read or validated."""


def _camel_words(value: Any) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(value or ""))
    return re.sub(r"[^a-zA-Z0-9_ -]+", " ", spaced).lower()


def _hits(text: str, needles: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [needle for needle in needles if re.search(
        r"(?<![a-z0-9])" + re.escape(needle.lower()) + r"(?![a-z0-9])",
        lowered,
    )]


def _entity_text(entity: Any) -> str:
    return _camel_words(" ".join(str(getattr(entity, field, "") or "")
                                  for field in (
                                      "name", "kind", "tag", "model_hint",
                                      "behavior",
                                  )))


def _entity_name_tokens(entity: Any) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", _camel_words(entity.name))
        if len(token) > 1 and token not in _CONCEPT_STOPWORDS
    }


def _concept_evidence(entity: Any, concept: str, needles: Iterable[str]) -> list[str]:
    """Return role words from Concept prose only when it names this entity.

    Concept prose is lineage evidence, not an independent entity inventory.
    Requiring all meaningful name tokens in one sentence prevents a generic
    Concept mention such as "the player reaches a marker" from assigning the
    marker role to every entity in the schema.
    """
    if not concept:
        return []
    tokens = _entity_name_tokens(entity)
    if not tokens:
        return []
    found: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", concept.lower()):
        words = set(re.findall(r"[a-z0-9]+", _camel_words(sentence)))
        if not tokens.issubset(words):
            continue
        for hit in _hits(sentence, needles):
            if hit not in found:
                found.append(hit)
    return found


def _meta_lineage(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, dict):
        meta = {}
    concept = str(meta.get("concept") or "")
    user_prompt = str(meta.get("user_prompt") or "")
    return {
        "concept_present": bool(concept.strip()),
        "concept_sha256": sha256_bytes(concept.encode("utf-8")) if concept else None,
        "concept_source": "schema.meta.concept" if concept else None,
        "user_prompt_present": bool(user_prompt.strip()),
        "user_prompt_sha256": (
            sha256_bytes(user_prompt.encode("utf-8")) if user_prompt else None
        ),
        "user_prompt_source": "schema.meta.user_prompt" if user_prompt else None,
    }


def _schema_hash(schema: UnitySchema) -> str:
    return semantic_schema_hash(schema)


def _route_field_evidence(entity: Any) -> list[str]:
    evidence: list[str] = []
    placement = str(getattr(entity, "placement", "") or "").lower().strip()
    path_points = list(getattr(entity, "path_points", []) or [])
    kind = str(getattr(entity, "kind", "") or "").lower().strip()
    text = _entity_text(entity)
    if placement == "path":
        evidence.append("entity.placement='path'")
    if path_points:
        evidence.append("entity.path_points is declared")
    if kind in _ROUTE_KINDS:
        evidence.append(f"entity.kind={kind!r} is a route kind")
    for hit in _hits(text, _ROUTE_WORDS):
        marker = f"declared route vocabulary {hit!r}"
        if marker not in evidence:
            evidence.append(marker)
    return evidence


def _has_route_fields(entity: Any) -> bool:
    return bool(_route_field_evidence(entity))


def _exact_entity_name(target_ref: str, entities: list[Any]) -> tuple[Optional[Any], str]:
    target = str(target_ref or "").strip()
    if not target:
        return None, "objective has no declared target_ref"
    folded = target.casefold()
    matches = [e for e in entities if str(e.name).casefold() == folded]
    if len(matches) == 1:
        return matches[0], f"objective.target_ref={target!r}"
    if len(matches) > 1:
        return None, f"objective target_ref={target!r} matches duplicate entity names"
    return None, f"objective target_ref={target!r} is not a declared schema entity"


def _objective_rows(schema: UnitySchema, entities: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for mission in schema.missions or []:
        for objective in mission.objectives or []:
            target, why = _exact_entity_name(objective.target_ref, entities)
            row = {
                "mission": mission.id,
                "objective": objective.id,
                "type": objective.type,
                "target_ref": objective.target_ref,
                "target_entity": target.name if target is not None else None,
                "status": "bound" if target is not None else "abstain",
                "evidence": [why],
            }
            if target is not None and objective.type == "reach":
                row["spatial_requirement"] = "target must be reachable from player_path"
            rows.append(row)
    return rows


def _objective_targets_for(entities: list[Any], objective_rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {str(e.name): [] for e in entities}
    for row in objective_rows:
        target = row.get("target_entity")
        if target in out:
            out[target].append(
                f"{row['mission']}/{row['objective']} ({row['type']})"
            )
    return out


def _role_for_entity(
    schema: UnitySchema,
    entity: Any,
    *,
    player: Any,
    medium: Optional[str],
    concept: str,
    objective_rows: list[dict[str, Any]],
) -> tuple[str, list[str], Optional[str]]:
    """Return one closed role, evidence, and an abstention reason."""
    text = _entity_text(entity)
    kind = str(getattr(entity, "kind", "") or "").lower().strip()
    tag = str(getattr(entity, "tag", "") or "").lower().strip()
    embodied = str(schema.locomotion or "walk").lower() != "none"
    evidence: list[str] = []

    if not embodied:
        element = ui_element_of(entity)
        if element is not None:
            return ROLE_UI_ONLY, [f"locomotion='none' + ui element={element!r}"], None
        if is_ui_backdrop(entity):
            return ROLE_UI_ONLY, ["locomotion='none' + declared UI backdrop"], None
        return (
            ROLE_ABSTAIN,
            [],
            "non-embodied schema does not prove that this entity is UI-only",
        )

    if player is entity:
        return ROLE_PLAYER_PATH, ["deterministic player binding"] + _route_field_evidence(entity), None

    if is_route_gate_entity(entity):
        evidence.append("declared route-gate/checkpoint predicate")
    checkpoint_concept = _concept_evidence(entity, concept, _CHECKPOINT_WORDS)
    if checkpoint_concept:
        evidence.append("Concept names this entity with checkpoint vocabulary: "
                        + ", ".join(checkpoint_concept))
    target_rows = [row for row in objective_rows
                   if row.get("target_entity") == entity.name]
    for row in target_rows:
        if row.get("type") == "reach" and _hits(
                f"{row.get('target_ref', '')} {entity.name}", _CHECKPOINT_WORDS):
            evidence.append(
                f"reach objective {row['mission']}/{row['objective']} targets checkpoint vocabulary"
            )
    if evidence:
        return ROLE_CHECKPOINT, evidence, None

    hazard = is_hazard_entity(entity)
    hazard_hits = _hits(text, _HAZARD_WORDS)
    hazard_concept = _concept_evidence(entity, concept, _HAZARD_WORDS)
    if hazard:
        evidence.append("declared hazard/enemy/trap kind or tag")
    if hazard_hits:
        evidence.append("declared hazard vocabulary: " + ", ".join(hazard_hits))
    if hazard_concept:
        evidence.append("Concept names this entity with hazard vocabulary: "
                        + ", ".join(hazard_concept))
    if hazard or hazard_hits or hazard_concept:
        airborne_hits = _hits(text, _AIRBORNE_WORDS)
        if medium == "air" and not _has_ground_signal(entity):
            evidence.append("layout medium='air' and no declared ground signal")
            return ROLE_AIRBORNE_HAZARD, evidence, None
        if airborne_hits:
            evidence.append("declared airborne vocabulary: " + ", ".join(airborne_hits))
            return ROLE_AIRBORNE_HAZARD, evidence, None
        return ROLE_HAZARD, evidence, None

    navigation_hits = _hits(text, _NAVIGATION_WORDS)
    if kind in _NAVIGATION_KINDS:
        navigation_hits.insert(0, f"entity.kind={kind!r} is a navigation kind")
    navigation_concept = _concept_evidence(entity, concept, _NAVIGATION_WORDS)
    if navigation_concept:
        navigation_hits.append(
            "Concept names this entity with navigation vocabulary: "
            + ", ".join(navigation_concept)
        )
    if navigation_hits:
        return ROLE_NAVIGATION_MARKER, navigation_hits, None

    route_evidence = _route_field_evidence(entity)
    if route_evidence and (
        kind in _ROUTE_KINDS or _hits(text, _ROUTE_WORDS)
    ):
        return ROLE_PLAYER_PATH, route_evidence, None

    scenery_hits = []
    if kind in _SCENERY_KINDS:
        scenery_hits.append(f"entity.kind={kind!r} is an explicit scenery kind")
    if tag in _SCENERY_TAGS:
        scenery_hits.append(f"entity.tag={tag!r} is an explicit scenery tag")
    scenery_hits.extend("declared scenery vocabulary: " + hit
                        for hit in _hits(text, _SCENERY_WORDS))
    scenery_concept = _concept_evidence(entity, concept, _SCENERY_WORDS)
    if scenery_concept:
        scenery_hits.append(
            "Concept names this entity with scenery vocabulary: "
            + ", ".join(scenery_concept)
        )
    if scenery_hits:
        return ROLE_SCENERY, scenery_hits, None

    return (
        ROLE_ABSTAIN,
        [],
        "declared schema content does not prove a closed spatial role",
    )


def _has_ground_signal(entity: Any) -> bool:
    text = _entity_text(entity)
    kind = str(getattr(entity, "kind", "") or "").lower().strip()
    tag = str(getattr(entity, "tag", "") or "").lower().strip()
    return kind in {"ground", "terrain", "floor", "road", "track", "platform"} \
        or tag in {"ground", "terrain", "floor", "surface"} \
        or bool(_hits(text, ("ground", "floor", "terrain", "seabed", "riverbed")))


def _archetype_context(schema: UnitySchema) -> tuple[dict[str, Any], list[str], Optional[str]]:
    warnings: list[str] = []
    try:
        archetype = select_archetype(schema)
    except LayoutStrictError as exc:
        warnings.append(
            "[spatial.strict] layout archetype could not be selected; "
            f"medium-dependent roles abstain: {exc}"
        )
        return {
            "name": None,
            "motion_axis": None,
            "medium": None,
            "goal_topology": None,
            "evidence": None,
        }, warnings, None
    return {
        "name": archetype.name,
        "motion_axis": archetype.motion_axis,
        "medium": archetype.medium,
        "goal_topology": archetype.goal_topology,
        "evidence": dict(archetype.evidence),
    }, warnings, archetype.medium


def _adjacency_requirements(
    role_rows: list[dict[str, Any]],
    objective_rows: list[dict[str, Any]],
    player_path_entities: list[str],
    medium: Optional[str],
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for row in role_rows:
        role = row["role"]
        if role not in {ROLE_CHECKPOINT, ROLE_NAVIGATION_MARKER}:
            continue
        targeted = [o for o in objective_rows if o.get("target_entity") == row["entity"]]
        is_reach_target = any(o.get("type") == "reach" for o in targeted)
        requirements.append({
            "kind": "on_player_path" if role == ROLE_CHECKPOINT else "reachable_navigation_marker",
            "entity": row["entity"],
            "required": is_reach_target or role == ROLE_CHECKPOINT,
            "status": "bound" if player_path_entities else "abstain",
            "player_path_entities": list(player_path_entities),
            "evidence": (
                ["checkpoint role requires adjacency to player_path"]
                if role == ROLE_CHECKPOINT
                else ["navigation marker must remain reachable from player_path"]
            ),
            "abstention": (
                None if player_path_entities
                else "no declared player_path entity exists"
            ),
            "medium": medium,
        })
    return requirements


def derive_spatial_roles(schema: UnitySchema) -> dict[str, Any]:
    """Derive the closed Slice-6.2 role table without mutating ``schema``."""
    entities = list(schema.entities or [])
    lineage = _meta_lineage(getattr(schema, "meta", None))
    warnings: list[str] = []
    if not lineage["concept_present"]:
        warnings.append(
            "schema.meta.concept is absent; roles use declared schema content only"
        )

    archetype, arch_warnings, medium = _archetype_context(schema)
    warnings.extend(arch_warnings)
    player, player_why = pick_player_entity(entities)
    if str(schema.locomotion or "walk") != "none" and player is None:
        warnings.append(
            "[spatial.strict] embodied schema has no deterministic player entity; "
            "player_path adjacency abstains"
        )

    concept = str((schema.meta or {}).get("concept") or "") \
        if isinstance(getattr(schema, "meta", None), dict) else ""
    objective_rows = _objective_rows(schema, entities)
    role_rows: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        role, evidence, abstention = _role_for_entity(
            schema,
            entity,
            player=player,
            medium=medium,
            concept=concept,
            objective_rows=objective_rows,
        )
        role_rows.append({
            "entity": entity.name,
            "entity_index": index,
            "role": role,
            "evidence": evidence,
            "abstention": abstention,
            "declared": {
                "kind": entity.kind,
                "tag": entity.tag,
                "placement": entity.placement,
                "has_path_points": bool(entity.path_points),
                "path_width": entity.path_width,
                "elevation": entity.elevation,
            },
        })

    player_path_entities = [
        row["entity"] for row in role_rows if row["role"] == ROLE_PLAYER_PATH
    ]
    duplicate_names = sorted({
        name for name in (str(entity.name) for entity in entities)
        if sum(1 for other in entities if str(other.name) == name) > 1
    })
    if duplicate_names:
        warnings.append(
            "[spatial.strict] duplicate entity names make name-based fusion "
            "ambiguous: " + ", ".join(duplicate_names)
        )
    for row in objective_rows:
        if row["status"] == "abstain":
            warnings.append(
                f"[spatial.strict] objective {row['mission']}/{row['objective']} "
                f"has no unique declared target entity"
            )

    adjacency = _adjacency_requirements(
        role_rows, objective_rows, player_path_entities, medium
    )
    if not player_path_entities and adjacency:
        warnings.append(
            "[spatial.strict] adjacency requirements exist but no player_path "
            "role was proven"
        )

    abstention_count = sum(
        1 for row in role_rows if row["role"] == ROLE_ABSTAIN
    ) + sum(1 for row in objective_rows if row["status"] == "abstain") \
        + sum(1 for row in adjacency if row["status"] == "abstain")
    return {
        "format": SPATIAL_ROLES_FORMAT,
        "stage": SPATIAL_ROLES_STAGE,
        "status": "closed",
        "authority": {
            "llm_called_here": False,
            "schema_semantics_authority": True,
            "concept_lineage_read_here": lineage["concept_present"],
            "image_spatial_evidence_used_here": False,
            "asset_bindings_used_here": False,
            "world_coordinates_emitted_here": False,
            "entities_invented_here": False,
        },
        "inputs": {
            "schema_sha256": _schema_hash(schema),
            "project_name": schema.project_name,
            "concept_lineage": lineage,
            "locomotion": schema.locomotion,
            "archetype": archetype,
        },
        "role_vocabulary": sorted(SPATIAL_ROLES),
        "role_table": role_rows,
        "objectives": objective_rows,
        "adjacency_requirements": adjacency,
        "warnings": warnings,
        "abstention_count": abstention_count,
        "player_binding": {
            "entity": player.name if player is not None else None,
            "evidence": player_why or None,
        },
    }


def write_spatial_roles(
    schema_path: Path,
    output: Path,
    manifest: Optional[Path] = None,
) -> Path:
    """Read a saved schema and freeze the role table plus its hash manifest."""
    if not schema_path.is_file():
        raise SpatialRoleError(f"schema not found: {schema_path}")
    raw = schema_path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
        schema = UnitySchema.model_validate(data)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise SpatialRoleError(f"schema is not valid UnitySchema JSON: {schema_path}") from exc

    table = derive_spatial_roles(schema)
    output_bytes = (json.dumps(table, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path = manifest or output.with_name(output.stem + ".manifest.json")
    manifest_data = {
        "format": SPATIAL_ROLES_MANIFEST_FORMAT,
        "stage": SPATIAL_ROLES_STAGE,
        "status": "complete",
        "authority": table["authority"],
        "inputs": {
            "schema": {"path": str(schema_path), "sha256": sha256_bytes(raw)},
            "schema_semantic_sha256": table["inputs"]["schema_sha256"],
        },
        "outputs": {
            "spatial_roles": {
                "path": str(output),
                "sha256": sha256_bytes(output_bytes),
            }
        },
        "abstention_count": table["abstention_count"],
        "warnings": table["warnings"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(output_bytes)
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


__all__ = [
    "ROLE_ABSTAIN",
    "ROLE_AIRBORNE_HAZARD",
    "ROLE_CHECKPOINT",
    "ROLE_HAZARD",
    "ROLE_NAVIGATION_MARKER",
    "ROLE_PLAYER_PATH",
    "ROLE_SCENERY",
    "ROLE_UI_ONLY",
    "SPATIAL_ROLES",
    "SPATIAL_ROLES_FORMAT",
    "SPATIAL_ROLES_MANIFEST_FORMAT",
    "SPATIAL_ROLES_STAGE",
    "SpatialRoleError",
    "derive_spatial_roles",
    "write_spatial_roles",
]
