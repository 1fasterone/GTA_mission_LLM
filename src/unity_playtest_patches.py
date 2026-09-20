"""Deterministic schema/atom patching and snapshot diffing for playtest fixes.

This module is the mutation boundary for the mechanics-playtest improvement
loop.  It accepts only JSON snapshots and reviewed patch records.  It never
calls an LLM, edits generated Unity output, or mutates one of its arguments.

The low-level ``apply_schema_patch`` and ``apply_atom_patch`` helpers return
detached updated snapshots.  ``apply_playtest_improvement_plan`` applies the
two update blocks from a Slice 4 plan and returns the updated snapshots plus
the required deterministic diffs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .atoms import get_atom


class PlaytestPatchError(ValueError):
    """Base error for a malformed or unsafe playtest patch."""


class PatchPreconditionError(PlaytestPatchError):
    """Raised when a patch was created against a different snapshot."""


class PatchIdentityError(PlaytestPatchError):
    """Raised for duplicate, unknown, or ambiguous atom identities."""


_MISSING = object()
_PATCH_OPERATIONS = frozenset({"add", "modify", "remove", "abstain"})
_EXTENSION_NAMES = frozenset({
    "extensions_fields", "extensions_methods", "extensions_metadata",
})
_RULE_WORDS = frozenset({
    "rule", "rules", "transition", "transitions", "dependency",
    "dependencies", "requires", "commands",
})
_CS_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _clone_json(value: Any, label: str) -> Any:
    """Return a detached strict-JSON copy, or fail loudly."""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-compatible") from exc
    return copy.deepcopy(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_hash(value: Any) -> str:
    """Return a stable SHA-256 hash for a JSON snapshot."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _as_operations(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise PlaytestPatchError(f"{label} must be a list")
    operations: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PlaytestPatchError(f"{label}[{index}] must be an object")
        operation = dict(item)
        action = str(operation.get("operation", "")).strip().lower()
        if action not in _PATCH_OPERATIONS:
            raise PlaytestPatchError(
                f"{label}[{index}] has unsupported operation {action!r}"
            )
        operation["operation"] = action
        operation.setdefault("id", f"patch-{index:04d}")
        operation["id"] = str(operation["id"])
        if not operation["id"]:
            raise PlaytestPatchError(f"{label}[{index}] has an empty id")
        operations.append(operation)
    ids = [operation["id"] for operation in operations]
    if len(ids) != len(set(ids)):
        raise PatchIdentityError(f"{label} contains duplicate operation ids")
    return operations


def _path_tokens(path: Any, label: str) -> list[str | int]:
    if not isinstance(path, str) or not path.strip():
        raise PlaytestPatchError(f"{label} must be a non-empty path")
    text = path.strip()
    for prefix in ("SCHEMAS.", "schemas.", "schema.", "ATOM_IR.", "atom_ir."):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if text in {"SCHEMAS", "schemas", "ATOM_IR", "atom_ir"}:
        text = ""
    if text.startswith("$."):
        text = text[2:]
    elif text == "$":
        text = ""
    if not text:
        return []

    tokens: list[str | int] = []
    position = 0
    while position < len(text):
        if text[position] == ".":
            position += 1
            if position == len(text):
                raise PlaytestPatchError(f"{label} has a trailing dot")
        if text[position] == "[":
            end = text.find("]", position + 1)
            if end < 0:
                raise PlaytestPatchError(f"{label} has an unterminated index")
            index = text[position + 1:end]
            if not index.isdigit():
                raise PlaytestPatchError(f"{label} has a non-numeric list index")
            tokens.append(int(index))
            position = end + 1
            continue
        start = position
        while position < len(text) and text[position] not in ".[":
            position += 1
        key = text[start:position]
        if not key:
            raise PlaytestPatchError(f"{label} has an empty path component")
        tokens.append(key)
    return tokens


def _path_string(tokens: Sequence[str | int]) -> str:
    result = ""
    for token in tokens:
        if isinstance(token, int):
            result += f"[{token}]"
        else:
            result = f"{result}.{token}" if result else token
    return result


