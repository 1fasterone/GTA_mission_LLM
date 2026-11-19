"""
Pre-validation clamp for the Step-3 schema fill — collapses a runaway list
before Pydantic rejects the whole schema.

Live failure (Gigantinut, a free-form racing brief with no SCRIPTS section):
schema-fill returned 155 `scripts`. UnitySchema caps that list at 24, so
`model_validate` raised `too_long` and the entire run died — AFTER both LLM
calls, discarding the concept and the schema with it. Nothing in the 155 was
unusable; the list was simply longer than the schema allows, and a local model
that starts repeating a JSON object shape has no way to stop itself.

The caps stay ground truth: this module never raises one. It removes what is
provably redundant (two entries claiming the same name — the exact shape a
repetition loop produces), and when a list is STILL over its cap it keeps the
first `cap` entries and NAMES the ones it dropped, on the console and in full
under `schema.meta["schema_clamp"]`. Quietly shipping 24 of 155 systems is the
silent default this repo forbids; saying which 131 are not in the game is not.

A list already inside its cap is left exactly as the model wrote it. Duplicate
names below the cap are the Step 5 fixers' job, not this pass's
(unity_pipeline._programmatic_fix owns script naming,
unity_entity_names.disambiguate_entity_names owns entity names).

`schema.scenes` has no Pydantic cap. :func:`clamp_scene_names` applies the
same SCENE_CAP to it from the programmatic fixer and records the drop beside
these records, so neither scene list is ever truncated silently.

Runs between `llm.extract_json` and `UnitySchema.model_validate`
(src/unity_pipeline.py). Deterministic — no LLM, no network, no reordering
beyond the model's own.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .unity_schema import SCENE_CAP, UnitySchema

SCHEMA_CLAMP_META_KEY = "schema_clamp"
# `schema.scenes` is a bare name list with no Pydantic cap of its own. The
# programmatic fixer caps it at SCENE_CAP (clamp_scene_names, below) and
# records the drop under this key, next to the Step 3 clamp's own records.
SCENES_RECORD_KEY = "scenes"

# SceneSpec._clean_name's rule, so a dropped spec is recorded under the scene
# name the rest of the compiler would have used for it.
_SCENE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")

# Capped list field -> the keys that identify one entry, in priority order. Two
# entries with the same identity are the same thing said twice and the first
# wins. `packages` has no name field, so its identity is the package_id when the
# model filled one and the description otherwise. A field absent from this table
# is never touched, cap or no cap.
_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "scripts":     ("name",),
    "entities":    ("name",),
    "scene_specs": ("name",),
    "packages":    ("package_id", "description"),
}

# How many dropped names go on one console line before it stops listing. The
# FULL list always lands in schema.meta["schema_clamp"], so nothing is hidden —
# this only keeps a 131-entry runaway from burying the rest of the run log.
_NAMES_ON_CONSOLE = 12

# Identity strings are also the console labels; a package's is a whole sentence.
_LABEL_CHARS = 48


def _cap(field: str) -> int | None:
    """The `max_length=` Pydantic actually enforces for this field, read off the
    model itself so the clamp and the schema can never drift apart."""
    spec = UnitySchema.model_fields.get(field)
    if spec is None:
        return None
    for meta in getattr(spec, "metadata", ()) or ():
        n = getattr(meta, "max_length", None)
        if isinstance(n, int):
            return n
    return None


def capped_fields() -> dict[str, int]:
    """{field: cap} for every list this module clamps, straight from the schema."""
    return {f: n for f in _IDENTITY_KEYS
            if (n := _cap(f)) is not None}


def render_cap_reminder() -> str:
    """The capped-list sentence for the Step-3 retry nudge, built from the caps
    themselves so the number the model is told is the number enforced."""
    caps = capped_fields()
    return ", ".join(f'at most {n} "{field}"'
                     for field, n in sorted(caps.items()))


def _identity(item: object, keys: tuple[str, ...]) -> str | None:
    """Case- and whitespace-insensitive identity, or None when the entry carries
    no usable key — an unnamed entry is one this pass cannot judge, so it rides
    through to Pydantic untouched."""
    if not isinstance(item, dict):
        return None
    for key in keys:
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split()).lower()
    return None


def _label(item: object, keys: tuple[str, ...]) -> str:
    """What to call this entry in the log."""
    if isinstance(item, dict):
        for key in keys:
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                text = " ".join(val.split())
                return text if len(text) <= _LABEL_CHARS else text[:_LABEL_CHARS - 1] + "…"
    return "<unnamed>"


def _names(labels: list[str]) -> str:
    head = ", ".join(labels[:_NAMES_ON_CONSOLE])
    extra = len(labels) - _NAMES_ON_CONSOLE
    return head if extra <= 0 else f"{head}, +{extra} more"


def _counted(counts: dict[str, int]) -> str:
    """"PlayerController x131, EnemyAI x2" — a repetition loop writes ONE name a
    hundred times, so listing the copies individually says nothing."""
    return _names([f"{label} x{n}" if n > 1 else label
                   for label, n in counts.items()])


def _message(field: str, cap: int, received: int,
             dup_counts: dict[str, int], drop_labels: list[str]) -> str:
    parts = [f"schema-fill returned {received} `{field}` against a cap of {cap}"]
    if dup_counts:
        parts.append(
            f"{sum(dup_counts.values())} repeated an entry already written, "
            f"collapsed to the first of each ({_counted(dup_counts)})"
        )
    if drop_labels:
        parts.append(
            f"still over the cap — kept the first {cap} and DROPPED "
            f"{len(drop_labels)}: {_names(drop_labels)}. Those are NOT in the "
            f"game. Tighten the brief, or raise max_length on "
            f"UnitySchema.{field} if the project genuinely needs them"
        )
    else:
        parts.append(f"the survivors fit inside {cap} — nothing dropped")
    return " — ".join(parts) + ". Full record: schema.meta['schema_clamp']."


def clamp_capped_lists(schema_dict: dict) -> list[str]:
    """Collapse duplicates and enforce every capped list's cap, IN PLACE.

    Returns fixer-log messages, one per field it had to touch. [] when the fill
    was already inside every cap — the normal case, and the reason this is safe
    to run on every schema before validation.
    """
    if not isinstance(schema_dict, dict):
        return []

    msgs: list[str] = []
    record: dict[str, dict] = {}

    for field, keys in _IDENTITY_KEYS.items():
        cap = _cap(field)
        raw = schema_dict.get(field)
        if cap is None or not isinstance(raw, list) or len(raw) <= cap:
            continue

        deduped: list = []
        dup_counts: dict[str, int] = {}
        seen: set[str] = set()
        for item in raw:
            ident = _identity(item, keys)
            if ident is None:
                deduped.append(item)
                continue
            if ident in seen:
                label = _label(item, keys)
                dup_counts[label] = dup_counts.get(label, 0) + 1
                continue
            seen.add(ident)
            deduped.append(item)

        drop_labels = [_label(item, keys) for item in deduped[cap:]]
        schema_dict[field] = deduped[:cap]

        record[field] = {
            "cap":                cap,
            "received":           len(raw),
            "kept":               len(deduped[:cap]),
            "duplicates_removed": dup_counts,
            "dropped":            drop_labels,
        }
        if field == "scene_specs":
            # What each dropped scene hosted. The membership pass reads it to
            # name the clamp as the reason an entity listed only there spawns
            # nowhere (unity_scene_membership._abstention). Meta only: nothing
            # is placed from it.
            record[field]["dropped_entities"] = _spec_entities(deduped[cap:])
        msgs.append(_message(field, cap, len(raw), dup_counts, drop_labels))

    if record:
        meta = schema_dict.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            schema_dict["meta"] = meta
        meta[SCHEMA_CLAMP_META_KEY] = record
    return msgs


def _spec_entities(specs: list) -> dict[str, list[str]]:
    """{scene name: the entity names it listed} for raw scene_spec dicts that
    listed any. A spec that hosted every group (no list) has no entry: its
    groups are hosted by every other subset-less scene as well."""
    out: dict[str, list[str]] = {}
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = _SCENE_NAME_RE.sub("", str(spec.get("name") or ""))
        listed = spec.get("entities")
        if not name or not isinstance(listed, list):
            continue
        names = [str(n) for n in listed if isinstance(n, str) and n.strip()]
        if names:
            out.setdefault(name, []).extend(names)
    return out


def clamp_scene_names(schema: Any) -> list[str]:
    """Cap ``schema.scenes`` at SCENE_CAP, IN PLACE, and record any drop.

    The caller has already cleaned and de-duplicated the names. A list inside
    the cap is left untouched and nothing is recorded. Over it, the first
    SCENE_CAP names are kept. "Main" is never reported as dropped, because the
    fixer re-inserts it first whatever its position (it is the compiler's hub).
    The record has the Step 3 clamp's shape plus ``dropped_entities`` (what
    each dropped scene's spec listed) and the console ``message``. It goes to
    ``schema.meta["schema_clamp"]["scenes"]``. Returns that message, or [].
    """
    scenes = list(getattr(schema, "scenes", None) or [])
    if len(scenes) <= SCENE_CAP:
        return []
    kept = scenes[:SCENE_CAP]
    dropped = [s for s in scenes[SCENE_CAP:] if s != "Main"]
    schema.scenes = kept
    if not dropped:
        return []
    dropped_set = set(dropped)
    dropped_entities: dict[str, list[str]] = {}
    for spec in getattr(schema, "scene_specs", None) or []:
        if spec.name in dropped_set and spec.entities:
            dropped_entities.setdefault(spec.name, []).extend(spec.entities)
    message = (
        f"the schema declares {len(scenes)} `scenes` against a cap of "
        f"{SCENE_CAP} — kept the first {SCENE_CAP} and DROPPED "
        f"{len(dropped)}: {_names(dropped)}. Those scenes are NOT in the game"
        + (f", and neither is any entity listed only there "
           f"({_names(sorted({n for v in dropped_entities.values() for n in v}))})"
           if dropped_entities else "")
        + ". Trim the brief's scene list, or raise SCENE_CAP in "
        "src/unity_schema.py. Full record: schema.meta['schema_clamp']['scenes']."
    )
    meta = schema.meta if isinstance(getattr(schema, "meta", None), dict) else None
    if meta is not None:
        clamp = meta.get(SCHEMA_CLAMP_META_KEY)
        if not isinstance(clamp, dict):
            clamp = {}
            meta[SCHEMA_CLAMP_META_KEY] = clamp
        clamp[SCENES_RECORD_KEY] = {
            "cap":                SCENE_CAP,
            "received":           len(scenes),
            "kept":               len(kept),
            "duplicates_removed": {},
            "dropped":            dropped,
            "dropped_entities":   dropped_entities,
            "stage":              "5_programmatic_fix",
            "message":            message,
        }
    return [message]


def scene_cap_warnings(schema: Any) -> list[str]:
    """The loud line for a ``scenes`` cap drop recorded on this schema, or [].

    Read from the record rather than returned by the fixer, so the live Step 5
    and the ``unity-validate`` replay say the same thing about one schema."""
    meta = schema.meta if isinstance(getattr(schema, "meta", None), dict) else {}
    clamp = meta.get(SCHEMA_CLAMP_META_KEY)
    row = clamp.get(SCENES_RECORD_KEY) if isinstance(clamp, dict) else None
    if isinstance(row, dict) and row.get("dropped") and row.get("message"):
        return [f"[scene.cap] {row['message']}"]
    return []


def scene_cap_drops(schema: Any) -> list[dict[str, Any]]:
    """One row per scene a scene cap dropped, across both records (the Step 3
    ``scene_specs`` clamp, then the Step 5 ``scenes`` cap), in record order:
    ``{"scene", "cap", "record", "entities"}``. ``entities`` is what the
    dropped scene listed, or None when the record predates
    ``dropped_entities`` (a capture clamped before SCENE_CAP existed), in which
    case what it listed is unknown. [] when no scene was dropped."""
    meta = schema.meta if isinstance(getattr(schema, "meta", None), dict) else {}
    clamp = meta.get(SCHEMA_CLAMP_META_KEY)
    if not isinstance(clamp, dict):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("scene_specs", SCENES_RECORD_KEY):
        row = clamp.get(key)
        if not isinstance(row, dict):
            continue
        listed = row.get("dropped_entities")
        for scene in row.get("dropped") or []:
            name = _SCENE_NAME_RE.sub("", str(scene or ""))
            if not name or name in seen:
                continue
            seen.add(name)
            entities: Optional[list[str]] = None
            if isinstance(listed, dict):
                raw = listed.get(name)
                entities = [str(n) for n in raw] if isinstance(raw, list) else []
            rows.append({"scene": name, "cap": row.get("cap"), "record": key,
                         "entities": entities})
    return rows
