"""Failed-run diagnostics for the Unity pipeline (``unity-failed-run/v1``).

When :meth:`UnityPipeline.run` raises, the pipeline writes one structured
report describing WHERE the run died, WHY (a deterministic category plus a
remedy), and WHOSE hand wrote each artifact that survived it:

- the phase reached (last ``STEP`` banner, last intent-ledger checkpoint, the
  single-execution stages that completed);
- whether the Step 3.5 freeze boundary had been crossed, so a reviewer can tell
  a Pipeline-LLM failure from a deterministic-compiler failure at a glance;
- the schema source (``schema_llm`` / ``schema_deterministic``) and concept
  source (``llm`` / ``precomputed`` / ``none``) — the Eval Discipline answer to
  "whose hand wrote this";
- every raw Step 3 fill response and its rejection reason;
- the live schema at failure, saved beside the report, with a replay command
  when the run had already passed the freeze boundary.

This is review provenance only, in the same contract as
``unity-create-run-log/v2`` (docs/UNITY_RUN_LOGS.md): it never enters the
schema, is never supplied to the Pipeline LLM and is never consumed by
Steps 4-7. Writing it is best-effort observation and can never replace or mask
the original exception. docs/FAILED_RUN_DIAGNOSTICS.md is the reference.
"""
from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FAILED_RUN_FORMAT = "unity-failed-run/v1"
FAILED_RUN_DIRNAME = "_failed_runs"
# A raw fill response is evidence, but a runaway generation must not turn one
# diagnostic into a multi-megabyte file. The cut is recorded, never silent.
RAW_RESPONSE_LIMIT = 200_000
_COMPILER_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

SchemaSource = Literal["schema_llm", "schema_deterministic"]
ConceptSource = Literal["llm", "precomputed", "none"]


# ── Report schema ──────────────────────────────────────────────────────────────

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExceptionLink(_Strict):
    """One exception in the ``__cause__`` / ``__context__`` chain, outermost first."""

    type: str
    module: str
    message: str


class FailureRecord(_Strict):
    exception_type: str
    message: str
    category: str = Field(description="Deterministic failure category; "
                                      "'unclassified' when no rule matches")
    matched_exception: str | None = Field(
        description="The chain link whose type decided the category")
    remedy: str
    chain: list[ExceptionLink]
    traceback: str


class StepMark(_Strict):
    number: str
    title: str


class PhaseRecord(_Strict):
    failed_phase: str
    last_step: StepMark | None
    last_checkpoint: str | None
    checkpoints_passed: list[str]
    completed_stages: list[str]


class LlmBoundaryRecord(_Strict):
    llm_enabled: bool
    frozen_at_failure: bool = Field(
        description="True once Step 3.5 froze the client: the failure is "
                    "in deterministic Steps 4-7, not the Pipeline LLM")


class AuthorshipRecord(_Strict):
    schema_source: SchemaSource | None
    concept_source: ConceptSource | None
    note: str


class FillAttempt(_Strict):
    attempt: int
    raw_response: str | None
    raw_response_chars: int
    raw_truncated: bool
    error_type: str | None
    error: str | None


class PromptRecord(_Strict):
    sha256: str
    chars: int
    text: str


class SchemaArtifact(_Strict):
    available: bool
    state: Literal["live_at_failure", "none"]
    after_checkpoint: str | None
    project_name: str | None
    path: str | None
    sha256: str | None
    dump_error: str | None


class FailedRunReport(_Strict):
    format: Literal["unity-failed-run/v1"] = "unity-failed-run/v1"
    status: Literal["failed"] = "failed"
    created_utc: str
    source_lineage: dict[str, Any]
    prompt: PromptRecord
    options: dict[str, Any]
    failure: FailureRecord
    phase: PhaseRecord
    llm_boundary: LlmBoundaryRecord
    authorship: AuthorshipRecord
    concept: str | None
    fill_attempts: list[FillAttempt]
    warnings: list[str]
    intent_ledger: dict[str, Any] | None
    schema_artifact: SchemaArtifact
    replay_command: str | None


def failed_run_json_schema() -> dict[str, Any]:
    """The JSON Schema of :class:`FailedRunReport` (for external tooling)."""
    return FailedRunReport.model_json_schema()


# ── Failure classification ─────────────────────────────────────────────────────