def _lookup(document: Any, tokens: Sequence[str | int], label: str) -> tuple[bool, Any]:
    current = document
    for token in tokens:
        if isinstance(current, Mapping):
            if not isinstance(token, str) or token not in current:
                return False, _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not isinstance(token, int) or token < 0 or token >= len(current):
                return False, _MISSING
            current = current[token]
        else:
            raise PlaytestPatchError(
                f"{label} traverses a scalar at {_path_string(tokens)}"
            )
    return True, current


def _parent(document: Any, tokens: Sequence[str | int], label: str) -> tuple[Any, str | int]:
    if not tokens:
        raise PlaytestPatchError(f"{label} cannot target the document root")
    parent_exists, parent = _lookup(document, tokens[:-1], label)
    if not parent_exists:
        raise PatchPreconditionError(
            f"{label} parent {_path_string(tokens[:-1]) or '<root>'} does not exist"
        )
    return parent, tokens[-1]


def _expected_matches(operation: Mapping[str, Any], current: Any) -> bool:
    if "expected_hash" in operation:
        return snapshot_hash(current) == str(operation["expected_hash"])
    expected = operation.get("expected", _MISSING)
    if isinstance(expected, Mapping) and set(expected) == {"sha256"}:
        return snapshot_hash(current) == str(expected["sha256"])
    return expected is not _MISSING and current == expected


def _replacement(operation: Mapping[str, Any], label: str) -> Any:
    if "replacement" in operation:
        return _clone_json(operation["replacement"], f"{label}.replacement")
    if "value" in operation:
        return _clone_json(operation["value"], f"{label}.value")
    raise PlaytestPatchError(f"{label} requires replacement")


def _extension_parts(tokens: Sequence[str | int]) -> set[str]:
    return {
        str(token).lower()
        for token in tokens
        if isinstance(token, str) and token.lower() in _EXTENSION_NAMES
    }


def _schema_keys(value: Any) -> set[str]:
    """Collect field names already proven by the current schema snapshot."""
    keys: set[str] = set()
    if isinstance(value, Mapping):
        keys.update(str(key) for key in value)
        for child in value.values():
            keys.update(_schema_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_schema_keys(child))
    return keys


def _validate_existing_schema_shape(value: Any, known_keys: set[str], label: str) -> None:
    """Reject new normal-schema keys hidden inside a list-record addition."""
    if isinstance(value, Mapping):
        unknown = sorted(str(key) for key in value if str(key) not in known_keys)
        if unknown:
            raise PlaytestPatchError(
                f"{label} would invent normal schema field(s): {', '.join(unknown)}"
            )
        for key, child in value.items():
            _validate_existing_schema_shape(child, known_keys, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_existing_schema_shape(child, known_keys, f"{label}[{index}]")


def _validate_extension_value(
    tokens: Sequence[str | int],
    value: Any,
    *,
    document: Any,
) -> None:
    """Validate the structural part of the existing extension contract.

    The normal schema has no field-invention escape hatch.  For extension
    entries this checks the same facts the safe block can prove without a
    generator-specific reserved-name set: JSON shape, C# identifier syntax,
    and sibling-name collisions.
    """
    parts = _extension_parts(tokens)
    if not parts:
        return
    if not isinstance(value, (Mapping, list, str, int, float, bool)) and value is not None:
        raise PlaytestPatchError("extension value must be JSON-compatible")

    if "extensions_fields" in parts:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, Mapping):
                raise PlaytestPatchError("extensions_fields entries must be objects")
            name = item.get("name")
            if not isinstance(name, str) or not _CS_IDENTIFIER.fullmatch(name):
                raise PlaytestPatchError("extensions_fields requires a valid C# name")
            if "type" in item and not isinstance(item["type"], str):
                raise PlaytestPatchError("extensions_fields.type must be a string")
            for key in ("default", "header", "tooltip"):
                if key in item and item[key] is not None and not isinstance(item[key], str):
                    raise PlaytestPatchError(f"extensions_fields.{key} must be a string or null")

    if "extensions_methods" in parts:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, Mapping):
                raise PlaytestPatchError("extensions_methods entries must be objects")
            if not isinstance(item.get("signature"), str) or not item["signature"].strip():
                raise PlaytestPatchError("extensions_methods requires a signature")
            if "body" in item and not isinstance(item["body"], str):
                raise PlaytestPatchError("extensions_methods.body must be a string")

    if "extensions_metadata" in parts and not isinstance(value, (Mapping, list, str, int, float, bool)) and value is not None:
        raise PlaytestPatchError("extensions_metadata must remain JSON-compatible")

    # Adding a new extension field/method must not reuse a sibling entry's
    # name.  The full generator-specific safe block still owns reserved C#
    # members; this patch boundary refuses the deterministic collision it can
    # prove from the snapshot itself.
    if not isinstance(value, Mapping) or "name" not in value:
        return
    name = str(value["name"]).lower()
    if not name:
        return
    parent_tokens = list(tokens[:-1])
    exists, siblings = _lookup(document, parent_tokens, "extension sibling lookup")
    if exists and isinstance(siblings, list):
        for sibling in siblings:
            if isinstance(sibling, Mapping) and str(sibling.get("name", "")).lower() == name:
                raise PatchIdentityError(f"duplicate extension member name {value['name']!r}")
    extension_index = next(
        (index for index, token in enumerate(tokens)
         if isinstance(token, str) and token.lower() in _EXTENSION_NAMES),
        None,
    )
    if extension_index is not None:
        container_exists, container = _lookup(
            document, tokens[:extension_index], "extension container lookup"
        )
        if container_exists and isinstance(container, Mapping):
            fixed_names = {
                str(key).lower()
                for key in container
                if str(key).lower() not in _EXTENSION_NAMES
            }
            if name in fixed_names:
                raise PatchIdentityError(
                    f"extension member {value['name']!r} collides with a schema field"
                )


