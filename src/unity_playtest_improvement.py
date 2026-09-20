"""Deterministic PLAYABILITY_REPORT -> improvement-plan conversion.

Slice 4 is intentionally read-only. It turns the existing mechanics
playtest report and immutable snapshots into the structured
``PLAYTEST_IMPROVEMENT_PLAN`` requested by the improvement-loop design. It
does not call an LLM, mutate a schema, register atoms, patch resolver code, or
emit Unity output.

When a finding does not contain enough existing evidence for a safe
deterministic correction, the plan emits an explicit ``abstain`` operation.
That is deliberate: a later patch engine may apply only operations with
verified preconditions; it must never turn a missing gameplay fact into a
guess.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any


_DEFAULT_THRESHOLDS = {
    "fun_min": 7,
    "friction_max": 4,
    "pacing_min": 6,
    "readability_min": 7,
    "systemic_coherence_min": 7,
}

_SCORE_RULES = (
    ("FUN_SCORE", "fun_min", ">="),
    ("FRICTION_SCORE", "friction_max", "<="),
    ("PACING_SCORE", "pacing_min", ">="),
    ("READABILITY_SCORE", "readability_min", ">="),
    ("SYSTEMIC_COHERENCE_SCORE", "systemic_coherence_min", ">="),
)

_REPORT_SECTIONS = (
    "FUN_SCORE", "FRICTION_SCORE", "PACING_SCORE", "DIFFICULTY_SCORE",
    "READABILITY_SCORE", "SYSTEMIC_COHERENCE_SCORE", "DEAD_LOOP_DETECTOR",
    "REWARD_LOOP_VALIDATION", "MISSING_FEEDBACK_DETECTOR",
    "MISSING_AFFORDANCE_DETECTOR", "MISSION_FLOW_VALIDATION",
    "PLAYABILITY_FIXES", "UNITY_IR_IMPACT", "FINAL_DECISION",
)

_SEVERITIES = frozenset({"low", "medium", "high", "blocker"})
_FAILURE_STATUSES = frozenset({
    "fail", "failed", "missing", "missing_consumer", "unresolved",
    "broken", "abstain",
})
_RULE_PATH_WORDS = frozenset({
    "rule", "rules", "transition", "transitions", "dependency",
    "dependencies", "requires", "commands",
})


def _clone_json(value: Any, label: str) -> Any:
    """Return a detached JSON-compatible copy or fail loudly."""
    try:
        cloned = copy.deepcopy(value)
        json.dumps(cloned)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-compatible") from exc
    return cloned


def _as_report(report: Any) -> dict[str, Any]:
    if hasattr(report, "to_dict"):
        report = report.to_dict()
    if not isinstance(report, Mapping):
        raise ValueError("PLAYABILITY_REPORT must be an object")
    return _clone_json(dict(report), "PLAYABILITY_REPORT")


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a list")
    return list(value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _severity(value: Any) -> str:
    value = _text(value).lower()
    return value if value in _SEVERITIES else "medium"


def _trigger_path(section: str, index: int | None = None, field: str | None = None) -> str:
    path = section if index is None else f"{section}[{index}]"
    return f"{path}.{field}" if field else path


def _failure_key(failure: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _text(failure.get("check")),
        _text(failure.get("summary") or failure.get("finding")),
        _text(failure.get("trigger")),
    )


def _failure(
    *,
    check: str,
    severity: str,
    summary: str,
    trigger: str,
    source: str = "",
    affected_atoms: Sequence[Any] = (),
    correction: str = "",
) -> dict[str, Any]:
    return {
        "check": check or "unknown",
        "severity": _severity(severity),
        "summary": summary or "unspecified playability failure",
        "trigger": trigger,
        "source": source,
        "affected_atoms": sorted({_text(atom) for atom in affected_atoms if _text(atom)}),
        "recommended_correction": correction,
    }


def _collect_failures(
    report: Mapping[str, Any],
    *,
    thresholds: Mapping[str, int],
    require_graded_scores: bool,
) -> list[dict[str, Any]]:
    """Collect report failures in stable order, de-duplicating exact findings."""
    failures: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(item: Mapping[str, Any]) -> None:
        normalized = dict(item)
        key = _failure_key(normalized)
        if key in seen:
            return
        seen.add(key)
        failures.append(normalized)

    score_sections = {section for section, _, _ in _SCORE_RULES}
    for section in _REPORT_SECTIONS:
        if section in score_sections or section in report:
            continue
        add(_failure(
            check="report_validation",
            severity="blocker",
            summary=f"required report section {section} is missing",
            trigger=_trigger_path(section),
            source="PLAYABILITY_REPORT",
            correction="Emit the complete PLAYABILITY_REPORT contract before generating updates.",
        ))

    raw_fixes = _as_list(report.get("PLAYABILITY_FIXES"), "PLAYABILITY_REPORT.PLAYABILITY_FIXES")
    for index, item in enumerate(raw_fixes):
        if not isinstance(item, Mapping):
            add(_failure(
                check="report_validation",
                severity="blocker",
                summary=f"PLAYABILITY_FIXES[{index}] is not an object",
                trigger=_trigger_path("PLAYABILITY_FIXES", index),
                source="PLAYABILITY_REPORT",
                correction="Repair the evaluator report contract before generating updates.",
            ))
            continue
        add(_failure(
            check=_text(item.get("check")) or "playability",
            severity=_text(item.get("severity")),
            summary=_text(item.get("summary")),
            trigger=_trigger_path("PLAYABILITY_FIXES", index),
            source=_text(item.get("source")),
            affected_atoms=_as_list(item.get("affected_atoms"),
                                    f"PLAYABILITY_FIXES[{index}].affected_atoms"),
            correction=_text(item.get("recommended_correction")),
        ))

    for section, key, operator in _SCORE_RULES:
        raw_score = report.get(section)
        if not isinstance(raw_score, Mapping):
            add(_failure(
                check="threshold_gate",
                severity="blocker",
                summary=f"required score section {section} is missing or malformed",
                trigger=_trigger_path(section),
                source=f"PLAYABILITY_REPORT.{section}",
                correction="Emit a graded score section before entering the improvement loop.",
            ))
            continue
        graded = raw_score.get("graded", True)
        score = raw_score.get("score")
        if require_graded_scores and graded is False:
            add(_failure(
                check="threshold_gate",
                severity="high",
                summary=f"required score {section} is ungraded",
                trigger=_trigger_path(section, field="graded"),
                source=f"PLAYABILITY_REPORT.{section}",
                correction="Supply deterministic evidence so the required score can be graded.",
            ))
            continue
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            add(_failure(
                check="threshold_gate",
                severity="blocker",
                summary=f"required score {section} has no numeric score",
                trigger=_trigger_path(section, field="score"),
                source=f"PLAYABILITY_REPORT.{section}",
                correction="Emit a numeric score in the inclusive 0–10 range.",
            ))
            continue
        limit = thresholds[key]
        passed = score >= limit if operator == ">=" else score <= limit
        if not passed:
            add(_failure(
                check="threshold_gate",
                severity="high",
                summary=f"{section}={score} fails required threshold {operator} {limit}",
                trigger=_trigger_path(section, field="score"),
                source=f"PLAYABILITY_REPORT.{section}.score",
                correction="Apply a deterministic correction and re-run the playability evaluator.",
            ))

    for section in (
        "DEAD_LOOP_DETECTOR",
        "REWARD_LOOP_VALIDATION",
        "MISSING_FEEDBACK_DETECTOR",
        "MISSING_AFFORDANCE_DETECTOR",
        "MISSION_FLOW_VALIDATION",
    ):
        records = _as_list(report.get(section), f"PLAYABILITY_REPORT.{section}")
        for index, item in enumerate(records):
            if not isinstance(item, Mapping):
                continue
            severity = _text(item.get("severity")).lower()
            status = _text(item.get("status")).lower()
            finding = _text(item.get("finding"))
            if severity not in {"medium", "high", "blocker"} and status not in _FAILURE_STATUSES:
                continue
            add(_failure(
                check=section.lower(),
                severity=severity or ("medium" if status in _FAILURE_STATUSES else "low"),
                summary=finding or status or f"{section} reported a failure",
                trigger=_trigger_path(section, index),
                source=f"PLAYABILITY_REPORT.{section}[{index}]",
                affected_atoms=_as_list(item.get("affected_atoms"),
                                        f"{section}[{index}].affected_atoms"),
                correction=_text(item.get("recommended_correction")),
            ))

    final_decision = report.get("FINAL_DECISION")
    if isinstance(final_decision, Mapping) and _text(final_decision.get("decision")).upper() == "FAIL":
        if not failures:
            add(_failure(
                check="final_decision",
                severity="high",
                summary=_text(final_decision.get("justification")) or "playability report failed",
                trigger=_trigger_path("FINAL_DECISION", field="decision"),
                source="PLAYABILITY_REPORT.FINAL_DECISION",
                correction="Resolve the report failure before continuing to Unity IR.",
            ))
    return failures


def _atom_records(value: Any, label: str) -> list[dict[str, Any]]:
    records = _as_list(value, label)
    result: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if isinstance(item, str):
            result.append({"atom": item})
        elif isinstance(item, Mapping):
            result.append(dict(item))
        else:
            raise ValueError(f"{label}[{index}] must be a string or object")
    return result


def _atom_identity(item: Mapping[str, Any], index: int) -> str:
    explicit = _text(item.get("id"))
    if explicit:
        return explicit
    atom = _text(item.get("atom") or item.get("name"))
    label = _text(item.get("label"))
    owner = _text(item.get("owner"))
    return json.dumps([owner, atom, label], sort_keys=True, separators=(",", ":"))


def _indexed_atom_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Give duplicate atom identities a stable occurrence suffix."""
    counts: dict[str, int] = {}
    indexed: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(records):
        base = _atom_identity(item, index)
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        key = base if occurrence == 0 else f"{base}#{occurrence}"
        indexed[key] = item
    return indexed


