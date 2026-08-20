"""
Shared loaders for the SHVDN3 reality allowlists (lookups/gta_*.json).

Extracted from src/pipeline.py so both the mission pipeline AND the GTA micro
layer can lint LLM-authored C# against the same audited API surface without a
pipeline <-> micro import cycle. Missing files degrade to None, which turns the
corresponding lint pass into a no-op rather than a hard failure.
"""

from __future__ import annotations

import json
from pathlib import Path


# ── Real GTA.Native.Hash members — catches LLM-invented natives (e.g. the
# hallucinated SET_MISSION_FAILED / SET_MISSION_PASSED that fail CS0117 at
# compile time). Sourced from a real SHVDN3 audit tool's allowlist
# (6,460 native names).
def load_hash_allowlist(lookups_dir: str = "lookups") -> "frozenset[str] | None":
    path = Path(lookups_dir) / "gta_hash_allowlist.json"
    if not path.exists():
        return None
    try:
        return frozenset(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


# ── Real SHVDN3 member surface for a fixed set of commonly-referenced types —
# catches LLM-invented members (e.g. Ped.GetNearestVehicle, which doesn't exist).
def load_api_surface(lookups_dir: str = "lookups") -> "dict | None":
    path = Path(lookups_dir) / "gta_api_surface.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── Real SHVDN3 enum members — catches LLM-invented enum values (e.g.
# Weather.Rain; the real member is Weather.Raining).
def load_enum_allowlist(lookups_dir: str = "lookups") -> "dict | None":
    path = Path(lookups_dir) / "gta_enum_allowlist.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── Real native argument counts, mined from 715 decompiled Rockstar mission
# scripts (tools/mine_native_arity.py) — catches Function.Call(Hash.X, ...)
# passing the wrong number of arguments, a mistake csc can't see because
# InputArgument[] is variadic. Only natives with a single argument count
# observed across the whole corpus are included: a native seen with more
# than one distinct count is ambiguous ground truth (could be a genuinely
# variable-shape call site, or a decompiler mis-parse) and is deliberately
# left out rather than guessed at.
def load_native_arity(lookups_dir: str = "lookups") -> "dict[str, int] | None":
    path = Path(lookups_dir) / "gta_native_arity.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {name: rec["primary_arity"] for name, rec in data.items() if rec.get("consistent")}