def _set_path(document: Any, tokens: Sequence[str | int], value: Any, operation: str, label: str) -> None:
    parent, key = _parent(document, tokens, label)
    if isinstance(parent, Mapping):
        if not isinstance(key, str):
            raise PlaytestPatchError(f"{label} uses a list index on an object")
        present = key in parent
        if operation == "add" and present:
            raise PatchPreconditionError(f"{label} already exists")
        if operation in {"modify", "remove"} and not present:
            raise PatchPreconditionError(f"{label} does not exist")
        if operation == "remove":
            del parent[key]
        else:
            parent[key] = value
        return
    if isinstance(parent, list):
        if not isinstance(key, int):
            raise PlaytestPatchError(f"{label} uses an object key on a list")
        if operation == "add":
            if key < 0 or key > len(parent):
                raise PatchPreconditionError(f"{label} insertion index is out of range")
            parent.insert(key, value)
        else:
            if key < 0 or key >= len(parent):
                raise PatchPreconditionError(f"{label} index is out of range")
            if operation == "remove":
                parent.pop(key)
            else:
                parent[key] = value
        return
    raise PlaytestPatchError(f"{label} parent is not mutable")


def _apply_document_operations(
    document: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    label: str,
    schema_document: bool,
    strict: bool,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    updated = _clone_json(document, label)
    known_schema_keys = _schema_keys(updated) if schema_document else set()
    applied: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for index, operation in enumerate(_as_operations(operations, label + ".patch")):
        action = operation["operation"]
        op_label = f"{label}.patch[{index}] ({operation['id']})"
        if action == "abstain":
            abstentions.append(copy.deepcopy(operation))
            if strict:
                raise PlaytestPatchError(f"{op_label} is an explicit abstention")
            continue
        tokens = _path_tokens(operation.get("target"), op_label + ".target")
        exists, current = _lookup(updated, tokens, op_label + ".target")
        if action in {"modify", "remove"}:
            if not exists:
                raise PatchPreconditionError(f"{op_label} target does not exist")
            if not _expected_matches(operation, current):
                raise PatchPreconditionError(f"{op_label} expected-value precondition failed")
        elif action == "add" and "expected" in operation and operation["expected"] is not None:
            raise PatchPreconditionError(f"{op_label} add expected value must be null")

        replacement = None if action == "remove" else _replacement(operation, op_label)
        if schema_document and action == "add":
            parent_exists, parent = _lookup(updated, tokens[:-1], op_label + ".target")
            if not parent_exists:
                raise PatchPreconditionError(f"{op_label} parent does not exist")
            if isinstance(parent, Mapping) and not _extension_parts(tokens):
                raise PlaytestPatchError(
                    f"{op_label} would invent a normal schema field; use extensions_* or a reviewed migration"
                )
            if isinstance(parent, list) and not _extension_parts(tokens):
                _validate_existing_schema_shape(replacement, known_schema_keys, op_label)
        if schema_document and action == "modify" and not _extension_parts(tokens):
            _validate_existing_schema_shape(replacement, known_schema_keys, op_label)
        if schema_document and action != "remove" and _extension_parts(tokens):
            _validate_extension_value(tokens, replacement, document=updated)
        _set_path(updated, tokens, replacement, action, op_label)
        applied.append({
            "id": operation["id"],
            "operation": action,
            "target": _path_string(tokens),
            "trigger": str(operation.get("trigger", "")),
            "source_report_path": str(operation.get("trigger", "")),
        })
    return updated, applied, abstentions


def _atom_name(record: Any, label: str) -> str:
    if isinstance(record, str):
        name = record.strip()
    elif isinstance(record, Mapping):
        name = str(record.get("atom") or record.get("name") or "").strip()
    else:
        raise PatchIdentityError(f"{label} must be an atom name or object")
    if not name:
        raise PatchIdentityError(f"{label} has no atom name")
    if get_atom(name) is None:
        raise PatchIdentityError(f"{label} names unknown atom {name!r}")
    return name


def _atom_identity(record: Any, index: int) -> str:
    if isinstance(record, str):
        return _canonical_json(["", record, ""])
    explicit = str(record.get("id", "")).strip()
    if explicit:
        return explicit
    return _canonical_json([
        str(record.get("owner", "") or ""),
        str(record.get("atom") or record.get("name") or ""),
        str(record.get("label", "") or ""),
    ])


def _validate_atom_collection(records: Any, label: str) -> list[Any]:
    if not isinstance(records, list):
        raise PlaytestPatchError(f"{label} must be a list")
    seen: dict[str, int] = {}
    for index, record in enumerate(records):
        _atom_name(record, f"{label}[{index}]")
        identity = _atom_identity(record, index)
        if identity in seen:
            raise PatchIdentityError(
                f"{label} has duplicate atom identity {identity!r} at indexes {seen[identity]} and {index}"
            )
        seen[identity] = index
        if isinstance(record, Mapping) and "args" in record and not isinstance(record["args"], Mapping):
            raise PatchIdentityError(f"{label}[{index}].args must be an object")
    return records


def _atom_target_index(records: list[Any], target: Any, *, for_add: bool) -> int:
    if not isinstance(target, str) or not target.strip():
        raise PatchIdentityError("atom patch target must be an explicit identity or list path")
    text = target.strip()
    for prefix in ("ATOM_IR", "atom_ir", "atoms"):
        if text == prefix:
            text = ""
            break
        if text.startswith(prefix + "."):
            text = text[len(prefix) + 1:]
            break
    if text in {"", "append", "-"}:
        if for_add:
            return len(records)
        raise PatchIdentityError("atom mutation target cannot be the list root")
    if text.isdigit():
        index = int(text)
        if for_add and index <= len(records):
            return index
        if not for_add and index < len(records):
            return index
        raise PatchPreconditionError(f"atom target index {index} is out of range")
    if text.startswith("["):
        tokens = _path_tokens(text, "atom target")
        if len(tokens) == 1 and isinstance(tokens[0], int):
            index = tokens[0]
            if (for_add and index <= len(records)) or (not for_add and index < len(records)):
                return index
        raise PatchIdentityError("atom target path must identify one list index")
    matches = [index for index, record in enumerate(records) if _atom_identity(record, index) == text]
    if len(matches) != 1:
        if not matches:
            raise PatchIdentityError(f"atom target identity {text!r} was not found")
        raise PatchIdentityError(f"atom target identity {text!r} is ambiguous")
    return matches[0]


def _atom_replacement(operation: Mapping[str, Any], label: str) -> Any:
    replacement = _replacement(operation, label)
    if not isinstance(replacement, (str, Mapping)):
        raise PatchIdentityError(f"{label}.replacement must be an atom name or object")
    _atom_name(replacement, label + ".replacement")
    if isinstance(replacement, Mapping) and "args" in replacement and not isinstance(replacement["args"], Mapping):
        raise PatchIdentityError(f"{label}.replacement.args must be an object")
    return replacement


def _apply_atom_operations(
    atom_ir: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    strict: bool,
    enforce_mutation_rules: bool,
    accepted_schema_ids: set[str],
) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]]]:
    updated = _clone_json(atom_ir, "ATOM_IR")
    _validate_atom_collection(updated, "ATOM_IR")
    applied: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    for index, operation in enumerate(_as_operations(operations, "ATOM_IR.patch")):
        action = operation["operation"]
        op_label = f"ATOM_IR.patch[{index}] ({operation['id']})"
        if action == "abstain":
            abstentions.append(copy.deepcopy(operation))
            if strict:
                raise PlaytestPatchError(f"{op_label} is an explicit abstention")
            continue
        if action == "add" and enforce_mutation_rules:
            required_by = operation.get("required_by") or operation.get("schema_operation_id")
            if not required_by or str(required_by) not in accepted_schema_ids:
                raise PlaytestPatchError(
                    f"{op_label} atom additions require an accepted schema operation"
                )
        if action == "remove" and enforce_mutation_rules:
            validated_rule = operation.get("validated_schema_rule")
            if not validated_rule:
                raise PlaytestPatchError(
                    f"{op_label} atom removals require validated_schema_rule"
                )

        replacement = None if action == "remove" else _atom_replacement(operation, op_label)
        target_index = _atom_target_index(updated, operation.get("target"), for_add=action == "add")
        if action in {"modify", "remove"}:
            current = updated[target_index]
            if not _expected_matches(operation, current):
                raise PatchPreconditionError(f"{op_label} expected-value precondition failed")
        if action == "add":
            updated.insert(target_index, replacement)
        elif action == "remove":
            updated.pop(target_index)
        else:
            updated[target_index] = replacement
        _validate_atom_collection(updated, "ATOM_IR")
        applied.append({
            "id": operation["id"],
            "operation": action,
            "target": str(operation.get("target", "")),
            "trigger": str(operation.get("trigger", "")),
            "source_report_path": str(operation.get("trigger", "")),
        })
    return updated, applied, abstentions


