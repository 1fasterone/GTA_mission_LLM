"""Deterministic text artifacts for image-guided Unity asset selection.

The Pipeline LLM owns image inventory/Concept creation and the Step-3 schema
fill.  This module only formats those already-produced facts and the model
resolver's frozen output; it never infers entities or chooses assets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def render_image_context(
    board_image: Path,
    inventory: dict[str, Any],
    concept: str,
) -> str:
    """Render the auditable image-to-Step-2 context artifact."""
    queries = {
        _clean(item.get("label")): _clean(item.get("query"))
        for item in inventory.get("sketchfab_queries", [])
        if isinstance(item, dict) and _clean(item.get("label"))
    }
    elements = [
        item for item in inventory.get("visible_elements", [])
        if isinstance(item, dict) and _clean(item.get("label"))
    ]

    lines = [
        "# Visual Scene Context",
        "",
        f"Source board: {board_image.name}",
        f"Scene summary: {_clean(inventory.get('scene_summary'))}",
        "",
        "## Visible asset targets",
    ]
    if elements:
        for item in elements:
            label = _clean(item.get("label"))
            details = [f"category={_clean(item.get('category')) or 'other'}"]
            model_hint = _clean(item.get("model_hint"))
            if model_hint:
                details.append(f"model_hint={model_hint}")
            query = queries.get(label, "")
            if query:
                details.append(f"image_query={query}")
            description = _clean(item.get("description"))
            lines.append(f"- {label} | {' | '.join(details)}")
            if description:
                lines.append(f"  Appearance: {description}")
    else:
        lines.append("- None recorded")

    visible_text = [
        _clean(item) for item in inventory.get("visible_text", []) if _clean(item)
    ]
    uncertainties = [
        _clean(item) for item in inventory.get("uncertainties", []) if _clean(item)
    ]
    lines.extend(["", "## Visible text"])
    lines.extend(f"- {item}" for item in visible_text)
    if not visible_text:
        lines.append("- None recorded")
    lines.extend(["", "## Visual uncertainties"])
    lines.extend(f"- {item}" for item in uncertainties)
    if not uncertainties:
        lines.append("- None recorded")

    lines.extend([
        "",
        "## Image-derived Concept",
        "",
        concept.strip(),
        "",
    ])
    return "\n".join(lines)


def render_project_context(image_context: str, schema: Any) -> str:
    """Append Step-3 searches and exact resolver bindings to context.txt."""
    lines = [image_context.rstrip(), "", "## Step 3 Sketchfab searches"]
    entities = sorted(
        (entity for entity in (getattr(schema, "entities", None) or [])
         if _clean(getattr(entity, "name", ""))),
        key=lambda entity: _clean(getattr(entity, "name", "")).casefold(),
    )
    if entities:
        for entity in entities:
            name = _clean(getattr(entity, "name", ""))
            kind = _clean(getattr(entity, "kind", ""))
            query = _clean(getattr(entity, "sketchfab_query", ""))
            hint = _clean(getattr(entity, "model_hint", ""))
            effective = query or hint or name
            lines.append(
                f"- {name} | kind={kind or 'prop'} | query={effective}"
            )

    if not entities:
        lines.append("- None recorded")

    lines.extend(["", "## Frozen Unity model bindings"])
    meta = getattr(schema, "meta", {})
    bindings = meta.get("entity_model_bindings", {}) if isinstance(meta, dict) else {}
    if isinstance(bindings, dict) and bindings:
        for entity_name in sorted(bindings, key=str.casefold):
            binding = bindings[entity_name]
            if not isinstance(binding, dict):
                continue
            filename = _clean(binding.get("filename")) or "generated"
            resource_path = _clean(binding.get("resource_path")) or "unbound"
            source = _clean(binding.get("source")) or "unknown"
            lines.append(
                f"- {entity_name} | filename={filename} | "
                f"resource_path={resource_path} | source={source}"
            )
    else:
        lines.append("- No external model files were bound")
    lines.append("")
    return "\n".join(lines)
