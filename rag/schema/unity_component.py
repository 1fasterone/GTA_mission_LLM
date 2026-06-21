"""
Unity built-in and standard component ontology entries.

Each entry represents one Unity Component type that the compiler can attach to GameObjects.
"""
from __future__ import annotations
from .base import RagEntry


# category → tag
_CAT_TAG: dict[str, str] = {
    "rendering":   "category.rendering",
    "physics_3d":  "category.physics",
    "physics_2d":  "category.physics_2d",
    "audio":       "category.audio",
    "animation":   "category.animation",
    "navigation":  "category.navigation",
    "ui":          "category.ui",
    "ui_toolkit":  "category.ui_toolkit",
    "event":       "category.event",
    "network":     "category.network",
    "input":       "category.input",
    "timeline":    "category.timeline",
    "cinemachine": "category.cinemachine",
    "effects":     "category.effects",
    "terrain":     "category.terrain",
    "xr":          "category.xr",
    "lod":         "category.rendering",
    "transform":   "category.transform",
    "postprocess": "category.effects",
    "scripting":   "category.scripting",
}


def _tags(raw: dict) -> list[str]:
    tags: list[str] = []
    cat = raw.get("category", "")
    cat_tag = _CAT_TAG.get(cat)
    if cat_tag:
        tags.append(cat_tag)

    namespace = raw.get("namespace", "UnityEngine")
    if "Cinemachine" in namespace:
        tags.append("category.cinemachine")
        tags.append("requires.package.cinemachine")
    elif "TMPro" in namespace:
        tags.append("requires.package.tmpro")
    elif "UnityEngine.AI" in namespace:
        tags.append("category.navigation")

    for req in raw.get("requires", []):
        tags.append(f"requires.{req.lower()}")

    is_flags = raw.get("capabilities", [])
    for cap in is_flags:
        tags.append(f"is_{cap}")

    # Pipeline compatibility
    pipeline = raw.get("pipeline", "any")
    tags.append(f"pipeline.{pipeline}")

    return tags


def _aliases(raw: dict) -> list[str]:
    name = raw.get("name", "")
    a: list[str] = []
    # Split PascalCase into words as an alias
    import re
    words = re.sub(r"([A-Z])", r" \1", name).strip().lower()
    if words != name.lower():
        a.append(words)

    # Additional explicit aliases from the record
    a.extend(raw.get("aliases", []))

    # Category-based aliases
    cat = raw.get("category", "")
    if cat == "physics_3d":
        a.append(f"physics component")
    elif cat == "rendering":
        a.append(f"renderer")
    elif cat == "audio":
        a.append(f"audio component")
    elif cat == "animation":
        a.append(f"animation component")

    return list(dict.fromkeys(a))


def make_component_entry(raw: dict, source: str = "Unity built-in") -> RagEntry:
    name = raw.get("name", "")
    if not name:
        raise ValueError("component record missing name")

    tags = _tags(raw)
    aliases = _aliases(raw)
    namespace = raw.get("namespace", "UnityEngine")
    description = raw.get("description", f"Unity {name} component.")
    methods = raw.get("api_methods", [])
    requires = raw.get("requires", [])

    desc = (
        f"Unity component {name} ({namespace}). "
        f"{description} "
        f"Requires: {', '.join(requires) if requires else 'nothing'}. "
        f"API: {', '.join(methods[:6])}."
    ).strip()

    meta: dict = {k: v for k, v in {
        "namespace": namespace,
        "requires": requires,
        "api_methods": methods,
        "pipeline": raw.get("pipeline", "any"),
        "category": raw.get("category"),
    }.items() if v}

    entry = RagEntry(
        id=f"unity:component:{name}",
        domain="unity", kind="component",
        canonical=name, label=name,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = (
        f"Unity component {entry.canonical}\n"
        f"Namespace: {namespace}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"Description: {entry.description}\n"
        f"API methods: {', '.join(methods)}"
    )
    return entry
