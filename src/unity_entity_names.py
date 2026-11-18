"""Step 5 entity names: one name, one entity group, or a named abstention.

The scene graph (``src/scene_graph.py``) refuses a schema whose entity names
collide, because every graph endpoint is a name: ``[graph.strict] duplicate
entity names make graph endpoints ambiguous``. Identity there is the stripped,
casefolded name, and this module uses the same rule (:func:`entity_identity`).

The Step 3 fill writes scene content scene by scene, and a local model reuses a
plain noun. Live, on run 04 of the neon_fugitive E2E batch, it wrote two
different ``Debris`` groups: ``entities[1]`` (scrap, x20), listed in
``ActivationAlley``, and ``entities[15]`` (a flooded pile, x15), listed in
``SlumFlooded``. The schema-cap clamp (``unity_schema_clamp``) only de-duplicates
a list that is over its cap, so nothing below the cap ever looked, and the run
died in the graph.

:func:`disambiguate_entity_names` is the one owner of that repair. It runs first
in ``unity_pipeline._check_consistency``, so the live Step 5 and the
``unity-validate`` replay both get it. Each set of entities sharing an identity
is handled in three tiers:

1. **Identical copies.** Two entries equal in everything but the layout-owned
   ``instance_positions``/``instance_yaws`` (and the case of the name) are the
   same group said twice. The first is kept and the rest are dropped: the
   clamp's own "first wins" rule, applied below the cap.
2. **Positional, scene-qualified rename.** N distinct copies listed in exactly
   N declared ``scene_specs``, once each: the fill wrote copy k while writing
   the k-th of those scenes, so copy k (schema order) maps to the k-th listing
   spec (``scene_specs`` order). Every copy becomes ``<Scene><Name>``
   (``ActivationAlleyDebris``, ``SlumFloodedDebris``) and that spec's entry is
   rewritten, so no copy silently keeps the bare name. Applied only when no new
   name collides and nothing outside ``scene_specs`` refers to the bare name
   (a mission ``target_ref``, a ``characters[].model_ref``, a wiring edge, an
   atom parameter, ``extensions``...), because one reference cannot be split
   between two groups. The one exception is a layout-derived
   ``world_spec.regions`` row: ``unity_layout`` writes one per group, named
   after it, bounded by its ``origin_offset`` +/- ``group_radius``. Such a row
   is compiler output derived from the copy itself, and it is re-derived per
   copy. On the live path, Step 5 runs this before any layout exists.
3. **Anything else** abstains loudly. Names stay as authored, the record holds
   the copies, the listings and the reason, and the scene graph still raises,
   now quoting that reason.

Everything is recorded in ``schema.meta["entity_names"]``. Deterministic and
LLM-free (Step 5). ``extensions`` content is never renamed: a bare name inside
it is a reference, and the set abstains.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

from .unity_schema import WorldRegion

ENTITY_NAMES_META_KEY = "entity_names"
ENTITY_NAMES_FORMAT = "unity-entity-names/v1"
ENTITY_NAMES_STAGE = "5_entity_names"

# Written by compute_layout, never by Step 3. Two copies that differ only here
# are one group laid out twice.
_LAYOUT_OWNED_FIELDS: frozenset[str] = frozenset({"instance_positions", "instance_yaws"})

# EntitySpec fields that describe the group itself and never point at another
# group. Every other entity field (its atoms, above all) is searched for
# references like the rest of the schema.
_DESCRIPTIVE_ENTITY_FIELDS: frozenset[str] = frozenset({
    "name", "kind", "shape", "placement", "color_role", "tag", "behavior",
    "model_hint", "sketchfab_query",
})

# Not searched for references: compiler-owned records (meta), the scene
# namespace (a scene name is not an entity reference) and the two places this
# module handles itself (scene_specs[].entities, world_spec.regions).
_SKIPPED_TOP_LEVEL: frozenset[str] = frozenset({"meta", "scenes"})

_BOUNDS_TOLERANCE = 1e-6


def entity_identity(name: Any) -> str:
    """The scene graph's identity rule: stripped, casefolded."""
    return str(name or "").strip().casefold()


