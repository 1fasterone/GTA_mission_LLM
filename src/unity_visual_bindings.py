"""Deterministic binding from closed image nouns to Unity schema entities.

Slice 5 consumes the already-closed image IR and Slice-4 selections.  This
module does not search, score, or reinterpret assets; it only establishes the
unique schema-entity name that each fulfilled visual noun may lock to before
the generator runs.
"""

from __future__ import annotations

import re
from typing import Any

from .unity_asset_catalog import tokenize
from .unity_schema import SceneSpec
from .visual_reasoning_compiler import VisualReasoningCompilerError


_GENERIC = frozenset({
    "a", "an", "and", "asset", "character", "element", "entity", "game",
    "group", "object", "player", "prop", "scene", "thing", "vehicle",
    "world",
})


def _terms(value: Any) -> set[str]:
    raw = str(value or "")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    result = {
        token for token in tokenize(text)
        if len(token) > 1 and token not in _GENERIC
    }
    compact = re.sub(r"[^a-z0-9]", "", raw.casefold())
    if len(compact) > 1 and any(char.isdigit() for char in compact):
        result.add(compact)
    return result


def _entity_terms(entity: Any) -> set[str]:
    return (
        _terms(getattr(entity, "name", ""))
        | _terms(getattr(entity, "model_hint", ""))
        | _terms(getattr(entity, "sketchfab_query", ""))
    )


def _visual_terms(record: dict[str, Any]) -> set[str]:
    element = record.get("element") or {}
    selection = record.get("selection") or {}
    return (
        _terms(record.get("inventory_label"))
        | _terms(element.get("label"))
        | _terms(element.get("model_hint"))
        | _terms(record.get("query"))
        | _terms(selection.get("filename"))
    )


def bind_visual_assets(schema: Any,
                       visual_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return ``{schema_entity_name: exact Slice-4 selection}`` or abstain.

    The score is only a conservative name/model-hint binding.  It never
    examines thumbnails, CLIP embeddings, or catalog alternatives.  Equal
    best entity matches are rejected because silently wiring the wrong group
    would be worse than stopping before generation.
    """
    if not isinstance(visual_records, list) or not visual_records:
        raise VisualReasoningCompilerError(
            "Slice-5 has no fulfilled visual asset records to bind"
        )
    entities = [entity for entity in getattr(schema, "entities", [])
                if getattr(entity, "name", "")]
    if not entities:
        raise VisualReasoningCompilerError(
            "Slice-5 schema has no named entities for visual asset binding"
        )

    bindings: dict[str, dict[str, Any]] = {}
    used_entities: set[str] = set()
    failures: list[str] = []
    for record in visual_records:
        label = str(record.get("inventory_label") or "").strip()
        if not label:
            failures.append("fulfilled visual record has no inventory_label")
            continue
        target = _visual_terms(record)
        def score_entities(candidate_entities: list[Any]) -> list[tuple[int, int, str, Any]]:
            candidates: list[tuple[int, int, str, Any]] = []
            for entity in candidate_entities:
                name = str(entity.name)
                overlap = target & _entity_terms(entity)
                if not overlap:
                    continue
                exact = int(_terms(name) == _terms(label))
                # Exact names dominate; otherwise shared model nouns must carry
                # the match.  Description-only overlaps are intentionally weak
                # because the Concept can mention many nearby objects.
                name_overlap = len(_terms(name) & target)
                hint_overlap = len(
                    (_terms(getattr(entity, "model_hint", ""))
                     | _terms(getattr(entity, "sketchfab_query", ""))) & target
                )
                score = exact * 100 + name_overlap * 4 + hint_overlap * 3 + len(overlap)
                if score > 0:
                    candidates.append((score, exact, name, entity))
            candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
            return candidates

        candidates = score_entities([
            entity for entity in entities
            if str(entity.name) not in used_entities
        ])
        # Repeated image nouns can be instances of one schema group (for
        # example aircraft_2..aircraft_5 in an EnemyAircraftGroup).  Once all
        # distinct matching groups have been consumed, permit reuse only when
        # the all-entity score still has one unique best target.  This keeps
        # ambiguous same-kind groups and unrelated weak matches as abstentions.
        if not candidates:
            candidates = score_entities(entities)
        if not candidates:
            failures.append(f"{label!r} has no unique schema entity match")
            continue
        best = candidates[0]
        tied = [item for item in candidates if item[:2] == best[:2]]
        if len(tied) > 1:
            failures.append(
                f"{label!r} ambiguously matches schema entities "
                + ", ".join(item[2] for item in tied[:4])
            )
            continue
        used_entities.add(best[2])
        # Keep the first representative for a reused schema group.  The
        # fusion layer may receive several visible instances for one group;
        # replacing the first label with a later one would make the result
        # depend on incidental record ordering without improving coverage.
        bindings.setdefault(best[2], dict(record.get("selection") or {}))

    if failures:
        raise VisualReasoningCompilerError(
            "Slice-5 visual binding rejected:\n- " + "\n- ".join(failures)
        )
    return bindings


def ensure_visual_scene_coverage(schema: Any) -> list[str]:
    """Make the image-led gameplay scene spawn every final schema entity.

    A Concept-derived schema may preserve a partial ``scene_specs[].entities``
    subset even though the closed image IR requires the whole visible cast.
    For this handoff, missing entities are added to the first gameplay scene;
    no asset or placement decision is made here.
    """
    names = [str(entity.name) for entity in getattr(schema, "entities", [])
             if getattr(entity, "name", "")]
    if not names:
        return []
    specs = list(getattr(schema, "scene_specs", []) or [])
    if not specs:
        scene_name = next(iter(getattr(schema, "scenes", []) or []), "Main")
        schema.scene_specs = [SceneSpec(name=scene_name, entities=list(names))]
        return [f"Created visual gameplay scene spec {scene_name!r} for all "
                f"{len(names)} schema entities"]
    target = specs[0]
    if target.entities is None:
        return []
    existing = list(target.entities)
    missing = [name for name in names if name not in existing]
    if not missing:
        return []
    target.entities = existing + missing
    return [f"Added {len(missing)} entity group(s) to visual gameplay scene "
            f"{target.name!r}: {', '.join(missing)}"]