# (exception class name, category, remedy). Matched against every class in each
# chain link's MRO, outermost link first, so a RuntimeError wrapping a
# SpatialPipelineError is classified by the spatial cause. Names, not imports:
# classification must never import the modules whose failure it is describing.
_CATEGORY_RULES: tuple[tuple[str, str, str], ...] = (
    ("PostSchemaLLMError", "llm_freeze_violation",
     "A Step 4-7 code path called the Pipeline LLM. Move that content into "
     "Step 3.5 and persist it into the schema (docs/LLM_FREEZE_BOUNDARY.md)."),
    ("CSharpSyntaxError", "csharp_syntax_gate",
     "Generated C# failed the syntax gate. Fix the generator or template at "
     "the root, then replay the saved schema with unity-validate."),
    ("PlayabilityGateError", "playability_gate",
     "Step 7 rejected the build. Read playability.txt for the failing check, "
     "fix it at its owning pass, or relax playability.gate in config.yaml."),
    ("SpatialPipelineError", "spatial_layout",
     "Layout abstained. Read the [layout.strict] remedy and supply the missing "
     "spatial fact or axis (docs/LAYOUT_ARCHETYPES.md)."),
    ("SpatialRealizationError", "spatial_layout",
     "Spatial realization abstained. Read the named remedy in the message "
     "(docs/SPATIAL_LAYOUT_PLAN.md)."),
    ("SceneInputError", "authored_scene",
     "The Scene Compiler layout could not be read or bound. Check the layout "
     "file and its model paths (docs/SCENE_COMPILER.md)."),
    ("BriefParseError", "brief_parse",
     "The --no-llm brief parser rejected the brief. Add the missing structured "
     "section the message names."),
    ("ReferenceImageError", "reference_image",
     "The reference image could not be read or encoded. Check the path and "
     "format."),
    ("VisualReasoningCompilerError", "visual_compiler",
     "The visual reasoning compiler abstained. Read its message "
     "(docs/VISUAL_COMPILER.md)."),
    ("ValidationError", "schema_validation",
     "The schema failed Pydantic validation. The first error names the field; "
     "for a Step 3 fill, see fill_attempts for the raw response."),
    ("APIConnectionError", "llm_transport",
     "The LM backend was unreachable. Start LM Studio, load the configured "
     "model and check lm_studio.base_url."),
    ("APITimeoutError", "llm_transport",
     "The LM backend timed out. Check the loaded model and lm_studio timeouts."),
    ("ConnectError", "llm_transport",
     "The LM backend was unreachable. Start LM Studio and check "
     "lm_studio.base_url."),
    ("ConnectionError", "llm_transport",
     "A network connection failed. Check LM Studio and any asset service "
     "named in the message."),
    ("TimeoutError", "timeout",
     "An operation timed out. The traceback names the stage."),
    ("KeyboardInterrupt", "interrupted",
     "The run was interrupted by the user. Nothing to fix."),
    ("MemoryError", "resources",
     "The process ran out of memory. Free GPU/RAM (LM Studio, ComfyUI) and "
     "retry."),
    ("OSError", "filesystem",
     "A file or device operation failed. The message names the path."),
)

_SCHEMA_FILL_REMEDY = (
    "Every Step 3 fill attempt was rejected. Read fill_attempts: an empty or "
    "truncated response points at the loaded model or its context window; a "
    "field error points at the schema prompt."
)
_UNCLASSIFIED_REMEDY = (
    "No deterministic rule matches this exception chain. Read the traceback, "
    "and add a rule to unity_failed_run._CATEGORY_RULES once its remedy is "
    "known."
)


