"""Deterministic resolver-patch ledger for the mechanics-playtest loop.

Slice 6 is a reviewed ledger, not a source generator.  A resolver patch can
refer only to a rule already present in the registry (or to a separately
reviewed registry entry), names the input shape and output transition or
dependency it changes, and carries an executable validation-test reference.
The module never emits Python/C# and never calls an LLM.

The output projection is deliberately small: applying a valid ledger to a
``RESOLVER_OUTPUT`` JSON snapshot produces a deterministic transition/
dependency diff.  The real resolver remains the source of truth; Slice 8 will
place the rerun around this ledger before Unity IR generation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .atoms import get_atom
from .atoms.backends.unity import _SNIPPETS as _UNITY_SNIPPETS


class ResolverPatchError(ValueError):
    """Base error for malformed or unsafe resolver ledger entries."""


class ResolverPatchBlocker(ResolverPatchError):
    """Raised when a resolver ledger cannot be safely applied."""


class PatchPreconditionError(ResolverPatchError):
    """Raised when a resolver output no longer matches a patch baseline."""


_OPERATIONS = frozenset({"add", "modify", "remove", "abstain"})
_OUTPUT_KINDS = frozenset({"transition", "dependency"})
_EXECUTABLE_KEYS = frozenset({"python", "csharp", "source", "source_code", "code"})


def _clone_json(value: Any, label: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-compatible") from exc
    return copy.deepcopy(value)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def resolver_patch_hash(patch: Mapping[str, Any]) -> str:
    """Hash a patch while ignoring a caller-supplied hash field."""
    payload = {
        str(key): value
        for key, value in patch.items()
        if key not in {"content_hash", "resolver_module", "resolver_callable"}
    }
    return _hash(payload)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ResolverPatchError(f"{label} must be a list")
    return list(value)


def _normalize_precedence(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolverPatchError(f"{label} must identify phase and order")
    phase = _text(value.get("phase"))
    order = value.get("order")
    if not phase:
        raise ResolverPatchError(f"{label}.phase is required")
    if not isinstance(order, int) or isinstance(order, bool):
        raise ResolverPatchError(f"{label}.order must be an integer")
    before = sorted({_text(item) for item in _as_list(value.get("before"), label + ".before") if _text(item)})
    after = sorted({_text(item) for item in _as_list(value.get("after"), label + ".after") if _text(item)})
    return {"phase": phase, "order": order, "before": before, "after": after}


def _normalize_validation_test(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"test_id": value, "fixture": value}
    if not isinstance(value, Mapping):
        raise ResolverPatchError(f"{label} must identify a validation test and fixture")
    test_id = _text(value.get("test_id") or value.get("id"))
    fixture = _text(value.get("fixture") or value.get("fixture_id"))
    if not test_id or not fixture:
        raise ResolverPatchError(f"{label} requires test_id and fixture")
    return {
        "test_id": test_id,
        "fixture": fixture,
        **({"command": _text(value["command"])} if value.get("command") else {}),
    }


def _normalize_input_shape(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"shape": value}
    if not isinstance(value, Mapping) or not value:
        raise ResolverPatchError(f"{label} must describe the accepted input shape")
    return _clone_json(dict(value), label)


def _rule_id(rule: Mapping[str, Any]) -> str:
    return _text(rule.get("rule_id") or rule.get("id"))


def _normalize_rule(raw: Mapping[str, Any]) -> dict[str, Any]:
    rule = _clone_json(dict(raw), "resolver rule")
    rule_id = _rule_id(rule)
    if not rule_id:
        raise ResolverPatchError("resolver rule requires rule_id")
    if rule.get("reviewed") is not True:
        raise ResolverPatchError(f"resolver rule {rule_id!r} must be explicitly reviewed")
    module = _text(rule.get("module"))
    callable_name = _text(rule.get("callable") or rule.get("callable_name"))
    if not module or not callable_name:
        raise ResolverPatchError(f"resolver rule {rule_id!r} requires module and callable")
    normalized = {
        "rule_id": rule_id,
        "module": module,
        "callable": callable_name,
        "input_shape": _normalize_input_shape(rule.get("input_shape"), f"{rule_id}.input_shape"),
        "precedence": _normalize_precedence(rule.get("precedence"), f"{rule_id}.precedence"),
        "validation_test": _normalize_validation_test(rule.get("validation_test"), f"{rule_id}.validation_test"),
        "reviewed": True,
    }
    for key in ("output_kind", "description"):
        if rule.get(key) is not None:
            normalized[key] = _clone_json(rule[key], f"{rule_id}.{key}")
    return normalized


class ResolverRuleRegistry:
    """Closed, deterministic registry of resolver/backend rule identities."""

    def __init__(self, entries: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = ()):
        self._rules: dict[str, dict[str, Any]] = {}
        source = entries.values() if isinstance(entries, Mapping) else entries
        for entry in source:
            self.register(entry)

    def register(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        normalized = _normalize_rule(entry)
        rule_id = normalized["rule_id"]
        existing = self._rules.get(rule_id)
        if existing is not None and existing != normalized:
            raise ResolverPatchBlocker(
                f"resolver rule {rule_id!r} is already registered with different content"
            )
        self._rules[rule_id] = normalized
        return copy.deepcopy(normalized)

    def get(self, rule_id: str) -> dict[str, Any] | None:
        rule = self._rules.get(_text(rule_id))
        return copy.deepcopy(rule) if rule is not None else None

    def require(self, rule_id: str) -> dict[str, Any]:
        rule = self.get(rule_id)
        if rule is None:
            raise ResolverPatchBlocker(f"unknown resolver rule ID {_text(rule_id)!r}")
        return rule

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {key: copy.deepcopy(self._rules[key]) for key in sorted(self._rules)}


_BUILTIN_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "unity.resolve.schema",
        "module": "src/unity_resolver.py",
        "callable": "UnityResolver.resolve_schema",
        "input_shape": {"schema": "UnitySchema", "packages": "list[UnityPackage]"},
        "precedence": {"phase": "schema_resolution", "order": 10},
        "validation_test": {"test_id": "tests/test_unity_resolver.py", "fixture": "minimal_schema"},
        "output_kind": "schema",
        "description": "Resolve package/version fields and expand registered behaviors.",
        "reviewed": True,
    },
    {
        "rule_id": "unity.resolve.objective_targets",
        "module": "src/unity_objective_targets.py",
        "callable": "resolve_objective_targets",
        "input_shape": {"schema": "UnitySchema", "objectives": "list[MissionObjective]"},
        "precedence": {"phase": "schema_resolution", "order": 20},
        "validation_test": {"test_id": "tests/test_objective_targets.py", "fixture": "objective_entity_match"},
        "output_kind": "objective_target",
        "description": "Fill only objective target refs proven by existing entity names.",
        "reviewed": True,
    },
    {
        "rule_id": "unity.resolve.required_packages",
        "module": "src/unity_resolver.py",
        "callable": "UnityResolver.inject_required_packages",
        "input_shape": {"schema": "UnitySchema", "features": "list[str]"},
        "precedence": {"phase": "schema_resolution", "order": 30},
        "validation_test": {"test_id": "tests/test_unity_resolver.py", "fixture": "required_package_injection"},
        "output_kind": "dependency",
        "description": "Inject deterministic packages required by the resolved project.",
        "reviewed": True,
    },
    {
        "rule_id": "unity.resolve.manifest_dependencies",
        "module": "src/unity_resolver.py",
        "callable": "UnityResolver.build_manifest_deps",
        "input_shape": {"schema": "UnitySchema", "packages": "list[UnityPackage]"},
        "precedence": {"phase": "manifest_resolution", "order": 40},
        "validation_test": {"test_id": "tests/test_unity_resolver.py", "fixture": "manifest_dependencies"},
        "output_kind": "dependency",
        "description": "Build the final deterministic package dependency map.",
        "reviewed": True,
    },
    {
        "rule_id": "unity.compose.objective_wiring",
        "module": "src/unity_compose.py",
        "callable": "MicroComposer.plan_wiring",
        "input_shape": {"schema": "UnitySchema", "composed_units": "list[ComposedUnit]"},
        "precedence": {"phase": "gameplay_wiring", "order": 50},
        "validation_test": {"test_id": "tests/test_unity_mechanics_adapter.py", "fixture": "composer_wiring"},
        "output_kind": "transition",
        "description": "Bind declared objective events through the existing composition seams.",
        "reviewed": True,
    },
    {
        "rule_id": "unity.backend.atom_snippet",
        "module": "src/atoms/backends/unity.py",
        "callable": "unity_atom_snippet",
        "input_shape": {"atom": "AtomInvocation", "engine": "unity"},
        "precedence": {"phase": "backend_translation", "order": 60},
        "validation_test": {"test_id": "tests/test_atoms_unity.py", "fixture": "registered_unity_atom"},
        "output_kind": "atom_backend",
        "description": "Translate every Unity-capable registered atom through the dispatch table.",
        "reviewed": True,
    },
)

DEFAULT_RESOLVER_RULES = {entry["rule_id"]: copy.deepcopy(entry) for entry in _BUILTIN_RULES}
DEFAULT_RESOLVER_RULE_REGISTRY = ResolverRuleRegistry(_BUILTIN_RULES)


def default_resolver_rule_registry() -> ResolverRuleRegistry:
    """Return a detached registry so a reviewed test rule cannot leak globally."""
    return ResolverRuleRegistry(tuple(DEFAULT_RESOLVER_RULES.values()))


def _target_info(patch: Mapping[str, Any]) -> tuple[str, str, dict[str, Any] | None]:
    target = patch.get("target")
    target_kind = _text(patch.get("target_kind"))
    target_id = ""
    if isinstance(target, Mapping):
        target_kind = target_kind or _text(target.get("kind") or target.get("type"))
        target_id = _text(target.get("id") or target.get("target_id"))
    elif target is not None:
        target_id = _text(target)
        if ":" in target_id:
            prefix, suffix = target_id.split(":", 1)
            if prefix in _OUTPUT_KINDS:
                target_kind, target_id = prefix, suffix
    for kind in _OUTPUT_KINDS:
        if kind in patch and isinstance(patch[kind], Mapping):
            target_kind = target_kind or kind
            target_id = target_id or _text(patch[kind].get("id"))
    replacement = patch.get("replacement")
    if not isinstance(replacement, Mapping):
        for kind in _OUTPUT_KINDS:
            if isinstance(patch.get(kind), Mapping):
                replacement = patch[kind]
                break
    if not isinstance(replacement, Mapping) and isinstance(patch.get("change"), Mapping):
        replacement = patch["change"]
    if isinstance(replacement, Mapping):
        target_id = target_id or _text(replacement.get("id"))
    return target_kind, target_id, copy.deepcopy(dict(replacement)) if isinstance(replacement, Mapping) else None


def _patch_identity(patch: Mapping[str, Any]) -> str:
    explicit = _text(patch.get("id"))
    if explicit:
        return explicit
    kind, target_id, _ = _target_info(patch)
    return "|".join((_text(patch.get("rule_id")), kind, target_id))


def _normalize_patch(raw: Mapping[str, Any], index: int, registry: ResolverRuleRegistry) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    label = f"RESOLVER_PATCH[{index}]"
    patch = _clone_json(dict(raw), label)
    raw_content_hash = resolver_patch_hash(patch)
    action = _text(patch.get("operation")).lower()
    if action not in _OPERATIONS:
        return None, {"path": label, "kind": "invalid_operation", "message": f"unsupported operation {action!r}"}
    patch["operation"] = action
    patch.setdefault("id", f"resolver-patch-{index:04d}")
    patch["id"] = _text(patch["id"])
    if action == "abstain":
        patch["reason"] = _text(patch.get("reason")) or "explicit resolver patch abstention"
        return patch, None
    if _EXECUTABLE_KEYS & {str(key).lower() for key in patch}:
        return None, {"path": label, "kind": "executable_source", "message": "resolver ledger cannot carry executable source"}

    rule_id = _text(patch.get("rule_id") or patch.get("resolver_rule_id"))
    rule = registry.get(rule_id)
    if rule is None:
        return None, {"path": label, "kind": "unknown_rule", "rule_id": rule_id, "message": f"unknown resolver rule ID {rule_id!r}"}
    patch["rule_id"] = rule_id
    try:
        patch["input_shape"] = _normalize_input_shape(patch.get("input_shape"), label + ".input_shape")
        patch["precedence"] = _normalize_precedence(patch.get("precedence"), label + ".precedence")
        patch["validation_test"] = _normalize_validation_test(patch.get("validation_test"), label + ".validation_test")
        trigger = _text(patch.get("trigger") or patch.get("report_failure") or patch.get("source_report_path"))
        if not trigger:
            raise ResolverPatchError("report failure trigger is required")
        patch["trigger"] = trigger
        kind, target_id, replacement = _target_info(patch)
        if kind not in _OUTPUT_KINDS:
            raise ResolverPatchError("target kind must be transition or dependency")
        if not target_id:
            raise ResolverPatchError("target id is required")
        patch["target"] = {"kind": kind, "id": target_id}
        if action in {"add", "modify"}:
            if replacement is None:
                raise ResolverPatchError(f"{action} requires a transition/dependency replacement")
            if not _text(replacement.get("id")):
                raise ResolverPatchError("replacement requires an id")
            if _text(replacement.get("id")) != target_id:
                raise ResolverPatchError("replacement id must match target id")
            patch["replacement"] = replacement
        if action in {"modify", "remove"} and "expected" not in patch:
            raise ResolverPatchError(f"{action} requires an exact expected record")
        if "expected" in patch:
            patch["expected"] = _clone_json(patch["expected"], label + ".expected")
        supplied_hash = _text(raw.get("content_hash"))
        normalized_content_hash = resolver_patch_hash(patch)
        if supplied_hash and supplied_hash not in {raw_content_hash, normalized_content_hash}:
            raise ResolverPatchError("content_hash does not match resolver patch content")
        patch["content_hash"] = normalized_content_hash
        # The registry metadata is copied into the normalized entry so the
        # ledger is self-describing and review does not depend on mutable code.
        patch["resolver_module"] = rule["module"]
        patch["resolver_callable"] = rule["callable"]
    except ResolverPatchError as exc:
        return None, {"path": label, "kind": "invalid_patch", "rule_id": rule_id, "message": str(exc)}
    return patch, None


def _expected_matches(operation: Mapping[str, Any], current: Any) -> bool:
    if "expected_hash" in operation:
        return _hash(current) == _text(operation["expected_hash"])
    return "expected" in operation and current == operation["expected"]


def _check_precedence(patches: Sequence[Mapping[str, Any]], registry: ResolverRuleRegistry) -> list[dict[str, Any]]:
    contradictions: list[dict[str, Any]] = []
    known_nodes = set(registry.ids()) | {_patch_identity(patch) for patch in patches}
    graph: dict[str, set[str]] = {node: set() for node in known_nodes}
    target_precedence: dict[tuple[str, str], tuple[str, int, str]] = {}
    for patch in patches:
        identity = _patch_identity(patch)
        precedence = patch["precedence"]
        target = patch["target"]
        target_key = (target["kind"], target["id"])
        current_precedence = (precedence["phase"], precedence["order"], identity)
        previous = target_precedence.get(target_key)
        if previous is not None and previous[:2] != current_precedence[:2]:
            contradictions.append({
                "kind": "conflicting_transition_precedence",
                "target": {"kind": target["kind"], "id": target["id"]},
                "patches": [previous[2], identity],
                "message": "same transition/dependency has incompatible precedence",
            })
        target_precedence[target_key] = current_precedence
        before = set(precedence.get("before", []))
        after = set(precedence.get("after", []))
        if identity in before or identity in after or before & after:
            contradictions.append({
                "kind": "self_contradictory_precedence",
                "patch": identity,
                "message": "before/after precedence constraints contradict themselves",
            })
        for reference in before | after:
            if reference not in known_nodes:
                contradictions.append({
                    "kind": "unknown_precedence_reference",
                    "patch": identity,
                    "reference": reference,
                    "message": "precedence references an unregistered rule or patch",
                })
        for reference in before:
            graph.setdefault(identity, set()).add(reference)
        for reference in after:
            graph.setdefault(reference, set()).add(identity)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        if node in visiting:
            cycle = path[path.index(node):] + (node,) if node in path else path + (node,)
            contradictions.append({
                "kind": "precedence_cycle",
                "nodes": list(cycle),
                "message": "resolver precedence graph contains a cycle",
            })
            return
        if node in visited:
            return
        visiting.add(node)
        for child in sorted(graph.get(node, set())):
            visit(child, path + (node,))
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node, ())
    return contradictions


def validate_atom_resolver_coverage(
    atom_ir: Any,
    *,
    registry: ResolverRuleRegistry = DEFAULT_RESOLVER_RULE_REGISTRY,
) -> dict[str, Any]:
    """Prove every atom has a registered Unity resolver/backend path."""
    blockers: list[dict[str, Any]] = []
    covered: list[dict[str, Any]] = []
    records = _as_list(atom_ir, "ATOM_IR")
    backend_rule = registry.get("unity.backend.atom_snippet")
    if backend_rule is None:
        blockers.append({"kind": "missing_backend_rule", "message": "Unity atom backend rule is not registered"})
    for index, record in enumerate(records):
        if isinstance(record, str):
            name = record.strip()
        elif isinstance(record, Mapping):
            name = _text(record.get("atom") or record.get("name"))
        else:
            blockers.append({"kind": "invalid_atom", "path": f"ATOM_IR[{index}]", "message": "atom must be a string or object"})
            continue
        definition = get_atom(name)
        if definition is None:
            blockers.append({"kind": "unknown_atom", "path": f"ATOM_IR[{index}]", "atom": name, "message": "atom is not registered"})
            continue
        if "unity" not in definition.engines:
            blockers.append({"kind": "backend_unavailable", "path": f"ATOM_IR[{index}]", "atom": definition.name, "message": "registered atom has no Unity engine path"})
            continue
        if definition.name not in _UNITY_SNIPPETS:
            blockers.append({"kind": "backend_unavailable", "path": f"ATOM_IR[{index}]", "atom": definition.name, "message": "Unity backend dispatch has no registered snippet"})
            continue
        covered.append({
            "path": f"ATOM_IR[{index}]",
            "atom": definition.name,
            "resolver_rule_id": backend_rule["rule_id"] if backend_rule else "",
            "module": backend_rule["module"] if backend_rule else "",
            "callable": backend_rule["callable"] if backend_rule else "",
        })
    return {
        "status": "blocked" if blockers else "valid",
        "covered": covered,
        "blockers": blockers,
        "content_hash": _hash(covered),
    }


def validate_resolver_patch_ledger(
    patches: Any,
    *,
    registry: ResolverRuleRegistry = DEFAULT_RESOLVER_RULE_REGISTRY,
    atom_ir: Any | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Validate and deduplicate resolver patches without applying them."""
    raw_patches = _as_list(patches, "RESOLVER_PATCH")
    normalized: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    abstentions: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    identities: dict[str, tuple[str, dict[str, Any]]] = {}
    for index, raw in enumerate(raw_patches):
        if not isinstance(raw, Mapping):
            blockers.append({"path": f"RESOLVER_PATCH[{index}]", "kind": "invalid_patch", "message": "patch must be an object"})
            continue
        patch, blocker = _normalize_patch(raw, index, registry)
        if blocker is not None:
            blockers.append(blocker)
            continue
        if patch is None:
            continue
        if patch["operation"] == "abstain":
            abstentions.append(patch)
            continue
        identity = _patch_identity(patch)
        digest = patch["content_hash"]
        existing = identities.get(identity)
        if existing is not None:
            if existing[0] == digest:
                duplicates.append({"identity": identity, "content_hash": digest, "action": "deduplicated"})
                continue
            blockers.append({"kind": "conflicting_duplicate", "identity": identity, "content_hashes": [existing[0], digest], "message": "same resolver patch identity has different content"})
            continue
        identities[identity] = (digest, patch)
        normalized.append(patch)

    contradictions = _check_precedence(normalized, registry)
    coverage = validate_atom_resolver_coverage(atom_ir, registry=registry) if atom_ir is not None else None
    if coverage and coverage["blockers"]:
        blockers.extend({"kind": "atom_resolver_coverage", **item} for item in coverage["blockers"])
    status = "blocked" if blockers or contradictions or abstentions else ("unchanged" if not normalized else "valid")
    result = {
        "status": status,
        "patches": copy.deepcopy(normalized),
        "blockers": blockers,
        "contradictions": contradictions,
        "duplicates": duplicates,
        "abstentions": copy.deepcopy(abstentions),
        "atom_resolver_coverage": coverage,
        "content_hash": _hash(normalized),
    }
    if strict and status == "blocked":
        reasons = [item.get("message", item.get("kind", "blocker")) for item in blockers + contradictions]
        if abstentions:
            reasons.append("explicit resolver patch abstention")
        raise ResolverPatchBlocker("; ".join(str(reason) for reason in reasons))
    return result


