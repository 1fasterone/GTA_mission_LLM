"""
Playability eval harness — the compiler graded on real inputs, against a floor.

`src/unity_playability.py` and `src/unity_playability_artifact.py` say how good
one build is. This module says whether the compiler got WORSE, which is a
different and more useful question: it replays a fixed set of captured Step-3
schemas through the deterministic tail (Steps 4-6), grades the schema AND the
emitted files, and compares the result to a floor locked in the case file.

Why a harness at all, when `tests/test_unity_playability.py` already exists:
those tests are engineer-authored schemas exercising the CHECKS. They cannot
catch a regression in the pipeline, because the engineer writes their input.
These cases are real schema-fill output, so what they exercise is the compiler.

Eval discipline (CLAUDE.md): every case declares where its input came from.
`input: "captured-llm"` is verbatim Step-3 output that no engineer edited;
`input: "engineer-authored"` is a hand-written schema, which still tests Steps
4-7 but proves nothing about Steps 2-3. A case only carries a `brief` when that
brief is known to have produced that schema — otherwise brief coverage stays
ungraded rather than being compared against a brief that may not be its own.

Two tiers:
  * Tier R (replay) — what this module runs. No LLM, no Unity, no network, so
    it is deterministic and CI-safe.
  * Tier L (live) — `unity-eval --live`, the full pipeline from brief text
    through LM Studio. Deferred: the flag exists and shares this grading path,
    but no live cases ship yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .unity_concept_audio import inject_missing_concept_audio
from .unity_concept_entities import inject_missing_concept_entities
from .unity_concept_narrative import inject_missing_concept_narrative
from .unity_concept_scenes import inject_missing_concept_scenes
from .unity_concept_scripts import inject_missing_concept_scripts
from .unity_concept_title import inject_missing_concept_title
from .unity_generator import UnityProjectGenerator
from .unity_layout import compute_layout
from .unity_pathnorm import normalize_paths
from .unity_pipeline import (_check_consistency, _programmatic_fix,
                             _reconcile_scene_specs)
from .unity_playability import PlayabilityReport, _CHECK_SEVERITY, ERROR
from .unity_playability_artifact import evaluate_project
from .unity_racetrack import compose_racetrack, is_racing_degenerate
from .unity_resolver import UnityResolver
from .unity_schema import UnitySchema

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = REPO_ROOT / "evals" / "playability" / "cases"
RUNS_DIR = REPO_ROOT / "evals" / "playability" / "_runs"


class EvalCaseError(RuntimeError):
    """A case file that cannot be run — malformed, or pointing at a schema that
    is not on disk. Loud, because a silently skipped case is a case that stops
    protecting anything."""


@dataclass(frozen=True)
class EvalCase:
    name: str
    schema_path: Path
    # "captured-llm" | "engineer-authored" — see the module docstring.
    input_kind: str
    provenance: str = ""
    notes: str = ""
    brief_path: Optional[Path] = None
    floor_score: int = 0
    must_pass: tuple[str, ...] = ()
    # ERROR-class checks this input genuinely cannot supply evidence for (a
    # captured schema has no user_prompt, so brief coverage is uncheckable).
    # Anything ungraded OUTSIDE this list is a regression: a check stopped
    # running and the score silently stopped covering it.
    ungraded_ok: tuple[str, ...] = ()

    @property
    def declares_brief(self) -> bool:
        return self.brief_path is not None


@dataclass
class EvalResult:
    case: EvalCase
    report: PlayabilityReport
    regressed: list[str] = field(default_factory=list)
    unexpected_ungraded: dict[str, str] = field(default_factory=dict)
    replay_log: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return self.report.score

    @property
    def below_floor(self) -> bool:
        return self.score < self.case.floor_score

    @property
    def ok(self) -> bool:
        return not (self.below_floor or self.regressed
                    or self.unexpected_ungraded)

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.below_floor:
            out.append(f"score {self.score}/100 below the locked floor "
                       f"{self.case.floor_score}")
        if self.regressed:
            out.append("checks that must pass are failing: "
                       + ", ".join(sorted(self.regressed)))
        for name, why in sorted(self.unexpected_ungraded.items()):
            out.append(f"{name} stopped being graded ({why})")
        return out

    def stage_hints(self) -> list[str]:
        """The pipeline stages the findings point at — what to open first."""
        return sorted({f.source for f in self.report.findings if f.source})

    def to_json(self) -> dict:
        checks: dict[str, str] = {
            n: ("PASS" if ok else "FAIL")
            for n, ok in sorted(self.report.checks.items())}
        for name, why in sorted(self.report.not_graded.items()):
            checks[name] = f"NOT_GRADED: {why}"
        return {
            "case": self.case.name,
            "input": self.case.input_kind,
            "provenance": self.case.provenance,
            "schema": str(self.case.schema_path).replace("\\", "/"),
            "verdict": "PASS" if self.ok else "FAIL",
            "score": self.score,
            "floor": self.case.floor_score,
            "playable": self.report.playable,
            "primitive": self.report.primitive,
            "reasons": self.reasons(),
            "regressed": sorted(self.regressed),
            "unexpected_ungraded": self.unexpected_ungraded,
            "checks": checks,
            "findings": [
                {"check": f.check, "severity": f.severity,
                 "summary": f.summary, "source": f.source}
                for f in sorted(self.report.findings,
                                key=lambda f: (f.severity, f.check))],
            "stage_hints": self.stage_hints(),
            "replay_log": self.replay_log,
        }


# ── case loading ──────────────────────────────────────────────────────────────

def _case_from_dict(data: dict, path: Path) -> EvalCase:
    try:
        name = str(data["name"])
        schema_rel = str(data["schema"])
        input_kind = str(data["input"])
    except KeyError as exc:  # noqa: PERF203 — one message per missing key is fine
        raise EvalCaseError(
            f"{path.name}: missing required key {exc.args[0]!r} "
            "(name, schema, input are mandatory)") from exc
    if input_kind not in ("captured-llm", "engineer-authored"):
        raise EvalCaseError(
            f"{path.name}: input must be 'captured-llm' or "
            f"'engineer-authored', not {input_kind!r} — an eval that does not "
            "declare whose hand wrote its input measures nothing")
    schema_path = (REPO_ROOT / schema_rel).resolve()
    if not schema_path.is_file():
        raise EvalCaseError(f"{path.name}: schema not on disk: {schema_rel}")
    brief_rel = data.get("brief")
    brief_path = (REPO_ROOT / str(brief_rel)).resolve() if brief_rel else None
    if brief_path is not None and not brief_path.is_file():
        raise EvalCaseError(f"{path.name}: brief not on disk: {brief_rel}")
    floor = data.get("floor") or {}
    return EvalCase(
        name=name,
        schema_path=schema_path,
        input_kind=input_kind,
        provenance=str(data.get("provenance", "")),
        notes=str(data.get("notes", "")),
        brief_path=brief_path,
        floor_score=int(floor.get("score", 0)),
        must_pass=tuple(floor.get("must_pass") or ()),
        ungraded_ok=tuple(floor.get("ungraded_ok") or ()),
    )


def load_cases(cases_dir: Path | None = None,
               only: Iterable[str] | None = None) -> list[EvalCase]:
    d = Path(cases_dir) if cases_dir else CASES_DIR
    if not d.is_dir():
        raise EvalCaseError(f"no eval case directory at {d}")
    wanted = {s.strip() for s in only} if only else None
    cases = [_case_from_dict(json.loads(p.read_text(encoding="utf-8")), p)
             for p in sorted(d.glob("*.json"))]
    if wanted is not None:
        known = {c.name for c in cases}
        missing = wanted - known
        if missing:
            raise EvalCaseError(
                f"unknown case(s) {', '.join(sorted(missing))} — have: "
                + ", ".join(sorted(known)))
        cases = [c for c in cases if c.name in wanted]
    if not cases:
        raise EvalCaseError(f"no eval cases found in {d}")
    return cases


# ── the replay ────────────────────────────────────────────────────────────────
# Mirrors UnityPipeline.run() Steps 4-6 by calling the SAME functions in the
# same order. tests/test_unity_playability_eval.py asserts that every
# deterministic-tail function the pipeline calls between its Step 4 and Step 6
# markers is called here too, so this cannot quietly drift away from the
# pipeline it is supposed to be measuring.

def replay(schema: UnitySchema, *,
           lookups_dir: str | None = None,
           template_dir: str | None = None) -> tuple[UnitySchema,
                                                     dict[str, str],
                                                     list[str]]:
    """Run the deterministic tail on a filled schema. No LLM, no network, no
    Blender, no disk writes. Returns (final schema, emitted files, log)."""
    lookups = lookups_dir or str(REPO_ROOT / "lookups")
    templates = template_dir or str(REPO_ROOT / "templates" / "unity")
    log: list[str] = []
    resolver = UnityResolver(lookups_dir=lookups)

    # Step 4 — collect requirements; resolve once after all schema mutations.
    schema = resolver.inject_required_packages(schema)

    # Step 5 — fixer, consistency, concept rescues, scaffold, normalization
    schema = _programmatic_fix(schema)
    schema, msgs = _check_consistency(schema, reconcile_scenes=False)
    log += msgs
    log += inject_missing_concept_entities(schema)
    log += inject_missing_concept_scenes(schema)
    log += inject_missing_concept_scripts(schema)
    log += inject_missing_concept_audio(schema)
    log += inject_missing_concept_narrative(schema)
    log += inject_missing_concept_title(schema)
    if ((schema.meta or {}).get("genre") == "racing"
            and schema.game_type not in ("2D", "2d_topdown")
            and is_racing_degenerate(schema)):
        log += compose_racetrack(schema)
    log += normalize_paths(schema)
    schema, scene_msgs = _reconcile_scene_specs(schema)
    log += scene_msgs
    log += compute_layout(schema)

    # Step 6 — resolve once, then generate. strict_assets stays off: an eval must grade
    # playability, not fail on a local asset library it did not ship with.
    schema = resolver.resolve_schema(schema)
    deps = resolver.build_manifest_deps(schema, inject_mcp_bridge=False)
    files = UnityProjectGenerator(template_dir=templates).generate(
        schema, deps, lookups_dir=lookups, strict_assets=False,
        sketchfab_cfg={"enabled": False, "auto_fetch": False,
                       "vision_select": False})
    return schema, files, log


def grade_case(case: EvalCase, **replay_kw) -> EvalResult:
    """Replay one case and compare it to its locked floor."""
    data = json.loads(case.schema_path.read_text(encoding="utf-8"))
    schema = UnitySchema.model_validate(data)
    if case.brief_path is not None:
        # Only when the case declares that this brief produced this schema —
        # `brief_entities` compares the build against it.
        meta = dict(schema.meta or {})
        meta.setdefault("user_prompt",
                        case.brief_path.read_text(encoding="utf-8"))
        schema.meta = meta
    schema, files, log = replay(schema, **replay_kw)
    report = evaluate_project(schema, files)
    regressed = [c for c in case.must_pass
                 if not report.checks.get(c, False)]
    unexpected = {n: why for n, why in report.ungraded_errors.items()
                  if n not in case.ungraded_ok}
    return EvalResult(case=case, report=report, regressed=regressed,
                      unexpected_ungraded=unexpected, replay_log=log)


def write_logs(result: EvalResult,
               runs_dir: Path | None = None) -> tuple[Path, Path]:
    """Write the machine-readable failure log plus the human scorecard. The
    JSON is the artifact a fix loop reads: every finding carries the pipeline
    stage that produced it, so a failure points somewhere."""
    d = Path(runs_dir) if runs_dir else RUNS_DIR
    d.mkdir(parents=True, exist_ok=True)
    js = d / f"{result.case.name}.json"
    txt = d / f"{result.case.name}.txt"
    js.write_text(json.dumps(result.to_json(), indent=2) + "\n",
                  encoding="utf-8")
    header = (f"case={result.case.name}  input={result.case.input_kind}  "
              f"verdict={'PASS' if result.ok else 'FAIL'}  "
              f"floor={result.case.floor_score}")
    body = [header, result.report.render()]
    if not result.ok:
        body.append("WHY THIS FAILED")
        body += [f"  - {r}" for r in result.reasons()]
        body.append("WHERE TO LOOK")
        body += [f"  - {s}" for s in result.stage_hints()]
    txt.write_text("\n".join(body) + "\n", encoding="utf-8")
    return js, txt


def run_cases(cases: Iterable[EvalCase], *, write: bool = True,
              runs_dir: Path | None = None, **replay_kw) -> list[EvalResult]:
    results = []
    for case in cases:
        result = grade_case(case, **replay_kw)
        if write:
            write_logs(result, runs_dir)
        results.append(result)
    return results


def floor_from_result(result: EvalResult) -> dict:
    """The floor block that locks in what a run currently achieves — used by
    `unity-eval --update-floors` after a deliberate improvement."""
    return {
        "score": result.score,
        "must_pass": sorted(n for n, ok in result.report.checks.items() if ok),
        "ungraded_ok": sorted(result.report.ungraded_errors),
    }


def error_class_checks() -> list[str]:
    return sorted(n for n, sev in _CHECK_SEVERITY.items() if sev == ERROR)
