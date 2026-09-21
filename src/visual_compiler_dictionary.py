"""Visual Dictionary authority and deterministic consumption.

The visual dictionary is the boundary between the Step-3 Pipeline LLM and
the deterministic asset resolver.  It is deliberately a sidecar: Concept.txt
is read and hashed, never edited, and no dictionary entry can create a new
entity.  The LLM may only return annotations for the entity names supplied by
the schema fill.

The resolver consumes this artifact by applying annotations to already
declared ``EntitySpec`` objects.  Asset selection remains downstream and
deterministic in ``unity_entity_asset_resolver.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .ident.canon import chash, fhash
from .ident.tags import BUILD
from .unity_concept_entities import extract_named_entities
from .unity_reference_image import encode_reference_image


FORMAT = "visual-compiler-visual-dictionary/v1"

_GENERATED_CONCEPT_ENTITY_RE = re.compile(
    r"^\s*(?:[-*]|\d+\.)\s*\*{0,2}"
    r"([A-Za-z][A-Za-z0-9_ -]*?)\*{0,2}\s*:\s*"
)
_CONCEPT_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9 /&'-]{1,48}$")


class VisualDictionaryError(ValueError):
    """The dictionary is malformed, conflicting, or unsafe to consume."""


def _get(entity: Any, key: str, default: Any = "") -> Any:
    if isinstance(entity, dict):
        return entity.get(key, default)
    return getattr(entity, key, default)


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _file_record(path: str | Path, role: str, repo_root: Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise VisualDictionaryError(f"[visual-dictionary] missing {role}: {p}")
    return {
        "role": role,
        "path": _display_path(p, repo_root),
        "bytes": p.stat().st_size,
        "hash": fhash(p),
    }


def _identity(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"bytes": record["bytes"], "hash": record["hash"]}


def _entity_table(entities: list[Any]) -> str:
    rows: list[str] = []
    for entity in entities:
        rows.append(
            f"  {_get(entity, 'name', '?')} | "
            f"{_get(entity, 'kind', 'prop') or 'prop'} | "
            f"{int(_get(entity, 'count', 1) or 1)} | "
            f"{_get(entity, 'model_hint', '') or '(none)'}"
        )
    return "\n".join(rows)


def _declared_concept_names(concept_text: str) -> set[str]:
    """Read both structured-brief and generated Concept entity indexes.

    ``extract_named_entities`` owns the input-brief format (``- Name: ...``).
    Step 2 Concepts are prose artifacts and commonly use a numbered bold list
    (``1. **PlayerCar**: ...``), so the authority check needs this small
    read-only fallback as well. It returns names only; it never manufactures
    EntitySpec data.
    """
    parsed = {str(item["name"]) for item in extract_named_entities(concept_text)}
    if parsed:
        return parsed
    lines = concept_text.splitlines()
    in_entities = False
    names: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("ENTITIES"):
            in_entities = True
            continue
        if in_entities and _CONCEPT_HEADER_RE.fullmatch(stripped.rstrip(":")):
            break
        if not in_entities:
            continue
        match = _GENERATED_CONCEPT_ENTITY_RE.match(line)
        if match:
            names.add(match.group(1).strip())
    return names


def _canonical_entity_name(name: str) -> str:
    """Match prose display names to schema-safe PascalCase spellings."""
    return "".join(ch.lower() for ch in str(name) if ch.isalnum())


_SYSTEM = """\
You are the visual annotator in a deterministic game asset compiler.
Concept.txt is semantic authority. The supplied entity table is the complete
and closed set of entities you may annotate.

Look at the optional planning board only for appearance: silhouette, material,
palette, and visual construction. Map those observations only onto the named
entities in the table. Never create an entity, rename an entity, add a count,
invent gameplay, or turn a background motif into an entity.