def _is_rule_path(path: str) -> bool:
    parts = set(path.replace("[", ".").replace("]", "").split("."))
    return bool(_RULE_WORDS & {part.lower() for part in parts})


def _leaf_paths(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        result: list[tuple[str, Any]] = []
        for key in sorted(value, key=str):
            child = f"{path}.{key}" if path else str(key)
            result.extend(_leaf_paths(value[key], child))
        return result
    return [(path, value)]


def diff_schema_snapshots(
    previous: Any,
    updated: Any,
    *,
    operation_id: str = "snapshot-diff",
    source_report_path: str = "SCHEMAS",
) -> dict[str, Any]:
    """Return stable field/rule diffs with provenance on every entry."""
    if previous is None:
        return {
            "status": "baseline_missing",
            "reason": "PREVIOUS_SCHEMAS was not supplied",
            "added_fields": [], "removed_fields": [], "modified_fields": [],
            "added_rules": [], "removed_rules": [], "modified_rules": [],
        }
    old = _clone_json(previous, "PREVIOUS_SCHEMAS")
    new = _clone_json(updated, "UPDATED_SCHEMAS")
    old_paths = dict(_leaf_paths(old))
    new_paths = dict(_leaf_paths(new))

    def entry(path: str, old_value: Any = None, new_value: Any = None) -> dict[str, Any]:
        return {
            "path": path,
            "old": old_value,
            "new": new_value,
            "operation_id": operation_id,
            "source_report_path": source_report_path,
        }

    result = {
        "status": "unchanged",
        "added_fields": [], "removed_fields": [], "modified_fields": [],
        "added_rules": [], "removed_rules": [], "modified_rules": [],
    }
    for path in sorted(set(new_paths) - set(old_paths)):
        key = "added_rules" if _is_rule_path(path) else "added_fields"
        result[key].append(entry(path, new_value=new_paths[path]))
    for path in sorted(set(old_paths) - set(new_paths)):
        key = "removed_rules" if _is_rule_path(path) else "removed_fields"
        result[key].append(entry(path, old_value=old_paths[path]))
    for path in sorted(set(old_paths) & set(new_paths)):
        if old_paths[path] == new_paths[path]:
            continue
        key = "modified_rules" if _is_rule_path(path) else "modified_fields"
        result[key].append(entry(path, old_paths[path], new_paths[path]))
    if any(result[key] for key in result if key not in {"status"}):
        result["status"] = "modified"
    return result


def _atom_diff_entry(
    identity: str,
    *,
    old: Any = None,
    new: Any = None,
    operation_id: str,
    source_report_path: str,
) -> dict[str, Any]:
    return {
        "id": identity,
        "old": old,
        "new": new,
        "operation_id": operation_id,
        "source_report_path": source_report_path,
    }


def _transition_atom(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    values = [record.get(key) for key in ("kind", "type", "atom", "name")]
    return any(str(value or "").lower() in {"rule", "transition", "dependency"} for value in values)


def diff_atom_ir(
    previous: Any,
    updated: Any,
    *,
    operation_id: str = "atom-diff",
    source_report_path: str = "ATOM_IR",
) -> dict[str, Any]:
    """Return stable atom and transition diffs with provenance."""
    if previous is None:
        return {
            "status": "baseline_missing",
            "reason": "PREVIOUS_ATOM_IR was not supplied",
            "added_atoms": [], "removed_atoms": [], "modified_atoms": [],
            "added_transitions": [], "removed_transitions": [], "modified_transitions": [],
        }
    old = _clone_json(previous, "PREVIOUS_ATOM_IR")
    new = _clone_json(updated, "UPDATED_ATOM_IR")
    _validate_atom_collection(old, "PREVIOUS_ATOM_IR")
    _validate_atom_collection(new, "UPDATED_ATOM_IR")

    def indexed(records: list[Any]) -> dict[str, Any]:
        return {_atom_identity(record, index): record for index, record in enumerate(records)}

    old_by_id = indexed(old)
    new_by_id = indexed(new)
    result = {
        "status": "unchanged",
        "added_atoms": [], "removed_atoms": [], "modified_atoms": [],
        "added_transitions": [], "removed_transitions": [], "modified_transitions": [],
    }
    for identity in sorted(set(new_by_id) - set(old_by_id)):
        key = "added_transitions" if _transition_atom(new_by_id[identity]) else "added_atoms"
        result[key].append(_atom_diff_entry(
            identity, new=new_by_id[identity], operation_id=operation_id,
            source_report_path=source_report_path,
        ))
    for identity in sorted(set(old_by_id) - set(new_by_id)):
        key = "removed_transitions" if _transition_atom(old_by_id[identity]) else "removed_atoms"
        result[key].append(_atom_diff_entry(
            identity, old=old_by_id[identity], operation_id=operation_id,
            source_report_path=source_report_path,
        ))
    for identity in sorted(set(old_by_id) & set(new_by_id)):
        if old_by_id[identity] == new_by_id[identity]:
            continue
        key = "modified_transitions" if _transition_atom(new_by_id[identity]) else "modified_atoms"
        result[key].append(_atom_diff_entry(
            identity, old=old_by_id[identity], new=new_by_id[identity],
            operation_id=operation_id, source_report_path=source_report_path,
        ))
    if any(result[key] for key in result if key != "status"):
        result["status"] = "modified"
    return result


def apply_schema_patches(
    schemas: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Apply schema operations and return snapshots plus operation status."""
    updated, applied, abstentions = _apply_document_operations(
        schemas, operations, label="SCHEMAS", schema_document=True, strict=strict,
    )
    return {
        "UPDATED_SCHEMAS": updated,
        "APPLIED_OPERATIONS": applied,
        "ABSTENTIONS": abstentions,
    }


def apply_schema_patch(
    schemas: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    strict: bool = True,
) -> Any:
    """Apply schema operations and return only the detached updated snapshot."""
    return apply_schema_patches(schemas, operations, strict=strict)["UPDATED_SCHEMAS"]


def apply_atom_patches(
    atom_ir: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    strict: bool = True,
    enforce_mutation_rules: bool = False,
    accepted_schema_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Apply atom operations and return snapshots plus operation status."""
    updated, applied, abstentions = _apply_atom_operations(
        atom_ir,
        operations,
        strict=strict,
        enforce_mutation_rules=enforce_mutation_rules,
        accepted_schema_ids=accepted_schema_ids or set(),
    )
    return {
        "UPDATED_ATOM_IR": updated,
        "APPLIED_OPERATIONS": applied,
        "ABSTENTIONS": abstentions,
    }


def apply_atom_patch(
    atom_ir: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    strict: bool = True,
) -> Any:
    """Apply atom operations and return only the detached updated snapshot."""
    return apply_atom_patches(atom_ir, operations, strict=strict)["UPDATED_ATOM_IR"]


def apply_playtest_improvement_plan(
    schemas: Any,
    atom_ir: Any,
    plan: Mapping[str, Any],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Apply a reviewed Slice 4 plan without mutating any input snapshot.

    Resolver operations are deliberately rejected here; Slice 6 owns the
    reviewed resolver ledger.  In strict mode an explicit abstention is a
    blocker.  In review mode (``strict=False``) abstentions are preserved in
    the result and accepted operations still apply to detached copies.
    """
    if not isinstance(plan, Mapping):
        raise PlaytestPatchError("PLAYTEST_IMPROVEMENT_PLAN must be an object")
    schema_block = plan.get("REQUIRED_SCHEMA_UPDATES")
    atom_block = plan.get("REQUIRED_ATOM_UPDATES")
    if not isinstance(schema_block, Mapping) or not isinstance(atom_block, Mapping):
        raise PlaytestPatchError("plan is missing required schema/atom update blocks")

    resolver_block = plan.get("RESOLVER_PATCH_GENERATOR", {})
    resolver_ops = resolver_block.get("RESOLVER_PATCH", []) if isinstance(resolver_block, Mapping) else []
    if any(str(item.get("operation", "")).lower() != "abstain" for item in resolver_ops if isinstance(item, Mapping)):
        raise PlaytestPatchError("resolver patches belong to Slice 6 and cannot be applied in Slice 5")

    schema_ops = schema_block.get("SCHEMA_PATCH", [])
    atom_ops = atom_block.get("ATOM_PATCH", [])
    schema_result = apply_schema_patches(schemas, schema_ops, strict=strict)
    applied_schema_ids = {item["id"] for item in schema_result["APPLIED_OPERATIONS"]}
    atom_result = apply_atom_patches(
        atom_ir,
        atom_ops,
        strict=strict,
        enforce_mutation_rules=True,
        accepted_schema_ids=applied_schema_ids,
    )
    updated_schemas = schema_result["UPDATED_SCHEMAS"]
    updated_atoms = atom_result["UPDATED_ATOM_IR"]
    schema_diff = diff_schema_snapshots(schemas, updated_schemas)
    atom_diff = diff_atom_ir(atom_ir, updated_atoms)
    return {
        "status": "modified" if schema_diff["status"] == "modified" or atom_diff["status"] == "modified" else "unchanged",
        "UPDATED_SCHEMAS": updated_schemas,
        "UPDATED_ATOM_IR": updated_atoms,
        "SCHEMA_DIFF": schema_diff,
        "ATOM_DIFF": atom_diff,
        "APPLIED_OPERATIONS": schema_result["APPLIED_OPERATIONS"] + atom_result["APPLIED_OPERATIONS"],
        "ABSTENTIONS": schema_result["ABSTENTIONS"] + atom_result["ABSTENTIONS"],
        "SNAPSHOT_HASHES": {
            "schemas": snapshot_hash(updated_schemas),
            "atom_ir": snapshot_hash(updated_atoms),
        },
    }


apply_patch_plan = apply_playtest_improvement_plan


__all__ = [
    "PlaytestPatchError",
    "PatchPreconditionError",
    "PatchIdentityError",
    "snapshot_hash",
    "diff_schema_snapshots",
    "diff_atom_ir",
    "apply_schema_patches",
    "apply_schema_patch",
    "apply_atom_patches",
    "apply_atom_patch",
    "apply_playtest_improvement_plan",
    "apply_patch_plan",
]
