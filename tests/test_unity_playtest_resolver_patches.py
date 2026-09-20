"""Acceptance tests for the Slice 6 resolver-patch ledger."""

from __future__ import annotations

import copy

import pytest

from src.unity_playtest_resolver_patches import (
    ResolverPatchBlocker,
    ResolverPatchError,
    ResolverRuleRegistry,
    apply_resolver_patch_ledger,
    apply_resolver_patch_plan,
    default_resolver_rule_registry,
    diff_resolver_output,
    resolver_patch_hash,
    stable_resolver_replay,
    validate_atom_resolver_coverage,
    validate_resolver_patch_ledger,
)


def _transition_patch(
    *,
    patch_id: str = "resolver-1",
    target_id: str = "event:gate",
    order: int = 50,
    replacement: dict | None = None,
) -> dict:
    return {
        "id": patch_id,
        "operation": "add",
        "rule_id": "unity.compose.objective_wiring",
        "input_shape": {"schema": "UnitySchema", "atoms": ["MoveTo"]},
        "target": {"kind": "transition", "id": target_id},
        "transition": replacement or {
            "id": target_id,
            "source": "composer_event",
            "target": "gate",
            "action": "reach",
            "progress": True,
            "completion": True,
        },
        "precedence": {"phase": "gameplay_wiring", "order": order},
        "validation_test": {
            "test_id": "tests/test_unity_mechanics_adapter.py",
            "fixture": "composer_wiring",
        },
        "trigger": "PLAYABILITY_REPORT.MISSION_FLOW_VALIDATION[0]",
        "reason": "The accepted objective needs an explicit event transition.",
    }


def _output() -> dict:
    return {
        "transitions": [{
            "id": "event:start",
            "source": "composer_event",
            "target": "start",
            "action": "reach",
            "progress": True,
            "completion": True,
        }],
        "dependencies": [],
    }


def test_default_registry_is_reviewed_and_deterministic():
    first = default_resolver_rule_registry()
    second = default_resolver_rule_registry()

    assert first.ids() == second.ids()
    assert "unity.compose.objective_wiring" in first.ids()
    assert first.get("unity.compose.objective_wiring")["reviewed"] is True


def test_unknown_rule_is_a_blocker_and_no_source_is_accepted():
    patch = _transition_patch()
    patch["rule_id"] = "unity.resolver.invented_rule"
    result = validate_resolver_patch_ledger([patch])

    assert result["status"] == "blocked"
    assert result["blockers"][0]["kind"] == "unknown_rule"
    with pytest.raises(ResolverPatchBlocker, match="unknown resolver rule"):
        validate_resolver_patch_ledger([patch], strict=True)

    patch = _transition_patch()
    patch["python"] = "def invented(): pass"
    result = validate_resolver_patch_ledger([patch])
    assert result["blockers"][0]["kind"] == "executable_source"


def test_reviewed_registry_entry_requires_fixture_and_can_be_used():
    registry = ResolverRuleRegistry()
    with pytest.raises(ResolverPatchError, match="explicitly reviewed"):
        registry.register({
            "rule_id": "test.unreviewed",
            "module": "src/test.py",
            "callable": "run",
            "input_shape": {"schema": "Fixture"},
            "precedence": {"phase": "test", "order": 1},
            "validation_test": {"test_id": "tests/test_fixture.py", "fixture": "fixture"},
            "reviewed": False,
        })
    registry.register({
        "rule_id": "test.reviewed",
        "module": "src/test.py",
        "callable": "run",
        "input_shape": {"schema": "Fixture"},
        "precedence": {"phase": "test", "order": 1},
        "validation_test": {"test_id": "tests/test_fixture.py", "fixture": "fixture"},
        "reviewed": True,
    })
    assert registry.require("test.reviewed")["module"] == "src/test.py"


def test_duplicate_identity_requires_matching_content_hash():
    patch = _transition_patch()
    patch["content_hash"] = resolver_patch_hash(patch)
    hashed = validate_resolver_patch_ledger([patch])
    assert hashed["status"] == "valid"
    result = validate_resolver_patch_ledger([patch, copy.deepcopy(patch)])
    assert result["status"] == "valid"
    assert result["duplicates"][0]["action"] == "deduplicated"
    assert result["content_hash"]

    conflict = copy.deepcopy(patch)
    conflict["transition"] = {**patch["transition"], "action": "collect"}
    conflict.pop("content_hash")
    result = validate_resolver_patch_ledger([patch, conflict])
    assert result["status"] == "blocked"
    assert result["blockers"][0]["kind"] == "conflicting_duplicate"