For every supplied entity return a visual ``model_hint`` and a short literal
``sketchfab_query`` suitable for marketplace keyword search. Keep the query to
concrete object words; do not put mechanics, objectives, or prose in it. If
the board does not provide enough evidence, return an empty value rather than
guessing. Output only valid JSON in this shape:
{"style":"low poly","entities":{
  "ExactEntityName":{"model_hint":"...","sketchfab_query":"...",
                      "visual_reference":null}
}}

The ``entities`` object must contain only the exact names from the table.
"""

_USER = """\
IMMUTABLE SEMANTIC CONCEPT (do not edit or reinterpret its gameplay truth):
{concept}

DECLARED ENTITY TABLE (closed set; name | kind | count | existing model_hint):
{entity_table}
{board_instruction}

Return one annotation object for every table row. JSON only.
"""


def _extract_json(raw: str) -> dict[str, Any] | None:
    try:
        clean = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL)
        clean = re.sub(r"```(?:json)?\s*", "", clean).strip()
        start, end = clean.find("{"), clean.rfind("}")
        if start == -1 or end == -1:
            return None
        payload = json.loads(clean[start:end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _source_hash(concept_text: str, concept_file: str | Path | None) -> tuple[dict[str, Any], str]:
    if concept_file is not None:
        record = _file_record(concept_file, "Concept.txt", Path.cwd().resolve())
        return record, record["hash"]
    digest = chash(BUILD, {"artifact": "Concept.txt", "text": concept_text})
    return {
        "role": "Concept.txt",
        "path": None,
        "bytes": len(concept_text.encode("utf-8")),
        "hash": digest,
    }, digest


def _annotation(value: Any) -> tuple[str, str, Any]:
    """Normalize one LLM annotation without accepting entity-shaped data."""
    if isinstance(value, str):
        return "", value.strip(), None
    if not isinstance(value, dict):
        return "", "", None
    return (
        str(value.get("model_hint") or "").strip(),
        str(value.get("sketchfab_query") or "").strip(),
        value.get("visual_reference"),
    )


def _entry(entity: Any, model_hint: str, query: str,
           visual_reference: Any, *, status: str) -> dict[str, Any]:
    return {
        "entity": str(_get(entity, "name", "")),
        "kind": str(_get(entity, "kind", "prop") or "prop"),
        "count": int(_get(entity, "count", 1) or 1),
        "model_hint": model_hint,
        "sketchfab_query": query or None,
        "visual_reference": visual_reference,
        "status": status,
    }


def build_visual_dictionary(
    concept_text: str,
    entities: list[Any],
    llm: Any,
    *,
    board_image: str | Path | None = None,
    concept_file: str | Path | None = None,
    board_file: str | Path | None = None,
    seed: int = 0,
    model: str | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Ask the Step-3 LLM for annotations and return a sidecar payload.

    This function never mutates ``entities`` or the Concept file.  A failed
    annotation is retained as explicit unfulfilled entries so the resolver can
    fall back deterministically while the run log still shows the reason.
    Unknown entity keys from the LLM are a hard error: accepting them would
    turn a visual suggestion into semantic invention.
    """
    if not concept_text.strip():
        raise VisualDictionaryError("[visual-dictionary] empty Concept.txt refused")
    names = [str(_get(entity, "name", "")).strip() for entity in entities]
    if any(not name for name in names):
        raise VisualDictionaryError("[visual-dictionary] every entity needs a name")
    if len(set(names)) != len(names):
        raise VisualDictionaryError("[visual-dictionary] duplicate entity names refused")

    # The semantic Concept prompt asks for a compact ENTITIES index.  When it
    # is present, use the repository's same deterministic parser to reject a
    # schema entity that the Concept never declared.  Older Concepts without
    # that section remain consumable, but the dictionary still has a closed
    # schema entity set and rejects any new LLM key below.
    declared_names = _declared_concept_names(concept_text)
    if declared_names:
        declared_keys = {_canonical_entity_name(name) for name in declared_names}
        undeclared_schema = sorted(
            name for name in set(names)
            if _canonical_entity_name(name) not in declared_keys
        )
        if undeclared_schema:
            raise VisualDictionaryError(
                "[visual-dictionary] schema contains entities absent from "
                "Concept.txt: " + ", ".join(undeclared_schema)
            )

    root = Path(repo_root or Path.cwd()).resolve()
    concept_record, _ = _source_hash(concept_text, concept_file)
    board_record = None
    if board_file is not None:
        board_record = _file_record(board_file, "visual board", root)
    elif board_image is not None:
        board_record = _file_record(board_image, "visual board", root)

    board_instruction = "\nNO VISUAL BOARD WAS SUPPLIED. Use only the Concept and existing hints."
    images = None
    if board_image is not None:
        board_instruction = (
            "\nVISUAL BOARD ATTACHED: annotate appearance only. The board is not "
            "allowed to add or rename entities."
        )
        images = [encode_reference_image(board_image)]

    raw = None
    failure = None
    try:
        raw = llm.chat(
            system=_SYSTEM,
            user=_USER.format(
                concept=concept_text.strip(),
                entity_table=_entity_table(entities),
                board_instruction=board_instruction,
            ),
            images=images,
            temperature=0.2,
            thinking=False,
            seed=seed or None,
        )
    except Exception as exc:  # noqa: BLE001 - retain explicit unfulfilled data
        failure = f"{type(exc).__name__}: {exc}"

    payload = _extract_json(raw) if raw is not None else None
    mapping = payload.get("entities") if payload else None
    style = str(payload.get("style") or "").strip() if payload else ""
    if payload is not None and not isinstance(mapping, dict):
        failure = "annotation JSON has no object-valued 'entities' mapping"
        mapping = {}
    if payload is None and failure is None:
        failure = "annotation response was not a JSON object"
    if isinstance(mapping, dict):
        unknown = sorted(set(mapping) - set(names))
        if unknown:
            raise VisualDictionaryError(
                "[visual-dictionary] LLM invented undeclared entities: "
                + ", ".join(unknown)
            )

    entries: list[dict[str, Any]] = []
    for entity in entities:
        name = str(_get(entity, "name", ""))
        model_hint, query, visual_reference = _annotation(
            mapping.get(name) if isinstance(mapping, dict) else None
        )
        if not model_hint:
            model_hint = str(_get(entity, "model_hint", "") or "")
        if style and query and not query.lower().startswith(style.lower() + " "):
            query = f"{style} {query}".strip()
        status = "query_present" if query else (
            "unfulfilled_" + ("annotation_error" if failure else "no_sketchfab_query")
        )
        entries.append(_entry(entity, model_hint, query, visual_reference, status=status))

    identity_payload = {
        "format": FORMAT,
        "concept": _identity(concept_record),
        "board": _identity(board_record),
        "model": model,
        "seed": seed,
        "style": style,
        "entries": entries,
    }
    result: dict[str, Any] = {
        "format": FORMAT,
        "authority": "visual_annotation",
        "status": "captured" if not failure else "captured_with_unfulfilled_entries",
        "concept": concept_record,
        "board": board_record,
        "run": {"model": model, "seed": seed, "style": style},
        "entries": entries,
        "contract": {
            "concept_is_read_only": True,
            "board_can_annotate_only_declared_entities": True,
            "concept_entity_index_checked": bool(declared_names),
            "resolver_is_deterministic": True,
            "llm_selected_runtime_asset": False,
        },
        "identity": {
            "hash": chash(BUILD, identity_payload),
            "payload": identity_payload,
        },
    }
    if failure:
        result["annotation_error"] = failure
    return result