def exception_chain(exc: BaseException) -> list[BaseException]:
    """Outermost-first ``__cause__`` / ``__context__`` chain, cycle-safe."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def classify_failure(
    exc: BaseException,
    *,
    fill_failed: bool = False,
) -> tuple[str, str | None, str]:
    """Return ``(category, matched exception type, remedy)``.

    Deterministic: the first rule (in table order) that matches any class in
    the outermost link's MRO wins, then the next link, and so on. A Step 3
    fill whose every attempt failed is ``schema_fill`` regardless of the
    wrapping type. No match abstains loudly as ``unclassified``.
    """
    if fill_failed:
        return "schema_fill", type(exc).__name__, _SCHEMA_FILL_REMEDY
    for link in exception_chain(exc):
        names = {cls.__name__ for cls in type(link).__mro__}
        for name, category, remedy in _CATEGORY_RULES:
            if name in names:
                return category, type(link).__name__, remedy
    return "unclassified", None, _UNCLASSIFIED_REMEDY


# ── Phase mapping ──────────────────────────────────────────────────────────────

# Phase entered AFTER each intent-ledger checkpoint (src/unity_pipeline.py).
_PHASE_AFTER_CHECKPOINT: dict[str | None, str] = {
    None: "setup (before Step 2)",
    "Pre-Step3": "Steps 2-3 (concept + schema fill)",
    "Post-Step3": "Step 3 intent resolution",
    "Post-Resolution": "Step 3.5 enrichment",
    "Post-Reconciliation": "Step 5 validation, asset binding and compose",
    "Post-Compose": "Step 5 pre-generation",
    "Pre-Generation": "Steps 6-7 (generation + playability)",
}
_POST_FREEZE_AFTER_RESOLUTION = "Steps 4-5 (resolve + fixer)"


def failed_phase(last_checkpoint: str | None, frozen: bool) -> str:
    if last_checkpoint == "Post-Resolution" and frozen:
        return _POST_FREEZE_AFTER_RESOLUTION
    phase = _PHASE_AFTER_CHECKPOINT.get(last_checkpoint)
    if phase is None:
        return f"after unknown checkpoint {last_checkpoint!r}"
    return phase


# ── Run-time collector ─────────────────────────────────────────────────────────

@dataclass
class RunDiagnostics:
    """Observation-only state the pipeline updates as one run advances.

    Holds references, never copies, so observing costs nothing on a run that
    succeeds. Nothing here is read by any compiler step.
    """

    user_prompt: str
    options: dict[str, Any] = field(default_factory=dict)
    llm_enabled: bool = True
    stage_registry: Any = None
    intent_ledger: Any = None
    schema: Any = None
    checkpoints: list[str] = field(default_factory=list)
    last_step: tuple[str, str] | None = None
    frozen: bool = False
    schema_source: SchemaSource | None = None
    concept_source: ConceptSource | None = None
    concept: str | None = None
    fill_attempts: list[dict[str, Any]] = field(default_factory=list)
    fill_failed: bool = False
    warnings: list[str] = field(default_factory=list)

    def step(self, number: int | str, title: str) -> None:
        self.last_step = (str(number), title)

    def checkpoint(self, name: str, schema: Any = None) -> None:
        self.checkpoints.append(name)
        if schema is not None:
            self.schema = schema

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def record_fill_attempt(
        self,
        attempt: int,
        raw_response: str | None,
        error: BaseException | None,
    ) -> None:
        self.fill_attempts.append({
            "attempt": attempt,
            "raw_response": raw_response,
            "error_type": type(error).__name__ if error is not None else None,
            "error": str(error) if error is not None else None,
        })


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fill_attempt_records(attempts: list[dict[str, Any]]) -> list[FillAttempt]:
    records = []
    for item in attempts:
        raw = item.get("raw_response")
        raw_chars = len(raw) if isinstance(raw, str) else 0
        truncated = raw_chars > RAW_RESPONSE_LIMIT
        records.append(FillAttempt(
            attempt=int(item["attempt"]),
            raw_response=(raw[:RAW_RESPONSE_LIMIT] if truncated else raw)
            if isinstance(raw, str) else None,
            raw_response_chars=raw_chars,
            raw_truncated=truncated,
            error_type=item.get("error_type"),
            error=item.get("error"),
        ))
    return records


def _authorship_note(schema_source: SchemaSource | None,
                     concept_source: ConceptSource | None) -> str:
    if schema_source == "schema_deterministic":
        return ("Schema written by the deterministic --no-llm brief parser; "
                "this run says nothing about Step 3.")
    if schema_source == "schema_llm":
        concept = {
            "precomputed": "a precomputed (non-Step-2) concept",
            "llm": "the Step 2 Pipeline LLM concept",
        }.get(concept_source or "", "an unrecorded concept source")
        return f"Schema filled by the Step 3 Pipeline LLM from {concept}."
    return "No schema existed when the run failed."


def _ledger_export(ledger: Any) -> dict[str, Any] | None:
    to_dict = getattr(ledger, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        exported = to_dict()
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        return None
    return exported if isinstance(exported, dict) else None


def _source_lineage() -> dict[str, Any]:
    try:
        from .unity_run_lineage import capture_source_lineage

        return capture_source_lineage(_COMPILER_REPOSITORY_ROOT)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
        return {"classification": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}"}


# Run options that change what ``unity-validate`` regenerates. A replay that
# drops one does not reproduce the failed run (e.g. without ``--compose`` the
# replay leaves Assets/Micros/** untouched).
_REPLAY_FLAGS: tuple[tuple[str, str], ...] = (
    ("compose", "--compose"),
    ("blender_models", "--blender-models"),
)


def replay_command_for(schema_path: Path, options: dict[str, Any]) -> str:
    """``unity-validate`` command reproducing the run's recorded build mode."""
    parts = ["python", "main.py", "unity-validate", str(schema_path)]
    parts.extend(flag for option, flag in _REPLAY_FLAGS if options.get(option) is True)
    return " ".join(parts)


