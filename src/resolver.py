"""
Model name resolver — Step 4.
Tries AnythingLLM RAG first; falls back to local lookup tables.
"""

import json
import re
from pathlib import Path
from typing import Optional

from rich.console import Console

from .anythingllm_client import AnythingLLMClient
from .mission_schema import MissionSchema

console = Console()


_MODEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")


def _load_lookups(lookups_dir: str) -> dict[str, dict]:
    base = Path(lookups_dir)
    result: dict[str, dict] = {}
    for name in ("peds", "vehicles", "objects", "weapons", "weather", "audio"):
        path = base / f"{name}.json"
        if path.exists():
            result[name] = json.loads(path.read_text(encoding="utf-8"))
    return result


def _local_resolve(description: str, category: str, lookups: dict,
                   exclude: Optional[set[str]] = None, fuzzy: bool = True) -> Optional[str]:
    """
    Local synonym lookup.

    ``exclude`` skips models already used elsewhere in the mission so distinct
    descriptions don't collapse to the same model. ``fuzzy`` enables the loose
    word-overlap fallback; dedup calls it with ``fuzzy=False`` so it only accepts
    an exact- or substring-related alternative (a related model, never an
    unrelated one) and otherwise returns None so the caller keeps the duplicate.
    """
    exclude  = exclude or set()
    table    = lookups.get(category, {})
    synonyms = table.get("synonyms", {})
    desc     = description.lower().strip()

    if desc in synonyms and synonyms[desc] not in exclude:
        return synonyms[desc]

    for key, model in sorted(synonyms.items()):   # sorted → deterministic
        if (key in desc or desc in key) and model not in exclude:
            return model

    if fuzzy:
        desc_words = set(desc.split())
        scored: list[tuple[int, str, str]] = []        # (overlap, key, model)
        for key, model in synonyms.items():
            if model in exclude:
                continue
            overlap = len(desc_words & set(key.split()))
            if overlap >= 1:
                scored.append((overlap, key, model))
        if scored:
            scored.sort(key=lambda t: (-t[0], t[1]))   # most overlap, then a→z
            return scored[0][2]

    return None


class ModelResolver:
    def __init__(self, rag: AnythingLLMClient, lookups_dir: str,
                 fallback: bool = True, use_rag: bool = True):
        self.rag      = rag
        self.lookups  = _load_lookups(lookups_dir)
        self.fallback = fallback
        self.use_rag  = use_rag

    def resolve(self, description: str, category: str) -> str:
        if not self.use_rag:
            # Basic Mode — skip RAG entirely, use local lookups only.
            local = _local_resolve(description, category, self.lookups)
            if local:
                return local
            console.print(f"  [yellow]WARN[/] No local match for '{description}' ({category}) — UNRESOLVED")
            return "UNRESOLVED"
        try:
            result = self.rag.resolve_model(description, category)
            if result and _MODEL_ID_RE.match(result):
                valid = self.lookups.get(category, {}).get("valid_models", [])
                if not valid or result in valid:
                    return result
                console.print(f"  [dim]RAG returned unknown model '{result}' for '{description}' — trying local[/]")
            elif result:
                console.print(f"  [dim]RAG returned non-ID '{result}' for '{description}' — trying local[/]")
        except Exception as exc:
            console.print(f"  [yellow]WARN[/] RAG failed for '{description}' ({type(exc).__name__}) — trying local fallback")

        if self.fallback:
            local = _local_resolve(description, category, self.lookups)
            if local:
                return local

        console.print(f"  [yellow]WARN[/] No match found for '{description}' ({category}) — UNRESOLVED")
        return "UNRESOLVED"

    def resolve_audio_name(self, context: str, audio_type: str) -> str:
        try:
            name = self.rag.resolve_audio(context, audio_type)
            if name:
                return name
        except Exception:
            pass

        defaults = {
            "mission_passed": "MISSION_PASSED_JINGLE",
            "mission_failed": "MISSION_FAILED_JINGLE",
            "ambient":        "DLC_ISLANDS_AMBIENCE_EXTERIOR",
            "sting":          "STING_SUBTLE_1",
        }
        return defaults.get(audio_type, "")

    def resolve_schema(self, schema: MissionSchema) -> MissionSchema:
        for ped in schema.peds:
            if not ped.model or not _MODEL_ID_RE.match(ped.model or ""):
                ped.model = self.resolve(ped.description, "peds")

        for vehicle in schema.vehicles:
            if not vehicle.model or not _MODEL_ID_RE.match(vehicle.model or ""):
                vehicle.model = self.resolve(vehicle.description, "vehicles")

        for obj in schema.objects:
            if not obj.model or not _MODEL_ID_RE.match(obj.model or ""):
                obj.model = self.resolve(obj.description, "objects")

        for audio in schema.audio:
            if not audio.name:
                audio.name = self.resolve_audio_name(audio.context, audio.type)

        # Within-mission dedup: distinct descriptions that collapsed to the same
        # model get re-resolved to a related-but-unused model, so a cast of
        # "corrupt cop / security guard / sniper" doesn't become three identical
        # peds. The first occurrence keeps its model; only later collisions move.
        self._dedup_models(
            [p for p in schema.peds], "peds")
        self._dedup_models(
            [v for v in schema.vehicles], "vehicles")
        self._dedup_models(
            [o for o in schema.objects], "objects")

        return schema

    def _dedup_models(self, entities: list, category: str) -> None:
        used: set[str] = set()
        seen_desc: dict[str, str] = {}    # description -> model (keep identical descs identical)
        for ent in entities:
            model = getattr(ent, "model", None)
            if not model or model == "UNRESOLVED":
                continue
            desc = ent.description.strip().lower()
            # Identical descriptions intentionally share a model.
            if desc in seen_desc:
                ent.model = seen_desc[desc]
                used.add(ent.model)
                continue
            if model in used:
                alt = _local_resolve(ent.description, category, self.lookups,
                                     exclude=used, fuzzy=False)
                if alt and alt not in used:
                    console.print(f"  [dim]dedup: '{ent.description}' {model} -> {alt} (variety)[/]")
                    ent.model = alt
                    model = alt
            used.add(model)
            seen_desc[desc] = model