def write_visual_dictionary(dictionary: dict[str, Any], output: str | Path) -> Path:
    """Write a dictionary once; never overwrite an existing capture."""
    path = Path(output)
    if path.exists():
        raise VisualDictionaryError(
            f"[visual-dictionary] refusing to overwrite existing sidecar: {path}"
        )
    if dictionary.get("format") != FORMAT:
        raise VisualDictionaryError("[visual-dictionary] unsupported dictionary format")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dictionary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_visual_dictionary(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise VisualDictionaryError(f"[visual-dictionary] missing sidecar: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualDictionaryError(
            f"[visual-dictionary] invalid JSON: {p}: {exc}"
        ) from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise VisualDictionaryError(
            f"[visual-dictionary] expected {FORMAT}: {p}"
        )
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise VisualDictionaryError("[visual-dictionary] entries must be a list")
    _validate_entries(entries)
    return data


def _validate_entries(entries: list[Any]) -> None:
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("entity") or "").strip():
            raise VisualDictionaryError("[visual-dictionary] every entry needs an entity")
        name = str(entry["entity"])
        if name in names:
            raise VisualDictionaryError(
                f"[visual-dictionary] duplicate entry for entity {name!r}"
            )
        names.add(name)
        if "sketchfab_query" not in entry or "model_hint" not in entry:
            raise VisualDictionaryError(
                f"[visual-dictionary] incomplete entry for entity {name!r}"
            )


