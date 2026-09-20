"""Deterministic threshold gate and bounded mechanics-playtest loop.

Slice 7 owns retry control between deterministic resolver/fixer work and the
future Unity IR handoff.  It consumes snapshots, reports, improvement plans,
and the Slice 5/6 patch engines.  It never launches Unity, generates code, or
constructs an LLM client.

The loop accepts keyword-only hooks so a caller can supply the real evaluator
and resolver/fixer rerun in Slice 8.  Hooks receive detached JSON snapshots and
must return detached JSON-compatible values.  A missing rerun hook uses the
validated resolver-output projection as a deterministic test fallback; the
handoff slice can replace that fallback with the real resolver/fixer callable.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .unity_playtest_improvement import build_playtest_improvement_plan
from .unity_playtest_patches import (
    apply_atom_patches,
    apply_schema_patches,
    diff_atom_ir,
    diff_schema_snapshots,
)
from .unity_playtest_resolver_patches import (
    apply_resolver_patch_plan,
    diff_resolver_output,
)


class PlaytestLoopError(ValueError):
    """Base error for invalid loop configuration or callback output."""


class PlaytestLoopBlocked(PlaytestLoopError):
    """A deterministic safety condition stopped the loop before retry."""


DEFAULT_THRESHOLDS: dict[str, int] = {
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


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _clone_json(value: Any, label: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PlaytestLoopError(f"{label} must be JSON-compatible") from exc
    return copy.deepcopy(value)


def _as_report(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise PlaytestLoopError("PLAYABILITY_REPORT must be an object")
    return _clone_json(dict(value), "PLAYABILITY_REPORT")


def _as_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlaytestLoopError(f"{label} must be an object")
    return _clone_json(dict(value), label)


def _thresholds(raw: Mapping[str, Any] | None) -> dict[str, int]:
    result = dict(DEFAULT_THRESHOLDS)
    for key, value in (raw or {}).items():
        normalized = str(key)
        aliases = {
            "FUN_SCORE": "fun_min",
            "FRICTION_SCORE": "friction_max",
            "PACING_SCORE": "pacing_min",
            "READABILITY_SCORE": "readability_min",
            "SYSTEMIC_COHERENCE_SCORE": "systemic_coherence_min",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in result:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PlaytestLoopError(f"threshold {key} must be numeric")
        result[normalized] = value
    return result


def _blocker_findings(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for path, value in _report_paths(report):
        if isinstance(value, Mapping) and str(value.get("severity", "")).lower() == "blocker":
            blockers.append({
                "path": path,
                "summary": str(value.get("summary") or value.get("finding") or value.get("message") or "blocker finding"),
                "check": str(value.get("check") or ""),
            })
    return blockers


def _report_paths(value: Any, path: str = "") -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        result.append((path, value))
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            result.extend(_report_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_report_paths(child, f"{path}[{index}]"))
    return result


def evaluate_threshold_gate(
    playability_report: Any,
    *,
    thresholds: Mapping[str, Any] | None = None,
    require_graded_scores: bool = True,
) -> dict[str, Any]:
    """Evaluate the fixed mechanics thresholds without applying any patch."""
    report = _as_report(playability_report)
    limits = _thresholds(thresholds)
    checks: list[dict[str, Any]] = []
    for section, key, operator in _SCORE_RULES:
        value = report.get(section)
        score = value.get("score") if isinstance(value, Mapping) else None
        graded = value.get("graded", True) if isinstance(value, Mapping) else False
        numeric = isinstance(score, (int, float)) and not isinstance(score, bool)
        passed = numeric and (graded or not require_graded_scores) and (
            score >= limits[key] if operator == ">=" else score <= limits[key]
        )
        reason = ""
        if not numeric:
            reason = "score is missing or non-numeric"
        elif require_graded_scores and not graded:
            reason = "score is ungraded"
        elif not passed:
            reason = f"{score} {operator} {limits[key]} is false"
        checks.append({
            "section": section,
            "score": score,
            "graded": graded,
            "operator": operator,
            "threshold": limits[key],
            "passed": passed,
            "reason": reason,
        })

    blockers = _blocker_findings(report)
    final_decision = report.get("FINAL_DECISION")
    report_failed = (
        isinstance(final_decision, Mapping)
        and str(final_decision.get("decision", "")).upper() == "FAIL"
    )
    reasons = [
        f"{check['section']}: {check['reason']}"
        for check in checks if not check["passed"]
    ]
    if blockers:
        reasons.extend(f"{item['path']}: {item['summary']}" for item in blockers)
    if report_failed:
        reasons.append("PLAYABILITY_REPORT.FINAL_DECISION is FAIL")
    required_scores_pass = all(check["passed"] for check in checks)
    passed = required_scores_pass and not blockers and not report_failed
    return {
        "thresholds": limits,
        "checks": checks,
        "require_graded_scores": require_graded_scores,
        "required_scores_pass": required_scores_pass,
        "blockers": blockers,
        "blocker_count": len(blockers),
        "report_failed": report_failed,
        "passed": passed,
        "NEXT_ITERATION_REQUIRED": not passed,
        "FINAL_READY_FOR_RESOLVER": passed,
        "reasons": reasons,
        "diagnostic_scores": {
            "DIFFICULTY_SCORE": report.get("DIFFICULTY_SCORE"),
        },
    }


def loop_config_from_mapping(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read Slice 7 settings from a ``mechanics_playtest`` config block."""
    raw = (config or {}).get("mechanics_playtest", {}) if isinstance(config, Mapping) else {}
    if not isinstance(raw, Mapping):
        raise PlaytestLoopError("mechanics_playtest config must be an object")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_iterations": int(raw.get("max_iterations", 3)),
        "auto_apply": bool(raw.get("auto_apply", False)),
        "require_graded_scores": bool(raw.get("require_graded_scores", True)),
        "report_dir": raw.get("report_dir"),
        "thresholds": _thresholds(raw.get("thresholds")),
    }