def test_conflicting_transition_precedence_is_reported():
    first = _transition_patch(patch_id="resolver-a", order=50)
    second = _transition_patch(patch_id="resolver-b", order=70)
    result = validate_resolver_patch_ledger([first, second])

    assert result["status"] == "blocked"
    assert any(item["kind"] == "conflicting_transition_precedence" for item in result["contradictions"])


def test_atom_coverage_requires_registered_unity_backend_path():
    covered = validate_atom_resolver_coverage([{"atom": "MoveTo", "args": {}}])
    assert covered["status"] == "valid"
    assert covered["covered"][0]["resolver_rule_id"] == "unity.backend.atom_snippet"

    blocked = validate_atom_resolver_coverage([{"atom": "InventedAtom", "args": {}}])
    assert blocked["status"] == "blocked"
    assert blocked["blockers"][0]["kind"] == "unknown_atom"


def test_apply_projection_is_detached_and_has_stable_transition_diff():
    resolver_output = _output()
    before = copy.deepcopy(resolver_output)
    patch = _transition_patch()

    result = apply_resolver_patch_ledger(resolver_output, [patch])

    assert resolver_output == before
    assert [item["id"] for item in result["RESOLVER_OUTPUT"]["transitions"]] == ["event:start", "event:gate"]
    added = result["RESOLVER_OUTPUT_DIFF"]["added_transitions"][0]
    assert added["id"] == "event:gate"
    assert added["operation_id"] == "resolver-1"
    assert added["source_report_path"].startswith("PLAYABILITY_REPORT")
    assert result["SNAPSHOT_HASH"]

    with pytest.raises(ResolverPatchError, match="already exists"):
        apply_resolver_patch_ledger(result["RESOLVER_OUTPUT"], [patch])


def test_modify_and_remove_require_exact_output_preconditions():
    baseline = _output()
    modify = _transition_patch(patch_id="modify-1", target_id="event:start")
    modify["operation"] = "modify"
    modify["transition"] = {**baseline["transitions"][0], "action": "complete"}
    modify["expected"] = baseline["transitions"][0]
    modified = apply_resolver_patch_ledger(baseline, [modify])
    assert modified["RESOLVER_OUTPUT"]["transitions"][0]["action"] == "complete"

    remove = _transition_patch(patch_id="remove-1", target_id="event:start")
    remove["operation"] = "remove"
    remove.pop("transition")
    remove["expected"] = baseline["transitions"][0]
    removed = apply_resolver_patch_ledger(baseline, [remove])
    assert removed["RESOLVER_OUTPUT"]["transitions"] == []

    remove["expected"] = {"id": "not-the-baseline"}
    with pytest.raises(ResolverPatchError, match="expected-value precondition"):
        apply_resolver_patch_ledger(baseline, [remove])


def test_stable_replay_and_plan_projection_are_repeatable():
    patch = _transition_patch()
    replay = stable_resolver_replay(_output(), [patch])
    assert replay["stable"] is True
    assert replay["first"]["RESOLVER_OUTPUT"] == replay["second"]["RESOLVER_OUTPUT"]
    assert resolver_patch_hash(patch) == resolver_patch_hash(copy.deepcopy(patch))

    plan = {"RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": [patch]}}
    result = apply_resolver_patch_plan(_output(), plan)
    assert result["APPLIED_PATCHES"][0]["id"] == "resolver-1"


def test_abstention_blocks_strict_apply_but_review_mode_preserves_baseline():
    abstain = {
        "id": "blocked-1",
        "operation": "abstain",
        "reason": "No deterministic precedence is proven.",
    }
    result = validate_resolver_patch_ledger([abstain])
    assert result["status"] == "blocked"
    with pytest.raises(ResolverPatchBlocker, match="abstention"):
        apply_resolver_patch_ledger(_output(), [abstain])
    reviewed = apply_resolver_patch_ledger(_output(), [abstain], strict=False)
    assert reviewed["status"] == "blocked"
    assert reviewed["RESOLVER_OUTPUT"] == _output()


def test_output_diff_has_empty_required_lists_and_deterministic_order():
    diff = diff_resolver_output(_output(), _output())
    assert diff["status"] == "unchanged"
    assert diff["added_transitions"] == []
    assert diff["removed_transitions"] == []
    assert diff["modified_transitions"] == []
    assert diff["added_dependencies"] == []
    assert diff["removed_dependencies"] == []
    assert diff["modified_dependencies"] == []