def apply_visual_dictionary(
    schema: Any,
    dictionary: dict[str, Any],
    *,
    expected_concept_hash: str | None = None,
    expected_board_hash: str | None = None,
) -> int:
    """Apply sidecar hints to existing schema entities, deterministically.

    No entity is appended and no Concept file is opened for writing.  The
    caller supplies the expected hashes from its immutable inputs; mismatches
    are rejected before any entity is changed.
    """
    if dictionary.get("format") != FORMAT:
        raise VisualDictionaryError("[visual-dictionary] unsupported dictionary format")
    entries = dictionary.get("entries")
    if not isinstance(entries, list):
        raise VisualDictionaryError("[visual-dictionary] entries must be a list")
    _validate_entries(entries)

    concept = dictionary.get("concept") or {}
    actual_concept_hash = concept.get("hash") if isinstance(concept, dict) else None
    if expected_concept_hash and actual_concept_hash != expected_concept_hash:
        raise VisualDictionaryError(
            "[visual-dictionary] Concept.txt hash mismatch: "
            f"expected {expected_concept_hash}, got {actual_concept_hash}"
        )
    board = dictionary.get("board")
    actual_board_hash = board.get("hash") if isinstance(board, dict) else None
    if expected_board_hash and actual_board_hash != expected_board_hash:
        raise VisualDictionaryError(
            "[visual-dictionary] visual board hash mismatch: "
            f"expected {expected_board_hash}, got {actual_board_hash}"
        )

    schema_entities = list(getattr(schema, "entities", []) or [])
    by_name = {str(_get(entity, "name", "")): entity for entity in schema_entities}
    if len(by_name) != len(schema_entities):
        raise VisualDictionaryError("[visual-dictionary] schema has duplicate entity names")
    unknown = sorted(
        str(entry.get("entity")) for entry in entries
        if str(entry.get("entity")) not in by_name
    )
    if unknown:
        raise VisualDictionaryError(
            "[visual-dictionary] sidecar references undeclared schema entities: "
            + ", ".join(unknown)
        )

    updated = 0
    for entry in entries:
        entity = by_name[str(entry["entity"])]
        hint = str(entry.get("model_hint") or "").strip()
        query = str(entry.get("sketchfab_query") or "").strip()
        if hint:
            if isinstance(entity, dict):
                entity["model_hint"] = hint
            else:
                entity.model_hint = hint
        if query:
            if isinstance(entity, dict):
                entity["sketchfab_query"] = query
            else:
                entity.sketchfab_query = query
        if hint or query:
            updated += 1
    return updated


def concept_file_hash(path: str | Path) -> str:
    """Return the exact-byte Concept.txt identity used by sidecar validation."""
    return fhash(path)


def board_file_hash(path: str | Path) -> str:
    """Return the exact-byte visual-board identity used by sidecar validation."""
    return fhash(path)