def build_failed_run_report(
    diagnostics: RunDiagnostics,
    exc: BaseException,
    *,
    schema_path: Path | None = None,
    schema_sha256: str | None = None,
    schema_dump_error: str | None = None,
    now: datetime | None = None,
    source_lineage: dict[str, Any] | None = None,
) -> FailedRunReport:
    """Assemble the report. Pure apart from the optional lineage capture."""
    category, matched, remedy = classify_failure(
        exc, fill_failed=diagnostics.fill_failed
    )
    chain = [
        ExceptionLink(type=type(link).__name__, module=type(link).__module__,
                      message=str(link))
        for link in exception_chain(exc)
    ]
    last_checkpoint = diagnostics.checkpoints[-1] if diagnostics.checkpoints else None
    completed = list(getattr(diagnostics.stage_registry, "executed", []) or [])
    schema = diagnostics.schema
    project_name = getattr(schema, "project_name", None) if schema is not None else None
    available = schema_path is not None
    replay = None
    if schema_path is not None and diagnostics.frozen:
        replay = replay_command_for(schema_path, diagnostics.options)
    prompt_bytes = diagnostics.user_prompt.encode("utf-8")
    return FailedRunReport(
        created_utc=_iso_utc(now or datetime.now(timezone.utc)),
        source_lineage=(source_lineage if source_lineage is not None
                        else _source_lineage()),
        prompt=PromptRecord(
            sha256=hashlib.sha256(prompt_bytes).hexdigest(),
            chars=len(diagnostics.user_prompt),
            text=diagnostics.user_prompt,
        ),
        options=dict(diagnostics.options),
        failure=FailureRecord(
            exception_type=type(exc).__name__,
            message=str(exc),
            category=category,
            matched_exception=matched,
            remedy=remedy,
            chain=chain,
            traceback="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        ),
        phase=PhaseRecord(
            failed_phase=failed_phase(last_checkpoint, diagnostics.frozen),
            last_step=(StepMark(number=diagnostics.last_step[0],
                                title=diagnostics.last_step[1])
                       if diagnostics.last_step else None),
            last_checkpoint=last_checkpoint,
            checkpoints_passed=list(diagnostics.checkpoints),
            completed_stages=completed,
        ),
        llm_boundary=LlmBoundaryRecord(
            llm_enabled=diagnostics.llm_enabled,
            frozen_at_failure=diagnostics.frozen,
        ),
        authorship=AuthorshipRecord(
            schema_source=diagnostics.schema_source,
            concept_source=diagnostics.concept_source,
            note=_authorship_note(diagnostics.schema_source,
                                  diagnostics.concept_source),
        ),
        concept=diagnostics.concept,
        fill_attempts=_fill_attempt_records(diagnostics.fill_attempts),
        warnings=list(diagnostics.warnings),
        intent_ledger=_ledger_export(diagnostics.intent_ledger),
        schema_artifact=SchemaArtifact(
            available=available,
            state="live_at_failure" if available else "none",
            after_checkpoint=last_checkpoint if available else None,
            project_name=project_name,
            path=str(schema_path) if schema_path is not None else None,
            sha256=schema_sha256,
            dump_error=schema_dump_error,
        ),
        replay_command=replay,
    )


def write_failed_run_report(
    diagnostics: RunDiagnostics,
    exc: BaseException,
    output_dir: str | Path,
    *,
    now: datetime | None = None,
    source_lineage: dict[str, Any] | None = None,
) -> Path:
    """Write ``<output_dir>/_failed_runs/<stem>.json`` (+ ``<stem>.schema.json``).

    Raises ``OSError`` only for the report file itself; the caller treats that
    as best-effort. A schema that cannot be dumped is recorded in the report,
    never raised.
    """
    stamp_time = now or datetime.now(timezone.utc)
    prompt_sha = hashlib.sha256(diagnostics.user_prompt.encode("utf-8")).hexdigest()
    target_dir = Path(output_dir) / FAILED_RUN_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"failed-run-{stamp_time.strftime('%Y%m%dT%H%M%S.%fZ')}-{prompt_sha[:12]}"

    schema_path: Path | None = None
    schema_sha: str | None = None
    dump_error: str | None = None
    schema = diagnostics.schema
    if schema is not None:
        try:
            text = schema.model_dump_json(indent=2)
            candidate = target_dir / f"{stem}.schema.json"
            candidate.write_text(text, encoding="utf-8")
            schema_path = candidate
            schema_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        except Exception as dump_exc:  # noqa: BLE001 - recorded, not raised
            dump_error = f"{type(dump_exc).__name__}: {dump_exc}"

    report = build_failed_run_report(
        diagnostics, exc,
        schema_path=schema_path, schema_sha256=schema_sha,
        schema_dump_error=dump_error, now=stamp_time,
        source_lineage=source_lineage,
    )
    report_path = target_dir / f"{stem}.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    return report_path