def _records_by_id(output: Mapping[str, Any], kind: str) -> tuple[str, list[dict[str, Any]]]:
    collection_name = "transitions" if kind == "transition" else "dependencies"
    records = output.get(collection_name)
    if not isinstance(records, list):
        raise ResolverPatchBlocker(f"RESOLVER_OUTPUT.{collection_name} must be a list")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or not _text(record.get("id")):
            raise ResolverPatchBlocker(f"RESOLVER_OUTPUT.{collection_name}[{index}] requires an id")
        normalized.append(dict(record))
    return collection_name, normalized


def diff_resolver_output(
    previous: Any,
    updated: Any,
    *,
    provenance: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Diff transitions and dependencies with stable IDs and provenance."""
    old = _clone_json(previous, "PREVIOUS_RESOLVER_OUTPUT")
    new = _clone_json(updated, "RESOLVER_OUTPUT")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise ResolverPatchError("RESOLVER_OUTPUT must be an object")
    result: dict[str, Any] = {"status": "unchanged"}
    changed = False
    for kind, collection in (("transition", "transitions"), ("dependency", "dependencies")):
        prefix = "transitions" if kind == "transition" else "dependencies"
        for operation in ("added", "removed", "modified"):
            result[f"{operation}_{prefix}"] = []
        old_records = {str(item["id"]): item for item in old.get(collection, []) if isinstance(item, Mapping) and _text(item.get("id"))}
        new_records = {str(item["id"]): item for item in new.get(collection, []) if isinstance(item, Mapping) and _text(item.get("id"))}

        def item_entry(record_id: str, old_value: Any = None, new_value: Any = None) -> dict[str, Any]:
            info = (provenance or {}).get((kind, record_id), {})
            return {
                "id": record_id,
                "old": old_value,
                "new": new_value,
                "operation_id": _text(info.get("operation_id")) or "resolver-output-diff",
                "source_report_path": _text(info.get("source_report_path")) or "RESOLVER_OUTPUT",
            }

        for record_id in sorted(set(new_records) - set(old_records)):
            result[f"added_{prefix}"].append(item_entry(record_id, new_value=new_records[record_id]))
        for record_id in sorted(set(old_records) - set(new_records)):
            result[f"removed_{prefix}"].append(item_entry(record_id, old_value=old_records[record_id]))
        for record_id in sorted(set(old_records) & set(new_records)):
            if old_records[record_id] != new_records[record_id]:
                result[f"modified_{prefix}"].append(item_entry(record_id, old_records[record_id], new_records[record_id]))
        changed |= any(result[f"{operation}_{prefix}"] for operation in ("added", "removed", "modified"))
    result["status"] = "modified" if changed else "unchanged"
    return result


def apply_resolver_patch_ledger(
    resolver_output: Any,
    patches: Any,
    *,
    registry: ResolverRuleRegistry = DEFAULT_RESOLVER_RULE_REGISTRY,
    atom_ir: Any | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Apply a validated ledger to a resolver-output snapshot.

    This projects the reviewed transition/dependency change into an immutable
    snapshot.  It does not modify resolver source; Slice 8 will rerun the
    registered callable around this ledger and compare its output.
    """
    baseline = _clone_json(resolver_output, "RESOLVER_OUTPUT")
    if not isinstance(baseline, Mapping):
        raise ResolverPatchError("RESOLVER_OUTPUT must be an object")
    validation = validate_resolver_patch_ledger(
        patches, registry=registry, atom_ir=atom_ir, strict=strict,
    )
    if validation["status"] == "blocked":
        if strict:
            raise ResolverPatchBlocker("resolver patch ledger is blocked")
        return {
            "status": "blocked",
            "RESOLVER_OUTPUT": baseline,
            "RESOLVER_OUTPUT_DIFF": diff_resolver_output(baseline, baseline),
            "VALIDATION": validation,
            "SNAPSHOT_HASH": _hash(baseline),
        }

    updated = copy.deepcopy(dict(baseline))
    provenance: dict[tuple[str, str], dict[str, Any]] = {}
    applied: list[dict[str, Any]] = []
    for patch in validation["patches"]:
        target = patch["target"]
        kind = target["kind"]
        collection_name, records = _records_by_id(updated, kind)
        record_id = target["id"]
        matches = [index for index, record in enumerate(records) if str(record["id"]) == record_id]
        action = patch["operation"]
        if action == "add":
            if matches:
                raise PatchPreconditionError(f"resolver add target {kind}:{record_id} already exists")
            records.append(copy.deepcopy(patch["replacement"]))
        elif action in {"modify", "remove"}:
            if len(matches) != 1:
                raise PatchPreconditionError(f"resolver target {kind}:{record_id} was not found exactly once")
            index = matches[0]
            current = records[index]
            if not _expected_matches(patch, current):
                raise PatchPreconditionError(f"resolver patch {patch['id']} expected-value precondition failed")
            if action == "remove":
                records.pop(index)
            else:
                records[index] = copy.deepcopy(patch["replacement"])
        updated[collection_name] = records
        provenance[(kind, record_id)] = {
            "operation_id": patch["id"],
            "source_report_path": patch["trigger"],
        }
        applied.append({"id": patch["id"], "operation": action, "target": dict(target)})

    return {
        "status": "modified" if applied else "unchanged",
        "RESOLVER_OUTPUT": updated,
        "RESOLVER_OUTPUT_DIFF": diff_resolver_output(baseline, updated, provenance=provenance),
        "VALIDATION": validation,
        "APPLIED_PATCHES": applied,
        "SNAPSHOT_HASH": _hash(updated),
    }


def apply_resolver_patch_plan(
    resolver_output: Any,
    plan: Mapping[str, Any],
    *,
    registry: ResolverRuleRegistry = DEFAULT_RESOLVER_RULE_REGISTRY,
    atom_ir: Any | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Validate/apply the ``RESOLVER_PATCH_GENERATOR`` block from a plan."""
    if not isinstance(plan, Mapping):
        raise ResolverPatchError("PLAYTEST_IMPROVEMENT_PLAN must be an object")
    block = plan.get("RESOLVER_PATCH_GENERATOR")
    if not isinstance(block, Mapping):
        raise ResolverPatchError("plan is missing RESOLVER_PATCH_GENERATOR")
    return apply_resolver_patch_ledger(
        resolver_output,
        block.get("RESOLVER_PATCH", []),
        registry=registry,
        atom_ir=atom_ir,
        strict=strict,
    )


def stable_resolver_replay(
    resolver_output: Any,
    patches: Any,
    *,
    registry: ResolverRuleRegistry = DEFAULT_RESOLVER_RULE_REGISTRY,
    atom_ir: Any | None = None,
) -> dict[str, Any]:
    """Prove repeated deterministic projection yields the same output/diff."""
    first = apply_resolver_patch_ledger(
        resolver_output, patches, registry=registry, atom_ir=atom_ir,
    )
    second = apply_resolver_patch_ledger(
        resolver_output, patches, registry=registry, atom_ir=atom_ir,
    )
    stable = (
        first["SNAPSHOT_HASH"] == second["SNAPSHOT_HASH"]
        and _canonical(first["RESOLVER_OUTPUT_DIFF"]) == _canonical(second["RESOLVER_OUTPUT_DIFF"])
    )
    if not stable:
        raise ResolverPatchBlocker("resolver replay produced a non-deterministic output diff")
    return {"stable": True, "first": first, "second": second}


validate_resolver_patches = validate_resolver_patch_ledger
apply_resolver_patches = apply_resolver_patch_ledger


__all__ = [
    "ResolverPatchError",
    "ResolverPatchBlocker",
    "PatchPreconditionError",
    "ResolverRuleRegistry",
    "DEFAULT_RESOLVER_RULES",
    "DEFAULT_RESOLVER_RULE_REGISTRY",
    "default_resolver_rule_registry",
    "resolver_patch_hash",
    "validate_atom_resolver_coverage",
    "validate_resolver_patch_ledger",
    "validate_resolver_patches",
    "diff_resolver_output",
    "apply_resolver_patch_ledger",
    "apply_resolver_patches",
    "apply_resolver_patch_plan",
    "stable_resolver_replay",
]
