"""
Unity scene grammar rule ontology entries.

Rules encode constraints that prevent the LLM from generating broken scene setups —
missing cameras, pipeline mismatches, invalid layer configs, etc.
"""
from __future__ import annotations
from .base import RagEntry


def make_scene_rule_entry(raw: dict, source: str = "Unity documentation") -> RagEntry:
    rule_id = raw.get("id", "")
    if not rule_id:
        raise ValueError("scene_rule record missing id")

    label = raw.get("label", rule_id.replace("_", " ").title())
    rule_type = raw.get("rule_type", "required")   # required | forbidden | warning
    pipeline = raw.get("pipeline", "any")           # any | urp | hdrp | builtin
    violation = raw.get("violation", "Undefined behavior")
    description = raw.get("description", label)
    aliases = raw.get("aliases", [])
    affected = raw.get("affects", [])

    tags = [
        f"rule.{rule_type}",
        f"pipeline.{pipeline}",
        "is_scene_rule",
    ]
    for a in affected:
        tags.append(f"affects.{a.lower()}")

    desc = (
        f"Unity scene rule {rule_id}. {description}. "
        f"Type: {rule_type}. Pipeline: {pipeline}. "
        f"Violation consequence: {violation}."
    ).strip()

    meta: dict = {k: v for k, v in {
        "rule_type": rule_type,
        "pipeline": pipeline,
        "violation": violation,
        "affects": affected,
        "fix": raw.get("fix"),
    }.items() if v}

    entry = RagEntry(
        id=f"unity:scene_rule:{rule_id}",
        domain="unity", kind="scene_rule",
        canonical=rule_id, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = (
        f"Unity scene rule {entry.canonical}\n"
        f"Type: {rule_type} | Pipeline: {pipeline}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"Description: {entry.description}\n"
        f"If violated: {violation}"
    )
    return entry