def _scene_qualified(scene: str, name: str) -> str:
    bare = name.strip()
    return f"{scene}{bare[:1].upper()}{bare[1:]}"


def _content(entity: Any) -> dict[str, Any]:
    dump = entity.model_dump(mode="python")
    for field in _LAYOUT_OWNED_FIELDS | {"name"}:
        dump.pop(field, None)
    return dump


def _describe(index: int, entity: Any) -> dict[str, Any]:
    return {"index": index, "name": entity.name, "kind": entity.kind,
            "tag": entity.tag, "count": entity.count,
            "model_hint": entity.model_hint}


def _copy_label(row: dict[str, Any]) -> str:
    return (f"entities[{row['index']}] {row['name']!r} (kind={row['kind']!r}, "
            f"count={row['count']}, model_hint={row['model_hint']!r})")


def _walk_strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def _bare_references(schema: Any, identity: str) -> list[str]:
    """Paths outside scene_specs[].entities and world_spec.regions whose whole
    string value is the bare name. Each is a reference this pass cannot split.
    """
    dump = schema.model_dump(mode="python")
    paths: list[str] = []
    for field, value in dump.items():
        if field in _SKIPPED_TOP_LEVEL or value is None:
            continue
        if field == "entities":
            for i, entity in enumerate(value or []):
                for key, item in entity.items():
                    if key in _DESCRIPTIVE_ENTITY_FIELDS:
                        continue
                    paths.extend(p for p, s in _walk_strings(item, f"entities[{i}].{key}")
                                 if entity_identity(s) == identity)
            continue
        if field == "scene_specs":
            for i, spec in enumerate(value or []):
                for key, item in spec.items():
                    if key in ("entities", "name"):
                        continue
                    paths.extend(p for p, s in _walk_strings(item, f"scene_specs[{i}].{key}")
                                 if entity_identity(s) == identity)
            continue
        if field == "world_spec":
            value = {k: v for k, v in value.items() if k != "regions"}
        paths.extend(p for p, s in _walk_strings(value, field)
                     if entity_identity(s) == identity)
    return paths


def _derived_bounds(entity: Any) -> list[float]:
    ox, oz = (list(entity.origin_offset) + [0.0, 0.0])[:2]
    r = entity.group_radius
    return [max(-10000.0, min(10000.0, b)) for b in (ox - r, oz - r, ox + r, oz + r)]


def _is_layout_region(region: Any, identity: str, copies: list[Any]) -> bool:
    """True when ``region`` is unity_layout's own per-group row for one of
    ``copies``: named after the group, listing only it, no biome, and bounded
    exactly by that copy's origin_offset +/- group_radius."""
    if entity_identity(region.name) != identity or region.biome:
        return False
    if [entity_identity(n) for n in region.entities] != [identity]:
        return False
    bounds = list(region.bounds)
    return any(all(abs(a - b) <= _BOUNDS_TOLERANCE
                   for a, b in zip(bounds, _derived_bounds(copy)))
               for copy in copies)


def _region_references(schema: Any, identity: str,
                       copies: list[Any]) -> tuple[list[int], list[str]]:
    """(layout-derived region indices, paths of other regions naming it)."""
    world = getattr(schema, "world_spec", None)
    derived: list[int] = []
    other: list[str] = []
    for i, region in enumerate(getattr(world, "regions", None) or []):
        named = (entity_identity(region.name) == identity
                 or any(entity_identity(n) == identity for n in region.entities))
        if not named:
            continue
        if _is_layout_region(region, identity, copies):
            derived.append(i)
        else:
            other.append(f"world_spec.regions[{i}]")
    return derived, other


def _listings(schema: Any, identity: str) -> list[tuple[int, Any, int]]:
    """(spec index, spec, times listed) for every scene_spec naming it."""
    out: list[tuple[int, Any, int]] = []
    for i, spec in enumerate(getattr(schema, "scene_specs", None) or []):
        hits = sum(1 for n in (spec.entities or []) if entity_identity(n) == identity)
        if hits:
            out.append((i, spec, hits))
    return out


