"""
Model name resolver — Step 4.
Tries the local RAG index first; falls back to local lookup tables.
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

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


# ── Deterministic model-ID pick (Step 5b) ────────────────────────────────────
#
# Last tier for a description the resolver left UNRESOLVED. It used to be an
# LLM call inside the fixer that fell back to `candidates[0]` — an arbitrary
# model shipped with a WARN. This scores the description against the words in
# each candidate's own ID instead: deterministic, and it ABSTAINS (keeps
# UNRESOLVED, spawn skipped, named in the log) when nothing matches or two
# different models tie. Variants of one model are not a tie — `_01`/`_02`,
# `police`/`police2`, or a ped's age/gender letters around the same role
# (`a_f_y_beach_01`/`a_f_m_beach_01`) — the lowest-sorted variant is used.

# ID fragments that name a namespace or DLC, never what the model depicts.
_GENERIC_ID_TOKENS = frozenset({"prop", "veh", "csb", "xm3", "dlc", "int", "des"})
_FEMALE_WORDS = frozenset({"woman", "women", "female", "girl", "lady", "ladies",
                           "she", "her", "wife", "mother", "mom", "grandma"})
_MALE_WORDS = frozenset({"man", "men", "male", "guy", "boy", "he", "his",
                         "husband", "father", "dad", "grandpa"})
_PED_ID_RE = re.compile(r"^[a-z]+_([fm])_[a-z]_(.+)$")
_VARIANT_SUFFIX_RE = re.compile(r"_?\d+[a-z]?$")


def _id_tokens(model_id: str) -> list[str]:
    """Descriptive words inside a model ID (`s_m_y_cop_01` -> ['cop'])."""
    return [t for t in re.split(r"[_\d]+", model_id.lower())
            if len(t) >= 3 and t not in _GENERIC_ID_TOKENS]


def _id_base(model_id: str, ped: bool) -> str:
    """What the model depicts, variant letters stripped (`police2` -> `police`,
    ped `a_f_y_beach_01` -> `beach`)."""
    base = _VARIANT_SUFFIX_RE.sub("", model_id.lower()) or model_id.lower()
    if ped:
        m = _PED_ID_RE.match(base)
        if m:
            return m.group(2)
    return base


def _desc_words(description: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", description.lower()))
    return words | {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}


def _word_score(word: str, tokens: list[str]) -> int:
    """2 for an exact ID token, 1 for a containment match (never short words)."""
    if word in tokens:
        return 2
    if len(word) >= 4 and any(len(t) >= 4 and (word in t or t in word)
                              for t in tokens):
        return 1
    return 0


def _gender_cue(words: set[str]) -> Optional[str]:
    female = any(w in _FEMALE_WORDS or w.endswith("woman") for w in words)
    male = any(w in _MALE_WORDS or (w.endswith("man") and not w.endswith("woman"))
               for w in words)
    return "f" if female and not male else "m" if male and not female else None


def pick_model_deterministic(description: str, candidates: list[str], *,
                             ped: bool = False) -> tuple[Optional[str], str]:
    """Pick the candidate whose ID words best match `description`.

    Returns (model, reason). `model` is None when the pick abstains: no
    candidate ID shares a word with the description, or the best score is
    shared by two different models. For peds, a gender word in the
    description ("woman", "businessman") ranks the matching `_f_`/`_m_` ID
    first.
    """
    words = _desc_words(description)
    cue = _gender_cue(words) if ped else None

    scored: list[tuple[int, int, str]] = []            # (score, gender, id)
    for model in sorted(set(candidates)):
        tokens = _id_tokens(model)
        score = sum(_word_score(w, tokens) for w in words)
        if score < 1:
            continue
        gender = 0
        if cue:
            m = _PED_ID_RE.match(model)
            gender = 1 if m and m.group(1) == cue else 0
        scored.append((score, gender, model))

    if not scored:
        return None, "no candidate model ID shares a word with the description"
    best = max((sc, g) for sc, g, _ in scored)
    top = sorted(m for sc, g, m in scored if (sc, g) == best)
    bases = sorted({_id_base(m, ped) for m in top})
    if len(bases) > 1:
        shown = ", ".join(bases[:3]) + (" …" if len(bases) > 3 else "")
        return None, f"tie between {len(bases)} different models ({shown})"
    return top[0], f"model-ID word match (score {best[0]})"


class ModelResolver:
    def __init__(self, rag: Any, lookups_dir: str,
                 fallback: bool = True, use_rag: bool = True):
        self.rag      = rag
        self.lookups  = _load_lookups(lookups_dir)
        self.fallback = fallback
        self.use_rag  = use_rag

    def resolve(self, description: str, category: str) -> str:
        if not self.use_rag or self.rag is None:
            # Basic Mode or no rag client — use local lookups only.
            local = _local_resolve(description, category, self.lookups)
            if local:
                return local
            console.print(f"  [yellow]WARN[/] No local match for '{description}' ({category}) — UNRESOLVED")
            return "UNRESOLVED"
        try:
            result = self.rag.resolve_model(description, category)
            if result:
                result = result.lower()   # GTA5 names are case-insensitive; normalize for codegen
            if result and _MODEL_ID_RE.match(result):
                # Corpus-backed clients (RagClient) return verified canonical names —
                # skip the valid_models guard that protects against LLM hallucination.
                corpus_backed = getattr(self.rag, "is_corpus_backed", False)
                valid = self.lookups.get(category, {}).get("valid_models", [])
                if corpus_backed or not valid or result in valid:
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
