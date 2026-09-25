"""Stable semantic identity for the deterministic spatial sidecars.

Spatial roles, relation graphs, and image/schema fusion are derived from the
schema's meaning.  World realization then writes generated geometry back into
that same schema, and the generator may add measured positions or cover-point
metadata later.  Hashing those generated fields would make a valid sidecar
invalidate itself during the normal Step-5/Step-6 layout handoff.

This module removes only fields owned by deterministic realization from the
identity payload.  Authored route points, scene membership, objectives, asset
bindings, and other semantic inputs remain part of the hash.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .unity_schema import UnitySchema
from .visual_reasoning_compiler import sha256_bytes


_GENERATED_ENTITY_FIELDS = (
    "origin_offset",
    "group_radius",
    "elevation",
    "instance_positions",
    "instance_yaws",
    # The generator deterministically backfills or reconciles runtime
    # behaviour after spatial preparation.  Behaviour is gameplay/codegen
    # state, not a spatial constraint, so it must not invalidate a frozen
    # 6.3 fusion artifact during the final post-generation freeze.
    "behavior",
    # The post-schema asset-query enrichment may add this field while the
    # generator is resolving models.  It is resolver metadata, not spatial
    # meaning; including it would make a valid 6.3 fusion stale before the
    # model-aware 6.5 relayout consumes it.
    "sketchfab_query",
)
_GENERATED_META_FIELDS = (
    "cover_points",
    "layout_archetype",
    "layout_archetype_decision",
    "spatial_primitive",
    "wiring",
    "entity_model_paths",
)
_GENERATED_SCHEMA_FIELDS = (
    # Generator sanitization and composition can rewrite these codegen-only
    # collections after 6.3 prepares its fusion artifact.  They do not affect
    # entity roles, relations, or world placement.
    "scripts",
)


def semantic_schema_payload(schema: UnitySchema) -> dict[str, Any]:
    """Return the schema payload whose hash represents spatial semantics."""
    payload = copy.deepcopy(schema.model_dump(mode="json"))
    for entity in payload.get("entities", []):
        if isinstance(entity, dict):
            for field in _GENERATED_ENTITY_FIELDS:
                entity.pop(field, None)

    # ``compute_layout`` creates a default WorldSpec/region envelope when the
    # schema did not author one, and appends derived regions on later passes.
    # World realization is the authority for that geometry, so the whole
    # mutable world envelope is intentionally outside the sidecar identity.
    payload.pop("world_spec", None)
    for field in _GENERATED_SCHEMA_FIELDS:
        payload.pop(field, None)

    meta = payload.get("meta")
    if isinstance(meta, dict):
        for field in _GENERATED_META_FIELDS:
            meta.pop(field, None)
    return payload


def semantic_schema_hash(schema: UnitySchema) -> str:
    """Hash stable schema meaning, excluding generated spatial realization."""
    return sha256_bytes(json.dumps(
        semantic_schema_payload(schema),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8"))


__all__ = ["semantic_schema_hash", "semantic_schema_payload"]