def _rename_blocker(schema: Any, identity: str, survivors: list[tuple[int, Any]],
                    listings: list[tuple[int, Any, int]],
                    taken: set[str]) -> tuple[str, list[str]]:
    """Why tier 2 cannot rename this set ("" when it can), and the new names."""
    declared = set(getattr(schema, "scenes", None) or [])
    names = [spec.name for _, spec, _ in listings]
    undeclared = [n for n in names if n not in declared]
    if undeclared:
        return (f"it is listed in scene_specs {undeclared} that schema.scenes "
                "does not declare, so the listing order is not evidence"), []
    repeated = [spec.name for _, spec, hits in listings if hits > 1]
    if repeated:
        return (f"scene_specs {repeated} list the name more than once, so a "
                "listing cannot be matched to one copy"), []
    if len(listings) != len(survivors):
        return (f"{len(survivors)} distinct copies but {len(listings)} declared "
                f"scene_spec(s) list the name ({names or 'none'}); the "
                "positional rule needs exactly one listing per copy"), []
    refs = _bare_references(schema, identity)
    _, region_refs = _region_references(schema, identity,
                                        [entity for _, entity in survivors])
    refs += region_refs
    if refs:
        return (f"{', '.join(refs)} refer to the bare name, and one reference "
                "cannot be split between the copies"), []
    new_names = [_scene_qualified(spec.name, entity.name)
                 for (_, entity), (_, spec, _) in zip(survivors, listings)]
    folded = [entity_identity(n) for n in new_names]
    clashes = sorted({n for n, f in zip(new_names, folded)
                      if f in taken or folded.count(f) > 1})
    if clashes:
        return (f"the scene-qualified name(s) {clashes} would collide with "
                "another entity name"), []
    return "", new_names


def _rederive_regions(schema: Any, identity: str, survivors: list[Any]) -> None:
    """Replace the layout's per-group rows for this name with one row per
    renamed copy, derived from the copy exactly as unity_layout derives it."""
    world = getattr(schema, "world_spec", None)
    if world is None:
        return
    derived, _ = _region_references(schema, identity, survivors)
    if not derived:
        return
    fresh = [WorldRegion(name=e.name, entities=[e.name], bounds=_derived_bounds(e))
             for e in survivors]
    drop = set(derived)
    regions: list[WorldRegion] = []
    for i, region in enumerate(world.regions):
        if i == derived[0]:
            regions.extend(fresh)
        if i not in drop:
            regions.append(region)
    world.regions = regions


def _record(schema: Any) -> Optional[dict[str, Any]]:
    meta = schema.meta if isinstance(getattr(schema, "meta", None), dict) else None
    if meta is None:
        return None
    row = meta.get(ENTITY_NAMES_META_KEY)
    return row if isinstance(row, dict) else None