def _failure_signature(report: Mapping[str, Any], gate: Mapping[str, Any]) -> str:
    findings: list[dict[str, Any]] = []
    for path, value in _report_paths(report):
        if not isinstance(value, Mapping):
            continue
        if value.get("check") or value.get("summary") or value.get("finding"):
            findings.append({
                "path": path,
                "check": value.get("check"),
                "summary": value.get("summary") or value.get("finding"),
                "severity": value.get("severity"),
            })
    payload = {
        "failed_checks": [
            {"section": item["section"], "reason": item["reason"]}
            for item in gate.get("checks", []) if not item.get("passed")
        ],
        "blockers": gate.get("blockers", []),
        "report_failed": gate.get("report_failed", False),
        "findings": findings,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _state_hash(schemas: Any, atom_ir: Any, resolver_output: Any) -> str:
    return hashlib.sha256(_canonical({
        "SCHEMAS": schemas,
        "ATOM_IR": atom_ir,
        "RESOLVER_OUTPUT": resolver_output,
    }).encode("utf-8")).hexdigest()


def _write_json_once(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise PlaytestLoopBlocked(f"immutable artifact already exists: {path}") from exc


def _write_iteration_artifacts(
    report_dir: Path | None,
    iteration: int,
    *,
    schemas: Any,
    atom_ir: Any,
    resolver_output: Any,
    report: Mapping[str, Any],
    plan: Mapping[str, Any],
    gate: Mapping[str, Any],
    diff: Mapping[str, Any],
) -> str | None:
    if report_dir is None:
        return None
    directory = report_dir / f"iteration-{iteration:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    _write_json_once(directory / "schema.json", schemas)
    _write_json_once(directory / "atoms.json", atom_ir)
    _write_json_once(directory / "resolver.json", resolver_output)
    _write_json_once(directory / "report.json", report)
    _write_json_once(directory / "plan.json", plan)
    _write_json_once(directory / "gate.json", gate)
    _write_json_once(directory / "diff.json", diff)
    return str(directory)


def _empty_plan() -> dict[str, Any]:
    return {
        "REQUIRED_SCHEMA_UPDATES": {"SCHEMA_PATCH": []},
        "REQUIRED_ATOM_UPDATES": {"ATOM_PATCH": []},
        "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
    }


def _apply_plan(
    schemas: Any,
    atom_ir: Any,
    resolver_output: Any,
    plan: Mapping[str, Any],
    *,
    world_atoms: Any,
    character_atoms: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    schema_block = plan.get("REQUIRED_SCHEMA_UPDATES", {})
    atom_block = plan.get("REQUIRED_ATOM_UPDATES", {})
    if not isinstance(schema_block, Mapping) or not isinstance(atom_block, Mapping):
        raise PlaytestLoopBlocked("improvement plan is missing schema or atom update blocks")

    schema_result = apply_schema_patches(
        schemas,
        schema_block.get("SCHEMA_PATCH", []),
        strict=True,
    )
    accepted_schema_ids = {
        item["id"] for item in schema_result["APPLIED_OPERATIONS"]
    }
    atom_result = apply_atom_patches(
        atom_ir,
        atom_block.get("ATOM_PATCH", []),
        strict=True,
        enforce_mutation_rules=True,
        accepted_schema_ids=accepted_schema_ids,
    )
    resolver_result = apply_resolver_patch_plan(
        resolver_output,
        plan,
        atom_ir=atom_result["UPDATED_ATOM_IR"],
        strict=True,
    )
    updated_schemas = schema_result["UPDATED_SCHEMAS"]
    updated_atoms = atom_result["UPDATED_ATOM_IR"]
    projected_resolver = resolver_result["RESOLVER_OUTPUT"]
    diff = {
        "SCHEMA_DIFF": diff_schema_snapshots(schemas, updated_schemas),
        "ATOM_DIFF": diff_atom_ir(atom_ir, updated_atoms),
        "RESOLVER_OUTPUT_DIFF": diff_resolver_output(resolver_output, projected_resolver),
        "APPLIED_SCHEMA_OPERATIONS": schema_result["APPLIED_OPERATIONS"],
        "APPLIED_ATOM_OPERATIONS": atom_result["APPLIED_OPERATIONS"],
        "APPLIED_RESOLVER_PATCHES": resolver_result.get("APPLIED_PATCHES", []),
        "RESOLVER_VALIDATION": resolver_result["VALIDATION"],
        "WORLD_ATOMS": _clone_json(world_atoms, "WORLD_ATOMS"),
        "CHARACTER_ATOMS": _clone_json(character_atoms, "CHARACTER_ATOMS"),
    }
    return {
        "SCHEMAS": updated_schemas,
        "ATOM_IR": updated_atoms,
        "RESOLVER_OUTPUT": projected_resolver,
    }, diff


def _call_evaluator(
    evaluator: Callable[..., Any],
    *,
    schemas: Any,
    atom_ir: Any,
    world_atoms: Any,
    character_atoms: Any,
    resolver_output: Any,
) -> dict[str, Any]:
    return _as_report(evaluator(
        schemas=_clone_json(schemas, "SCHEMAS"),
        atom_ir=_clone_json(atom_ir, "ATOM_IR"),
        world_atoms=_clone_json(world_atoms, "WORLD_ATOMS"),
        character_atoms=_clone_json(character_atoms, "CHARACTER_ATOMS"),
        resolver_output=_clone_json(resolver_output, "RESOLVER_OUTPUT"),
    ))


def run_bounded_playtest_loop(
    *,
    schemas: Any,
    atom_ir: Any,
    resolver_output: Any,
    evaluator: Callable[..., Any],
    world_atoms: Any = (),
    character_atoms: Any = (),
    plan_builder: Callable[..., Any] = build_playtest_improvement_plan,
    resolver_rerun: Callable[..., Any] | None = None,
    previous_schemas: Any | None = None,
    previous_atom_ir: Any | None = None,
    thresholds: Mapping[str, Any] | None = None,
    require_graded_scores: bool = True,
    max_iterations: int = 3,
    auto_apply: bool = False,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run a bounded, deterministic mechanics improvement loop.

    ``max_iterations`` counts evaluation artifacts including iteration 000.
    ``auto_apply=False`` always stops after the first failed report with a
    review-required result.  In auto-apply mode, each accepted patch is
    followed by the supplied deterministic ``resolver_rerun`` hook (or the
    Slice 6 resolver-output projection fallback) before re-evaluation.
    """
    if not callable(evaluator):
        raise PlaytestLoopError("evaluator must be callable")
    if not callable(plan_builder):
        raise PlaytestLoopError("plan_builder must be callable")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or max_iterations < 1:
        raise PlaytestLoopError("max_iterations must be a positive integer")
    limits = _thresholds(thresholds)
    artifact_root = Path(report_dir) if report_dir is not None else None
    current_schemas = _clone_json(schemas, "SCHEMAS")
    current_atoms = _clone_json(atom_ir, "ATOM_IR")
    current_resolver = _clone_json(resolver_output, "RESOLVER_OUTPUT")
    world_snapshot = _clone_json(world_atoms, "WORLD_ATOMS")
    character_snapshot = _clone_json(character_atoms, "CHARACTER_ATOMS")
    prior_schemas = _clone_json(previous_schemas, "PREVIOUS_SCHEMAS") if previous_schemas is not None else None
    prior_atoms = _clone_json(previous_atom_ir, "PREVIOUS_ATOM_IR") if previous_atom_ir is not None else None

    seen_state_hashes: set[str] = set()
    failure_counts: dict[str, int] = {}
    iterations: list[dict[str, Any]] = []
    final_reason = ""
    status = "running"

    for iteration in range(max_iterations):
        state_hash = _state_hash(current_schemas, current_atoms, current_resolver)
        if state_hash in seen_state_hashes:
            status = "blocked"
            final_reason = "repeated content hash before evaluation"
            break
        seen_state_hashes.add(state_hash)
        iteration_schemas = _clone_json(current_schemas, "SCHEMAS")
        iteration_atoms = _clone_json(current_atoms, "ATOM_IR")
        iteration_resolver = _clone_json(current_resolver, "RESOLVER_OUTPUT")

        report = _call_evaluator(
            evaluator,
            schemas=current_schemas,
            atom_ir=current_atoms,
            world_atoms=world_snapshot,
            character_atoms=character_snapshot,
            resolver_output=current_resolver,
        )
        gate = evaluate_threshold_gate(
            report,
            thresholds=limits,
            require_graded_scores=require_graded_scores,
        )
        signature = _failure_signature(report, gate) if not gate["passed"] else ""
        if signature:
            failure_counts[signature] = failure_counts.get(signature, 0) + 1

        try:
            plan_value = plan_builder(
                schemas=_clone_json(current_schemas, "SCHEMAS"),
                atom_ir=_clone_json(current_atoms, "ATOM_IR"),
                world_atoms=_clone_json(world_snapshot, "WORLD_ATOMS"),
                character_atoms=_clone_json(character_snapshot, "CHARACTER_ATOMS"),
                resolver_output=_clone_json(current_resolver, "RESOLVER_OUTPUT"),
                playability_report=_clone_json(report, "PLAYABILITY_REPORT"),
                previous_schemas=_clone_json(prior_schemas, "PREVIOUS_SCHEMAS") if prior_schemas is not None else None,
                previous_atom_ir=_clone_json(prior_atoms, "PREVIOUS_ATOM_IR") if prior_atoms is not None else None,
                thresholds=limits,
                require_graded_scores=require_graded_scores,
            )
            plan = _as_mapping(plan_value, "PLAYTEST_IMPROVEMENT_PLAN")
        except Exception as exc:
            status = "blocked"
            final_reason = f"plan generation failed: {exc}"
            plan = _empty_plan()
            diff = {"status": "blocked", "reason": final_reason}
            artifact_path = _write_iteration_artifacts(
                artifact_root, iteration,
                schemas=iteration_schemas, atom_ir=iteration_atoms,
                resolver_output=iteration_resolver, report=report,
                plan=plan, gate=gate, diff=diff,
            )
            iterations.append({
                "iteration": iteration,
                "state_hash": state_hash,
                "failure_signature": signature,
                "gate": gate,
                "artifact_dir": artifact_path,
                "applied": False,
            })
            break

        diff: dict[str, Any] = {"status": "unchanged", "reason": "no patch applied"}
        artifact_path: str | None = None
        iteration_record = {
            "iteration": iteration,
            "state_hash": state_hash,
            "failure_signature": signature,
            "gate": gate,
            "artifact_dir": None,
            "applied": False,
        }

        if gate["passed"]:
            status = "passed"
            final_reason = "all required mechanics thresholds passed"
        elif not auto_apply:
            status = "review_required"
            final_reason = "threshold failure requires reviewed patch application"
        elif (
            failure_counts.get(signature, 0) >= 3
            and iteration + 1 < max_iterations
        ):
            status = "blocked"
            final_reason = "same failure signature repeated three times"
        else:
            try:
                next_state, diff = _apply_plan(
                    current_schemas,
                    current_atoms,
                    current_resolver,
                    plan,
                    world_atoms=world_snapshot,
                    character_atoms=character_snapshot,
                )
                next_hash = _state_hash(
                    next_state["SCHEMAS"],
                    next_state["ATOM_IR"],
                    next_state["RESOLVER_OUTPUT"],
                )
                if next_hash == state_hash:
                    status = "blocked"
                    final_reason = "accepted patch produced no content change"
                elif next_hash in seen_state_hashes:
                    status = "blocked"
                    final_reason = "accepted patch repeated a previous content hash"
                else:
                    next_resolver = next_state["RESOLVER_OUTPUT"]
                    if resolver_rerun is not None:
                        next_resolver = _as_mapping(resolver_rerun(
                            schemas=_clone_json(next_state["SCHEMAS"], "SCHEMAS"),
                            atom_ir=_clone_json(next_state["ATOM_IR"], "ATOM_IR"),
                            resolver_output=_clone_json(next_resolver, "RESOLVER_OUTPUT"),
                        ), "resolver_rerun output")
                    next_state["RESOLVER_OUTPUT"] = next_resolver
                    new_hash = _state_hash(
                        next_state["SCHEMAS"],
                        next_state["ATOM_IR"],
                        next_state["RESOLVER_OUTPUT"],
                    )
                    if new_hash == state_hash:
                        status = "blocked"
                        final_reason = "resolver rerun produced no content change"
                    elif new_hash in seen_state_hashes:
                        status = "blocked"
                        final_reason = "resolver rerun repeated a previous content hash"
                    else:
                        prior_schemas = current_schemas
                        prior_atoms = current_atoms
                        current_schemas = next_state["SCHEMAS"]
                        current_atoms = next_state["ATOM_IR"]
                        current_resolver = next_state["RESOLVER_OUTPUT"]
                        iteration_record["applied"] = True
                        iteration_record["next_state_hash"] = new_hash
            except Exception as exc:
                status = "blocked"
                final_reason = f"patch application failed: {exc}"
                diff = {"status": "blocked", "reason": final_reason}

        artifact_path = _write_iteration_artifacts(
            artifact_root, iteration,
            schemas=iteration_schemas,
            atom_ir=iteration_atoms,
            resolver_output=iteration_resolver,
            report=report, plan=plan, gate=gate, diff=diff,
        )
        iteration_record["artifact_dir"] = artifact_path
        iterations.append(iteration_record)

        if iteration_record["applied"]:
            if iteration + 1 >= max_iterations:
                status = "max_iterations"
                final_reason = "maximum evaluation iterations reached"
            else:
                status = "running"
        if status in {"passed", "review_required", "blocked", "max_iterations"}:
            break

    final_gate = iterations[-1]["gate"] if iterations else {
        "passed": False,
        "NEXT_ITERATION_REQUIRED": True,
        "FINAL_READY_FOR_RESOLVER": False,
        "reasons": [final_reason or "loop did not evaluate"],
    }
    ready = status == "passed" and bool(final_gate.get("FINAL_READY_FOR_RESOLVER"))
    return {
        "status": status,
        "reason": final_reason,
        "iterations": iterations,
        "iteration_count": len(iterations),
        "NEXT_ITERATION_REQUIRED": not ready,
        "FINAL_READY_FOR_RESOLVER": ready,
        "SCHEMAS": _clone_json(current_schemas, "SCHEMAS"),
        "ATOM_IR": _clone_json(current_atoms, "ATOM_IR"),
        "RESOLVER_OUTPUT": _clone_json(current_resolver, "RESOLVER_OUTPUT"),
        "thresholds": limits,
        "max_iterations": max_iterations,
        "auto_apply": auto_apply,
        "report_dir": str(artifact_root) if artifact_root is not None else None,
    }


run_playtest_loop = run_bounded_playtest_loop
evaluate_playtest_gate = evaluate_threshold_gate


__all__ = [
    "PlaytestLoopError",
    "PlaytestLoopBlocked",
    "DEFAULT_THRESHOLDS",
    "evaluate_threshold_gate",
    "evaluate_playtest_gate",
    "loop_config_from_mapping",
    "run_bounded_playtest_loop",
    "run_playtest_loop",
]