def _diff_atoms(previous: Any, updated: Any) -> dict[str, Any]:
    if previous is None:
        return {
            "status": "baseline_missing",
            "reason": "PREVIOUS_ATOM_IR was not supplied",
            "added_atoms": [], "removed_atoms": [], "modified_atoms": [],
            "added_transitions": [], "removed_transitions": [], "modified_transitions": [],
        }
    old = _atom_records(previous, "PREVIOUS_ATOM_IR")
    new = _atom_records(updated, "UPDATED_ATOM_IR")
    old_by_id = _indexed_atom_records(old)
    new_by_id = _indexed_atom_records(new)
    added = [
        {"id": key, "new": new_by_id[key]}
        for key in sorted(set(new_by_id) - set(old_by_id))
    ]
    removed = [
        {"id": key, "old": old_by_id[key]}
        for key in sorted(set(old_by_id) - set(new_by_id))
    ]
    modified = [
        {"id": key, "old": old_by_id[key], "new": new_by_id[key]}
        for key in sorted(set(old_by_id) & set(new_by_id))
        if old_by_id[key] != new_by_id[key]
    ]
    status = "modified" if added or removed or modified else "unchanged"
    return {
        "status": status,
        "added_atoms": added,
        "removed_atoms": removed,
        "modified_atoms": modified,
        "added_transitions": [],
        "removed_transitions": [],
        "modified_transitions": [],
    }


