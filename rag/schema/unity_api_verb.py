"""
Unity scripting API verb ontology entries.

Each entry is a *compiler-supported* verb — only the calls the generator can emit.
This keeps the LLM inside a deterministic sandbox.
"""
from __future__ import annotations
from .base import RagEntry


def make_api_verb_entry(raw: dict, source: str = "Unity ScriptReference") -> RagEntry:
    verb_id = raw.get("id", "")
    if not verb_id:
        raise ValueError("api_verb record missing id")

    label = raw.get("label", verb_id.replace("_", " ").title())
    subsystem = raw.get("subsystem", "misc")
    description = raw.get("description", label)
    csharp = raw.get("csharp", "")
    aliases = raw.get("aliases", [])
    is_async = raw.get("async", False)
    requires = raw.get("requires", [])

    tags = [
        f"subsystem.{subsystem}",
        f"action.{raw.get('action', 'misc')}",
        "is_verb",
    ]
    if is_async:
        tags.append("is_coroutine")
    for r in requires:
        tags.append(f"requires.{r.lower()}")

    desc = (
        f"Unity API verb {verb_id}. {description}. "
        f"Subsystem: {subsystem}. "
        f"C# pattern: {csharp}."
    ).strip()

    meta: dict = {k: v for k, v in {
        "subsystem": subsystem,
        "csharp": csharp,
        "action": raw.get("action"),
        "async": is_async,
        "requires": requires,
    }.items() if v}

    entry = RagEntry(
        id=f"unity:api_verb:{verb_id}",
        domain="unity", kind="api_verb",
        canonical=verb_id, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = (
        f"Unity verb {entry.canonical}\n"
        f"Label: {entry.label}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"C#: {csharp}\n"
        f"Description: {entry.description}"
    )
    return entry