def disambiguate_entity_names(schema: Any) -> list[str]:
    """Apply the three tiers in place. Return one fix message per collapse or
    rename. Abstentions are recorded, and :func:`entity_name_warnings` says
    them loudly.

    Idempotent: a schema whose names are already unique is left alone, and the
    renames and collapses an earlier pass recorded are kept as its audit trail.
    """
    entities = list(getattr(schema, "entities", None) or [])
    sets: dict[str, list[int]] = {}
    for index, entity in enumerate(entities):
        key = entity_identity(entity.name)
        if key:
            sets.setdefault(key, []).append(index)
    duplicates = {key: idx for key, idx in sets.items() if len(idx) > 1}
    previous = _record(schema)
    if not duplicates and previous is None:
        return []

    msgs: list[str] = []
    renames: list[dict[str, Any]] = []
    collapsed: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    dropped: set[int] = set()
    taken = set(sets)

    for identity, indices in duplicates.items():
        # Tier 1 — identical copies. First of each content class wins.
        survivors: list[tuple[int, Any]] = []
        for index in indices:
            entity = entities[index]
            twin = next((s for s in survivors
                         if _content(s[1]) == _content(entity)), None)
            if twin is None:
                survivors.append((index, entity))
                continue
            dropped.add(index)
            evidence = ("identical to it in every field but the layout-owned "
                        "instance_positions/instance_yaws and the name's case")
            collapsed.append({"name": entity.name, "kept_index": twin[0],
                              "dropped_index": index, "evidence": evidence})
            msgs.append(f"[entity.names] entities[{index}] {entity.name!r} was "
                        f"a repeat of entities[{twin[0]}] {twin[1].name!r}, "
                        f"{evidence} — dropped the repeat (first wins)")
        if len(survivors) == 1:
            continue

        # Tier 2 — positional, scene-qualified rename.
        listings = _listings(schema, identity)
        reason, new_names = _rename_blocker(schema, identity, survivors,
                                            listings, taken - {identity})
        copies = [_describe(i, e) for i, e in survivors]
        if reason:
            display = survivors[0][1].name
            remedy = ("give each group its own name in entities[] and in the "
                      "scene_specs[].entities lists (and any reference) that "
                      "mean it, or merge them into one group. Step 3 must "
                      "write each entity name once.")
            abstentions.append({
                "name": display, "identity": identity, "copies": copies,
                "listings": [spec.name for _, spec, _ in listings],
                "reason": reason, "remedy": remedy,
            })
            continue

        listed_in = [spec.name for _, spec, _ in listings]
        evidence = (f"{len(survivors)} distinct groups named "
                    f"{survivors[0][1].name!r}, and exactly one declared "
                    f"scene_spec lists the name for each ({listed_in}); copy k "
                    "in schema order is the k-th listing in scene_specs order")
        for (index, entity), (_, spec, _), new in zip(survivors, listings, new_names):
            old = entity.name
            spec.entities = [new if entity_identity(n) == identity else n
                             for n in (spec.entities or [])]
            entity.name = new
            renames.append({"from": old, "to": new, "index": index,
                            "scene": spec.name, "evidence": evidence})
        _rederive_regions(schema, identity, [e for _, e in survivors])
        taken.discard(identity)
        taken.update(entity_identity(n) for n in new_names)
        msgs.append(f"[entity.names] {evidence} — renamed "
                    + "; ".join(f"{_copy_label(c)} -> {new!r} (listed in {scene})"
                                for c, new, scene in zip(copies, new_names, listed_in)))

    if dropped:
        schema.entities = [e for i, e in enumerate(entities) if i not in dropped]

    meta = schema.meta if isinstance(getattr(schema, "meta", None), dict) else None
    if meta is not None:
        old = previous or {}
        meta[ENTITY_NAMES_META_KEY] = {
            "format": ENTITY_NAMES_FORMAT,
            "stage": ENTITY_NAMES_STAGE,
            "renames": [dict(r) for r in (old.get("renames") or []) if isinstance(r, dict)]
                       + renames,
            "collapsed": [dict(r) for r in (old.get("collapsed") or []) if isinstance(r, dict)]
                         + collapsed,
            "abstentions": abstentions,
        }
    return msgs


def abstention_message(row: dict[str, Any]) -> str:
    """The loud one-line form of one abstention record."""
    copies = [c for c in row.get("copies") or [] if isinstance(c, dict)]
    return (f"[entity.names] ABSTAINED on duplicate entity name {row.get('name')!r} "
            f"({len(copies)} distinct groups: "
            + "; ".join(_copy_label(c) for c in copies)
            + f"): {row.get('reason')}. Names stay as authored, so the scene "
            f"graph still refuses the schema. Remedy: {row.get('remedy')}")


def entity_name_warnings(schema: Any) -> list[str]:
    """Loud messages for every collision the last pass abstained on."""
    record = _record(schema) or {}
    return [abstention_message(row) for row in record.get("abstentions") or []
            if isinstance(row, dict)]


def entity_name_abstention(schema: Any, identity: str) -> Optional[dict[str, Any]]:
    """The recorded abstention row for one casefolded name, or None."""
    record = _record(schema)
    for row in (record or {}).get("abstentions") or []:
        if isinstance(row, dict) and row.get("identity") == entity_identity(identity):
            return row
    return None


__all__ = [
    "ENTITY_NAMES_FORMAT",
    "ENTITY_NAMES_META_KEY",
    "ENTITY_NAMES_STAGE",
    "abstention_message",
    "disambiguate_entity_names",
    "entity_identity",
    "entity_name_abstention",
    "entity_name_warnings",
]
