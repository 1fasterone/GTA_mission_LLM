"""
Unity Package Registry ontology entries.

Each entry represents one UPM package from packages.unity.com.
The canonical is the package ID (e.g. com.unity.cinemachine).
"""
from __future__ import annotations
from .base import RagEntry


# Keyword → tag mappings (domain.value)
_DOMAIN_KW: dict[str, str] = {
    "render":        "domain.rendering",
    "shader":        "domain.rendering",
    "vfx":          "domain.vfx",
    "visual effect": "domain.vfx",
    "particle":     "domain.vfx",
    "audio":        "domain.audio",
    "sound":        "domain.audio",
    "physics":      "domain.physics",
    "ai":           "domain.ai",
    "navigation":   "domain.ai",
    "navmesh":      "domain.ai",
    "pathfinding":  "domain.ai",
    "ui":           "domain.ui",
    "canvas":       "domain.ui",
    "ugui":         "domain.ui",
    "input":        "domain.input",
    "animation":    "domain.animation",
    "cinemachine":  "domain.cinemachine",
    "camera":       "domain.cinemachine",
    "network":      "domain.network",
    "multiplayer":  "domain.network",
    "netcode":      "domain.network",
    "localization": "domain.localization",
    "2d":           "domain.2d",
    "tilemap":      "domain.2d",
    "sprite":       "domain.2d",
    "terrain":      "domain.terrain",
    "xr":           "domain.xr",
    "ar":           "domain.xr",
    "vr":           "domain.xr",
    "test":         "domain.testing",
    "testing":      "domain.testing",
    "addressable":  "domain.tools",
    "profil":       "domain.tools",
    "timeline":     "domain.animation",
    "playable":     "domain.animation",
}

_PIPELINE_KW: dict[str, str] = {
    "universal":    "pipeline.urp",
    "urp":          "pipeline.urp",
    "high-definition": "pipeline.hdrp",
    "hdrp":         "pipeline.hdrp",
    "core":         "pipeline.builtin",
    "shadergraph":  "pipeline.any",
    "2d":           "pipeline.any",
}


def _tags(pkg_id: str, description: str, keywords: list[str], category: str) -> list[str]:
    tags: list[str] = []
    haystack = f"{pkg_id} {description} {' '.join(keywords)} {category}".lower()

    for kw, tag in _PIPELINE_KW.items():
        if kw in haystack and tag not in tags:
            tags.append(tag)

    for kw, tag in _DOMAIN_KW.items():
        if kw in haystack and tag not in tags:
            tags.append(tag)

    if not any(t.startswith("pipeline.") for t in tags):
        tags.append("pipeline.any")
    if not any(t.startswith("domain.") for t in tags):
        tags.append("domain.tools")

    # Category tags
    if category:
        tags.append(f"category.{category.lower().replace(' ', '_')}")

    return tags


def _aliases(pkg_id: str, display_name: str, keywords: list[str], description: str) -> list[str]:
    a: list[str] = []
    if display_name:
        a.append(display_name.lower())
    # Short name after last dot segment
    short = pkg_id.split(".")[-1].replace("-", " ")
    if short and short.lower() not in a:
        a.append(short)
    # Middle part (e.g. com.unity.render-pipelines.universal → "render pipelines universal")
    parts = pkg_id.split(".")
    if len(parts) >= 3:
        mid = " ".join(parts[2:]).replace("-", " ")
        if mid not in a:
            a.append(mid)
    a.extend(kw.lower() for kw in keywords[:5] if kw.lower() not in a)
    # Package-specific well-known aliases
    _KNOWN: dict[str, list[str]] = {
        "com.unity.render-pipelines.universal": ["urp", "universal render pipeline", "lwrp"],
        "com.unity.render-pipelines.high-definition": ["hdrp", "high definition render pipeline"],
        "com.unity.cinemachine": ["cinemachine", "virtual camera", "camera rig"],
        "com.unity.inputsystem": ["new input system", "input system", "player input"],
        "com.unity.ai.navigation": ["navmesh", "ai navigation", "pathfinding"],
        "com.unity.addressables": ["addressables", "asset management"],
        "com.unity.netcode.gameobjects": ["netcode", "multiplayer", "ngo"],
        "com.unity.2d.tilemap": ["tilemap", "2d tilemap"],
        "com.unity.timeline": ["timeline", "cutscene timeline"],
        "com.unity.textmeshpro": ["tmp", "textmesh pro", "text mesh pro"],
        "com.unity.shadergraph": ["shader graph", "shader editor"],
        "com.unity.visualeffectgraph": ["vfx graph", "visual effect graph"],
        "com.unity.postprocessing": ["post processing", "bloom", "depth of field"],
        "com.unity.probuilder": ["probuilder", "level editor", "bsp"],
        "com.unity.test-framework": ["test framework", "unit test", "play mode test"],
        "com.unity.localization": ["localization", "i18n", "translation"],
        "com.unity.burst": ["burst compiler", "jobs", "burst"],
        "com.unity.entities": ["dots", "ecs", "entity component system"],
        "com.unity.physics": ["unity physics", "dots physics"],
        "com.unity.mathematics": ["math library", "float3", "mathematics"],
        "com.unity.collections": ["native collections", "nativearray"],
        "com.unity.jobs": ["job system", "parallel jobs"],
    }
    if pkg_id in _KNOWN:
        for alias in _KNOWN[pkg_id]:
            if alias not in a:
                a.append(alias)
    return a


def _label(display_name: str, pkg_id: str) -> str:
    if display_name:
        return display_name
    parts = pkg_id.split(".")
    return " ".join(p.replace("-", " ").title() for p in parts[2:]) or pkg_id


def make_package_entry(raw: dict, source: str = "packages.unity.com") -> RagEntry:
    pkg_id = raw.get("name", "")
    if not pkg_id:
        raise ValueError("package record missing name")

    # The npm registry returns nested version data; extract latest version info
    versions: dict = raw.get("versions", {})
    latest_v = raw.get("dist-tags", {}).get("latest", "")
    ver_data: dict = versions.get(latest_v, {}) if versions else {}

    display_name = (
        raw.get("displayName")
        or ver_data.get("displayName")
        or raw.get("_id", pkg_id).split("@")[0]
    )
    description = (
        raw.get("description")
        or ver_data.get("description")
        or ""
    )[:300]
    keywords: list[str] = (
        raw.get("keywords")
        or ver_data.get("keywords")
        or []
    )
    category = (
        raw.get("category")
        or ver_data.get("category")
        or ""
    )
    unity_min = (
        raw.get("unity")
        or ver_data.get("unity")
        or ""
    )

    label = _label(display_name, pkg_id)
    tags = _tags(pkg_id, description, keywords, category)
    aliases = _aliases(pkg_id, display_name, keywords, description)
    desc = (
        f"Unity package {pkg_id}. {label}. "
        f"{description} "
        f"Category: {category}. "
        f"Min Unity: {unity_min}. "
        f"Tags: {', '.join(tags)}."
    ).strip()

    meta: dict = {k: v for k, v in {
        "latest_version": latest_v,
        "unity_min": unity_min,
        "category": category,
        "keywords": keywords[:10],
    }.items() if v}

    entry = RagEntry(
        id=f"unity:package:{pkg_id}",
        domain="unity", kind="package",
        canonical=pkg_id, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = (
        f"Unity package {entry.canonical}\n"
        f"Label: {entry.label}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"Description: {entry.description}"
    )
    return entry