def _leaf_paths(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        result: list[tuple[str, Any]] = []
        for key in sorted(value, key=str):
            child = f"{path}.{key}" if path else str(key)
            result.extend(_leaf_paths(value[key], child))
        return result
    if isinstance(value, list):
        return [(path, value)]
    return [(path, value)]


def _is_rule_path(path: str) -> bool:
    parts = set(path.replace("[", ".").replace("]", "").split("."))
    return bool(_RULE_PATH_WORDS & {part.lower() for part in parts})


def _diff_snapshots(previous: Any, updated: Any) -> dict[str, Any]:
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

    def entry(path: str, *, old_value: Any = None, new_value: Any = None) -> dict[str, Any]:
        result = {"path": path}
        if old_value is not None:
            result["old"] = old_value
        if new_value is not None:
            result["new"] = new_value
        return result

    added_fields: list[dict[str, Any]] = []
    removed_fields: list[dict[str, Any]] = []
    modified_fields: list[dict[str, Any]] = []
    added_rules: list[dict[str, Any]] = []
    removed_rules: list[dict[str, Any]] = []
    modified_rules: list[dict[str, Any]] = []
    for path in sorted(set(new_paths) - set(old_paths)):
        (added_rules if _is_rule_path(path) else added_fields).append(
            entry(path, new_value=new_paths[path]))
    for path in sorted(set(old_paths) - set(new_paths)):
        (removed_rules if _is_rule_path(path) else removed_fields).append(
            entry(path, old_value=old_paths[path]))
    for path in sorted(set(old_paths) & set(new_paths)):
        if old_paths[path] == new_paths[path]:
            continue
        (modified_rules if _is_rule_path(path) else modified_fields).append(
            entry(path, old_value=old_paths[path], new_value=new_paths[path]))
    status = "modified" if any((added_fields, removed_fields, modified_fields,
                                 added_rules, removed_rules, modified_rules)) else "unchanged"
    return {
        "status": status,
        "added_fields": added_fields,
        "removed_fields": removed_fields,
        "modified_fields": modified_fields,
        "added_rules": added_rules,
        "removed_rules": removed_rules,
        "modified_rules": modified_rules,
    }


def _responsibility(failure: Mapping[str, Any], atom_ir: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    affected = {_text(atom) for atom in failure.get("affected_atoms", []) if _text(atom)}
    exact_atoms: list[str] = []
    summary = _text(failure.get("summary"))
    for atom in atom_ir:
        atom_name = _text(atom.get("atom") or atom.get("name") or atom.get("id"))
        if atom_name and (atom_name in affected or atom_name in summary):
            exact_atoms.append(atom_name)
    source = _text(failure.get("source"))
    schema_refs = [source] if source.startswith("RESOLVER_OUTPUT.objectives") or source.startswith("RESOLVER_OUTPUT.rewards") else []
    resolver_refs = [source] if source.startswith("RESOLVER_OUTPUT") else []
    proven = bool(exact_atoms or schema_refs or resolver_refs)
    return {
        "proven": proven,
        "atoms": sorted(set(exact_atoms)),
        "schema_refs": sorted(set(schema_refs)),
        "resolver_refs": sorted(set(resolver_refs)),
        "unresolved": [] if proven else ["no exact atom/schema/resolver owner was present in the report"],
    }


def _update_record(
    failure: Mapping[str, Any],
    kind: str,
    responsibility: Mapping[str, Any],
    index: int,
) -> dict[str, Any]:
    source = _text(failure.get("source"))
    target = source or ".".join(responsibility.get("schema_refs", [])) or "unresolved"
    return {
        "id": f"fix-{index:04d}-{_text(failure.get('check')) or 'finding'}",
        "kind": kind,
        "operation": "abstain",
        "target": target,
        "expected": None,
        "replacement": None,
        "severity": _severity(failure.get("severity")),
        "trigger": _text(failure.get("trigger")),
        "reason": _text(failure.get("recommended_correction"))
        or "No deterministic replacement is proven by the current snapshots.",
        "responsibility": dict(responsibility),
    }


def _thresholds(raw: Mapping[str, Any] | None) -> dict[str, int]:
    result = dict(_DEFAULT_THRESHOLDS)
    for key, value in (raw or {}).items():
        if key not in result:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"threshold {key} must be numeric")
        result[key] = value
    return result


def _threshold_gate(
    report: Mapping[str, Any],
    failures: Sequence[Mapping[str, Any]],
    limits: Mapping[str, int],
    require_graded_scores: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for section, key, operator in _SCORE_RULES:
        value = report.get(section)
        score = value.get("score") if isinstance(value, Mapping) else None
        graded = value.get("graded", True) if isinstance(value, Mapping) else False
        numeric = isinstance(score, (int, float)) and not isinstance(score, bool)
        passed = numeric and (graded or not require_graded_scores) and (
            score >= limits[key] if operator == ">=" else score <= limits[key])
        checks.append({
            "section": section,
            "score": score,
            "graded": graded,
            "operator": operator,
            "threshold": limits[key],
            "passed": passed,
        })
    scores_pass = all(check["passed"] for check in checks)
    blocker_count = sum(_severity(item.get("severity")) == "blocker" for item in failures)
    final_decision = report.get("FINAL_DECISION")
    report_failed = (
        isinstance(final_decision, Mapping)
        and _text(final_decision.get("decision")).upper() == "FAIL"
    )
    next_iteration = not scores_pass or blocker_count > 0 or report_failed
    reasons = [
        f"{check['section']} {check['score']} {check['operator']} {check['threshold']} failed"
        for check in checks if not check["passed"]
    ]
    if blocker_count:
        reasons.append(f"{blocker_count} blocker finding(s) remain")
    if report_failed and not blocker_count:
        reasons.append("PLAYABILITY_REPORT.FINAL_DECISION is FAIL")
    return {
        "thresholds": dict(limits),
        "checks": checks,
        "require_graded_scores": require_graded_scores,
        "NEXT_ITERATION_REQUIRED": next_iteration,
        "FINAL_READY_FOR_RESOLVER": not next_iteration,
        "NEXT_ITERATION": reasons,
    }


def build_playtest_improvement_plan(
    *,
    schemas: Any,
    atom_ir: Any,
    world_atoms: Any = (),
    character_atoms: Any = (),
    resolver_output: Mapping[str, Any] | None,
    playability_report: Any,
    previous_schemas: Any | None = None,
    previous_atom_ir: Any | None = None,
    thresholds: Mapping[str, Any] | None = None,
    require_graded_scores: bool = True,
) -> dict[str, Any]:
    """Build a deterministic, read-only ``PLAYTEST_IMPROVEMENT_PLAN``.

    The returned candidate snapshots are detached copies of the current
    snapshots. Slice 4 never applies an operation; all suggested operations
    are explicit abstentions until a later reviewed patch registry proves a
    safe deterministic replacement.
    """
    current_schemas = _clone_json(schemas, "SCHEMAS")
    current_atoms = _clone_json(atom_ir, "ATOM_IR")
    _clone_json(world_atoms, "WORLD_ATOMS")
    _clone_json(character_atoms, "CHARACTER_ATOMS")
    current_resolver = _clone_json(resolver_output or {}, "RESOLVER_OUTPUT")
    report = _as_report(playability_report)
    limits = _thresholds(thresholds)
    atom_records = _atom_records(current_atoms, "ATOM_IR")
    failures = _collect_failures(
        report,
        thresholds=limits,
        require_graded_scores=require_graded_scores,
    )

    root_causes: list[dict[str, Any]] = []
    schema_updates: list[dict[str, Any]] = []
    atom_updates: list[dict[str, Any]] = []
    resolver_updates: list[dict[str, Any]] = []
    for index, failure in enumerate(failures):
        responsibility = _responsibility(failure, atom_records)
        root_causes.append({
            "trigger": failure["trigger"],
            "failure": failure["summary"],
            "responsibility": responsibility,
            "missing_or_contradictory_rule": failure["recommended_correction"]
            or "No deterministic rule was supplied by the current inputs.",
            "required_correction": "Resolve the explicit finding without inventing missing facts.",
        })
        check = _text(failure.get("check")).lower()
        if check in {"atom_validation"}:
            atom_updates.append(_update_record(failure, "atom", responsibility, index))
        elif check in {"mission_flow", "systemic_coherence", "threshold_gate", "dead_loop_detector"}:
            resolver_updates.append(_update_record(failure, "resolver", responsibility, index))
        else:
            schema_updates.append(_update_record(failure, "schema", responsibility, index))

    threshold_gate = _threshold_gate(report, failures, limits, require_graded_scores)
    status_schema = "abstain" if schema_updates else "no_changes"
    status_atoms = "abstain" if atom_updates else "no_changes"
    status_resolver = "abstain" if resolver_updates else "no_changes"
    schema_diff = _diff_snapshots(previous_schemas, current_schemas)
    atom_diff = _diff_atoms(previous_atom_ir, current_atoms)

    return {
        "FAIL_SUMMARY": failures,
        "ROOT_CAUSE_ANALYSIS": root_causes,
        "REQUIRED_SCHEMA_UPDATES": {
            "status": status_schema,
            "SCHEMA_PATCH": schema_updates,
            "UPDATED_SCHEMAS": current_schemas,
            "abstentions": [item for item in schema_updates if item["operation"] == "abstain"],
        },
        "REQUIRED_ATOM_UPDATES": {
            "status": status_atoms,
            "ATOM_PATCH": atom_updates,
            "UPDATED_ATOM_IR": current_atoms,
            "abstentions": [item for item in atom_updates if item["operation"] == "abstain"],
        },
        "ATOM_MUTATION_RULES": {
            "rules": [
                {"id": "preserve_registered_atoms", "action": "never remove an atom unless it contradicts a validated schema rule"},
                {"id": "add_schema_required_atoms", "action": "add only atoms explicitly required by an accepted schema update"},
                {"id": "reject_unknown_atoms", "action": "abstain on unknown atom names instead of registering them"},
                {"id": "preconditioned_mutation", "action": "require the expected snapshot value before applying any mutation"},
            ],
            "MUTATED_ATOM_IR": current_atoms,
        },
        "SCHEMA_DIFF": schema_diff,
        "ATOM_DIFF": atom_diff,
        "RESOLVER_PATCH_GENERATOR": {
            "status": status_resolver,
            "RESOLVER_PATCH": resolver_updates,
            "resolver_output_snapshot": current_resolver,
            "abstentions": [item for item in resolver_updates if item["operation"] == "abstain"],
        },
        "THRESHOLD_GATING_LOGIC": threshold_gate,
        "CI_LOOP_INSTRUCTIONS": (
            [
                "Replace SCHEMAS with UPDATED_SCHEMAS only after patch validation.",
                "Replace ATOM_IR with UPDATED_ATOM_IR only after patch validation.",
                "Apply RESOLVER_PATCH through the reviewed deterministic registry.",
                "Re-run the resolver and fixer.",
                "Re-run the PLAYABILITY evaluator.",
                "Re-run the PLAYTEST IMPROVEMENT SYSTEM until thresholds pass or the iteration cap is reached.",
            ]
            if threshold_gate["NEXT_ITERATION_REQUIRED"]
            else [
                "Freeze SCHEMAS.",
                "Freeze ATOM_IR.",
                "Freeze resolver rules.",
                "Proceed to Unity IR generation.",
            ]
        ),
    }


__all__ = ["build_playtest_improvement_plan"]
