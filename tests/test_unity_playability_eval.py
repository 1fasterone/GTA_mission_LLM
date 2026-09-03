"""
The playability eval, as a gate (`pytest -m playability`).

tests/test_unity_playability.py exercises the CHECKS with engineer-authored
schemas. This file exercises the COMPILER: every case replays a captured
schema-fill output through the deterministic tail and compares the result to a
floor locked in evals/playability/cases/. A drop in score, a check that used to
pass and now fails, or a check that quietly stops being gradable all fail here.

Marked `playability` rather than left in the default run because each case
generates a whole Unity project in memory (~1 s per case); `pytest -m
playability` is the gate, and CI runs it alongside the default suite.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.unity_eval import (CASES_DIR, EvalCase, EvalCaseError, grade_case,
                            load_cases, replay, write_logs)
from src.unity_playability import _CHECK_SEVERITY, ERROR
from src.unity_schema import UnitySchema

pytestmark = pytest.mark.playability

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES = load_cases()


def _ids(cases):
    return [c.name for c in cases]


# ── the gate ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_case_holds_its_floor(case: EvalCase, tmp_path):
    result = grade_case(case)
    # Logs go to tmp_path in tests so a run never depends on (or clobbers) the
    # working copy of evals/playability/_runs.
    write_logs(result, tmp_path)
    assert result.ok, (
        f"{case.name} regressed:\n  "
        + "\n  ".join(result.reasons())
        + "\n\nscorecard:\n" + result.report.render()
        + "\n\nwhere to look:\n  " + "\n  ".join(result.stage_hints()))


@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_every_error_class_check_is_accounted_for(case: EvalCase):
    """No ERROR-class check may be silently absent: it either graded (pass or
    fail) or is declared unavailable for this input in `ungraded_ok`. This is
    the invariant the old evaluator broke — it printed 96/100 for a build whose
    avatar was 2000 m below its own game, because four ERROR-class checks had
    never run and the score was the share of the ones that did."""
    result = grade_case(case)
    rep = result.report
    for name, severity in _CHECK_SEVERITY.items():
        if severity != ERROR:
            continue
        accounted = (name in rep.checks or name in rep.not_graded)
        assert accounted, (
            f"{case.name}: ERROR-class check {name!r} produced no verdict at "
            "all — it must record a pass, a failure, or why it could not be "
            "graded")
    assert not result.unexpected_ungraded, (
        f"{case.name}: {sorted(result.unexpected_ungraded)} stopped being "
        "graded and are not declared in the case's floor.ungraded_ok")


# ── case-file hygiene ─────────────────────────────────────────────────────────

def test_every_case_declares_whose_hand_wrote_its_input():
    """Eval discipline (CLAUDE.md): an eval that does not record whether its
    input is real schema-fill output or an engineer's fixture cannot say what a
    pass proves."""
    for case in CASES:
        assert case.input_kind in ("captured-llm", "engineer-authored")
        assert case.provenance.strip(), (
            f"{case.name}: floor without provenance — say where this schema "
            "came from")


def test_floors_are_locked_not_blank():
    """A case with a zero floor and no must-pass checks passes unconditionally
    and protects nothing."""
    for case in CASES:
        assert case.floor_score > 0, f"{case.name}: floor.score is 0"
        assert case.must_pass, f"{case.name}: floor.must_pass is empty"


def test_must_pass_and_ungraded_ok_name_real_checks():
    known = set(_CHECK_SEVERITY)
    for case in CASES:
        for name in (*case.must_pass, *case.ungraded_ok):
            assert name in known, (
                f"{case.name}: floor names unknown check {name!r} — a typo here "
                "silently stops gating that check")


def test_a_malformed_case_fails_loudly(tmp_path):
    (tmp_path / "bad.json").write_text(
        json.dumps({"name": "bad", "schema": "tests/fixtures/"
                    "dropzone_arcade_llm.schema.json"}), encoding="utf-8")
    with pytest.raises(EvalCaseError, match="input"):
        load_cases(tmp_path)


def test_an_unknown_case_name_is_an_error():
    with pytest.raises(EvalCaseError, match="unknown case"):
        load_cases(only=["no_such_case"])


# ── the replay must not drift from the pipeline ───────────────────────────────

# Functions that make up the pipeline's deterministic tail. The replay has to
# call every one the pipeline calls between its Step 4 and Step 6 markers, or it
# is grading something the pipeline does not build.
_TAIL_CALLS = (
    "resolve_schema", "inject_required_packages", "_programmatic_fix",
    "_check_consistency", "inject_missing_concept_entities",
    "_reconcile_scene_specs", "inject_missing_concept_scripts",
    "inject_missing_concept_scenes", "inject_missing_concept_audio",
    "inject_missing_concept_narrative", "inject_missing_concept_title",
    "compose_racetrack", "normalize_paths", "compute_layout",
    "build_manifest_deps",
)


def _pipeline_tail_source() -> str:
    src = (REPO_ROOT / "src" / "unity_pipeline.py").read_text(encoding="utf-8")
    start = src.index("self._step(4")
    end = src.index("self._step(6")
    return src[start:end]


def test_replay_calls_every_deterministic_tail_function_the_pipeline_does():
    tail = _pipeline_tail_source()
    replay_src = (REPO_ROOT / "src" / "unity_eval.py").read_text(
        encoding="utf-8")
    for fn in _TAIL_CALLS:
        if re.search(rf"\b{re.escape(fn)}\s*\(", tail) is None:
            continue          # the pipeline no longer calls it; nothing to mirror
        assert re.search(rf"\b{re.escape(fn)}\s*\(", replay_src), (
            f"src/unity_eval.replay() does not call {fn}(), but the pipeline's "
            "Step 4-6 tail does — the eval would grade a build the pipeline "
            "never produces")


def test_the_guard_list_still_matches_the_pipeline():
    """The list above is only useful while it describes the real tail. If the
    pipeline gains a deterministic step, this test is the reminder to add it."""
    tail = _pipeline_tail_source()
    found = {fn for fn in _TAIL_CALLS
             if re.search(rf"\b{re.escape(fn)}\s*\(", tail)}
    assert len(found) >= 9, (
        "the pipeline's Step 4-6 tail no longer contains the calls this guard "
        f"tracks (found only {sorted(found)}) — re-derive _TAIL_CALLS")


# ── the replay itself ─────────────────────────────────────────────────────────

def test_replay_is_deterministic():
    """Two replays of one schema must produce the same project, or a floor is
    measuring noise."""
    case = CASES[0]
    data = json.loads(case.schema_path.read_text(encoding="utf-8"))
    _s1, files_a, _l1 = replay(UnitySchema.model_validate(data))
    _s2, files_b, _l2 = replay(UnitySchema.model_validate(data))
    assert files_a == files_b


def test_replay_never_edits_the_captured_schema_on_disk():
    """The captures are held-out inputs. A replay that writes back to them
    would quietly convert an eval into a recording of itself."""
    for case in CASES:
        before = case.schema_path.read_bytes()
        grade_case(case)
        assert case.schema_path.read_bytes() == before, (
            f"{case.name}: replaying the case modified {case.schema_path.name}")


def test_case_files_cover_every_shipped_layout_primitive():
    """One case per spatial primitive, so a regression in any of the four
    cannot hide behind the other three."""
    primitives = {grade_case(c).report.primitive for c in CASES}
    assert {"airspace_column", "depth_column", "wall_surface"} <= primitives, (
        f"eval cases only cover {sorted(primitives)}")


def test_cases_dir_is_the_one_the_cli_uses():
    assert CASES_DIR.is_dir()
    assert {p.stem for p in CASES_DIR.glob("*.json")} == {c.name for c in CASES}
