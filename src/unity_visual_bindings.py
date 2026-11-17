"""Deterministic binding from closed image nouns to Unity schema entities.

Slice 5 consumes the already-closed image IR and Slice-4 selections.  This
module does not search, score, or reinterpret assets; it only establishes the
unique schema-entity name that each fulfilled visual noun may lock to before
the generator runs.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple, Optional

from .unity_asset_catalog import tokenize
from .unity_scene_membership import hosting_scenes, is_menu_scene
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


def _instance_numbers(value: Any) -> set[str]:
    return set(re.findall(r"\d+", str(value or "")))


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
                if (_instance_numbers(label) & _instance_numbers(name)
                        and name_overlap > 0):
                    # Slice-2 labels use "stone block 2" while the schema
                    # commonly uses "StoneBlock2Group".  Keep the numeric
                    # identity only as a disambiguator after a shared noun
                    # already proves the semantic family.
                    score += 1000
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


class VisualSceneCoverage(NamedTuple):
    """What :func:`ensure_visual_scene_coverage` did.

    ``scene`` is the image-led gameplay scene it chose, or None when it
    abstained. ``fixes`` are the changes it made. ``warnings`` are loud
    abstentions, and the caller must surface them as warnings, not fixes.
    """

    scene: Optional[str]
    fixes: list[str]
    warnings: list[str]


def _declared_specs(schema: Any) -> dict[str, Any]:
    """``{scene name: spec}`` for specs of declared scenes. This is the same
    reading as ``unity_scene_membership.hosting_scenes``."""
    scenes = {str(s) for s in (getattr(schema, "scenes", None) or []) if s}
    return {spec.name: spec for spec in (getattr(schema, "scene_specs", None) or [])
            if spec.name in scenes}


def image_led_gameplay_scene(schema: Any) -> tuple[Optional[str], list[str]]:
    """The scene the closed image IR's visible cast belongs in, or None.

    The closed image IR and the visual compile inputs name no scene:
    ``visual_reasoning_compile*``, ``visual_asset_*``,
    ``visual_spatial_extraction``, ``unity_spatial_fusion`` and
    ``unity_spatial_roles`` carry nouns, selections and image-space evidence,
    but no ``scene`` field. So the schema is the evidence. The image-led scene
    is the first scene in ``schema.scenes`` order that a player plays in:

    - not a menu (``is_menu_scene``). A menu is never a gameplay scene.
    - not explicitly empty. A spec whose ``entities`` is ``[]`` declared that
      it hosts nothing. ``_reconcile_scene_specs`` and the membership pass
      never add to such a scene, and this pass does not either.

    The second element names each earlier scene that was passed over, and
    why. When no scene qualifies, the caller abstains. It never picks a
    scene by guess.
    """
    specs = _declared_specs(schema)
    passed_over: list[str] = []
    for scene in [str(s) for s in (getattr(schema, "scenes", None) or []) if s]:
        if is_menu_scene(scene):
            passed_over.append(f"{scene!r} is a menu")
            continue
        spec = specs.get(scene)
        if spec is not None and spec.entities is not None and not spec.entities:
            passed_over.append(f"{scene!r} declared itself empty (entities: [])")
            continue
        return scene, passed_over
    return None, passed_over


def ensure_visual_scene_coverage(schema: Any) -> VisualSceneCoverage:
    """Make the image-led gameplay scene spawn every final schema entity.

    A Concept-derived schema may keep a partial ``scene_specs[].entities``
    subset even though the closed image IR requires the whole visible cast.
    This pass adds the missing entities to the image-led gameplay scene
    (:func:`image_led_gameplay_scene`). It makes no asset or placement
    decision.

    - A chosen scene with no spec, or with ``entities=None``, already hosts
      every group, so nothing is added.
    - A menu scene, or a spec that lists nothing, never receives entities.
    - With no eligible scene, the pass ABSTAINS by name and changes nothing.
      An entity that is still in no scene stays a recorded scene-membership
      abstention, because ``refresh_scene_membership`` clears only entities
      that now have a scene.
    """
    names = [str(entity.name) for entity in getattr(schema, "entities", [])
             if getattr(entity, "name", "")]
    if not names:
        return VisualSceneCoverage(None, [], [])
    scene, passed_over = image_led_gameplay_scene(schema)
    if scene is None:
        homeless = [name for name in names if not hosting_scenes(schema, name)]
        reasons = ", ".join(passed_over) or "schema.scenes declares no scene"
        message = (
            "[visual.coverage] ABSTAINED: no gameplay scene can take the "
            f"image's visible cast ({reasons}), and no visual input names one. "
            f"Left out of every gameplay scene: {', '.join(names)}."
        )
        if homeless:
            message += (f" In no scene at all, so they stay scene-membership "
                        f"abstentions: {', '.join(homeless)}.")
        message += (" Remedy: declare a non-menu scene in schema.scenes whose "
                    "scene_specs entry lists at least one group, or has no "
                    "entities list.")
        return VisualSceneCoverage(None, [], [message])
    spec = _declared_specs(schema).get(scene)
    if spec is None or spec.entities is None:
        return VisualSceneCoverage(scene, [], [])
    existing = list(spec.entities)
    missing = [name for name in names if name not in existing]
    if not missing:
        return VisualSceneCoverage(scene, [], [])
    spec.entities = existing + missing
    why = ("first scene in schema.scenes that is not a menu and does not "
           "declare itself empty"
           + (f"; passed over {', '.join(passed_over)}" if passed_over else ""))
    return VisualSceneCoverage(scene, [
        f"Added {len(missing)} entity group(s) to visual gameplay scene "
        f"{scene!r} ({why}): {', '.join(missing)}"
    ], [])
