"""The Unity Step 2 concept has a measured token cap, and a cut-off concept never reaches Step 3.

Before this cap the concept call passed no `max_tokens`, so it inherited the
client-wide `lm_studio.max_tokens` (88,000 in the E2E config). It is the one
call that reasons, so a reasoning loop there stopped only at the context wall.

Test data, and who wrote it:
- `_MEASURED_CONCEPTS` are numbers, not model text: `usage.completion_tokens`
  of every Unity concept reply ("Generated prediction" with reasoning tokens,
  prose content naming Unity) in the LM Studio server logs of 2026-10-04 ..
  2026-11-19, qwen3.5-9b. All 64 finished with finish_reason=stop.
- `_CONCEPT`, the fake client and every reply below are engineer-authored test
  doubles, never Pipeline LLM output.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.llm_budgets import (
    CONCEPT_HEALTHY_MAX_TOKENS,
    CONCEPT_TOKENS,
    ReplyTruncatedError,
    concept_tokens,
    concept_truncated,
)
from src.unity_failed_run import FAILED_RUN_DIRNAME, FailedRunReport
from src.unity_pipeline import UnityPipeline
from src.unity_prompts import UNITY_CONCEPT_SYSTEM, UNITY_SCHEMA_FILL_SYSTEM

_MEASURED_CONCEPTS = (
    2127, 3156, 2233, 2848, 484, 4424, 3890, 3198, 2840, 2684, 2500, 2761, 3234,
    3063, 3905, 3255, 3521, 3167, 3929, 3485, 3317, 3871, 4320, 2943, 2540, 2916,
    3440, 4198, 3133, 3877, 2717, 3388, 3746, 2147, 3518, 3816, 4996, 7230, 6332,
    6279, 8302, 2930, 3719, 2990, 2907, 3814, 2991, 2760, 4204, 3302, 2449, 2682,
    2954, 3982, 2233, 3299, 4063, 3882, 4234, 3248, 4439, 3013, 3314, 4315,
)
_MEASURED_MAX_PROMPT_TOKENS = 2348    # largest prompt_tokens among those replies
_LOADED_CONTEXT = 47360               # `lms ps`, qwen3.5-9b on the E2E machine
_GLOBAL_MAX_TOKENS = 88000            # E2E config lm_studio.max_tokens

_CONCEPT = "A rain-soaked cyberpunk city where a bounty hunter races a hoverbike. PC, 3D."
_PROMPT = "a bounty hunter chases fugitives through a neon city on a hoverbike"


class _ConceptServer:
    """Scripts the concept call; records the cap every call was sent with.

    `concept` is (reply or exception, finish_reason). The schema fill is
    recorded and answered with an error so the run stops right after Step 3
    starts: these tests are about Step 2.
    """

    def __init__(self, client, concept):
        self.client = client
        self.concept = concept
        self.concept_max_tokens: list[int | None] = []
        self.fill_calls = 0

    def __call__(self, *, system: str = "", user: str = "", max_tokens=None, **_kw) -> str:
        if system == UNITY_CONCEPT_SYSTEM:
            self.concept_max_tokens.append(max_tokens)
            reply, finish = self.concept
            self.client.last_finish_reason = finish
            if isinstance(reply, BaseException):
                raise reply
            return reply
        if system == UNITY_SCHEMA_FILL_SYSTEM:
            self.fill_calls += 1
            raise RuntimeError("stop at the fill")
        self.client.last_finish_reason = "stop"
        return ""


def _pipeline(tmp_path: Path, max_tokens: int = _GLOBAL_MAX_TOKENS) -> UnityPipeline:
    return UnityPipeline({
        "lm_studio": {
            "base_url": "http://localhost:1234/v1", "model": "test-model",
            "temperature": 0.7, "max_tokens": max_tokens, "timeout": 30.0,
            "concept_temperature": 0.85, "schema_temperature": 0.2,
        },
        "unity_pipeline": {
            "lookups_dir": "lookups", "unity_template_dir": "templates/unity",
            "unity_output_dir": str(tmp_path),
        },
        "logging": {"show_prompts": False},
    })


def _wire(pipeline: UnityPipeline, concept) -> _ConceptServer:
    server = _ConceptServer(pipeline.llm, concept)
    pipeline.llm.chat = MagicMock(side_effect=server)
    pipeline.generator.write = MagicMock(side_effect=RuntimeError("stop after the fill"))
    return server


def _report(output_dir: Path) -> dict:
    reports = [p for p in (output_dir / FAILED_RUN_DIRNAME).glob("*.json")
               if not p.name.endswith(".schema.json")]
    assert len(reports) == 1, reports
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    FailedRunReport.model_validate(data)
    return data


# ── the cap reaches the call ───────────────────────────────────────────────────

def test_the_concept_call_is_sent_with_the_measured_cap(tmp_path):
    pipeline = _pipeline(tmp_path)
    server = _wire(pipeline, (_CONCEPT, "stop"))
    with pytest.raises(RuntimeError):
        pipeline.run(_PROMPT)
    assert server.concept_max_tokens == [CONCEPT_TOKENS]
    assert server.fill_calls >= 1                  # a healthy concept reaches Step 3
    assert pipeline.run_diagnostics.concept == _CONCEPT


def test_a_config_below_the_budget_keeps_its_own_concept_cap(tmp_path):
    pipeline = _pipeline(tmp_path, max_tokens=8192)
    server = _wire(pipeline, (_CONCEPT, "stop"))
    with pytest.raises(RuntimeError):
        pipeline.run(_PROMPT)
    assert server.concept_max_tokens == [8192]


# ── a cut-off concept is refused, never filled ─────────────────────────────────

def test_a_concept_cut_off_in_its_answer_never_reaches_step_3(tmp_path):
    pipeline = _pipeline(tmp_path)
    server = _wire(pipeline, ("A rain-soaked cyberpunk city where a bounty", "length"))
    with pytest.raises(ReplyTruncatedError, match="Step 2 concept reply cut off at its "
                                                  "12,000-token cap") as excinfo:
        pipeline.run(_PROMPT)
    assert "likely a runaway" in str(excinfo.value)
    assert server.fill_calls == 0
    report = _report(tmp_path)
    assert report["failure"]["category"] == "reply_truncated"
    assert report["phase"]["failed_phase"].startswith("Steps 2-3")


def test_a_concept_cut_off_inside_its_reasoning_is_refused_with_the_runaway_remedy(tmp_path):
    # chat() raises on an empty answer with reasoning; finish_reason=length
    # says the cap did it, so the remedy is "runaway", not "raise max_tokens".
    pipeline = _pipeline(tmp_path)
    spent = ValueError("The model spent its entire token budget reasoning and never "
                       "emitted a final answer. Raise lm_studio.max_tokens in config.yaml")
    server = _wire(pipeline, (spent, "length"))
    with pytest.raises(ReplyTruncatedError) as excinfo:
        pipeline.run(_PROMPT)
    assert excinfo.value.__cause__ is spent
    assert "after 0 chars" in str(excinfo.value)
    assert "Raise lm_studio.max_tokens" not in str(excinfo.value)
    assert server.fill_calls == 0


def test_an_empty_concept_that_was_not_cut_off_keeps_its_own_error(tmp_path):
    pipeline = _pipeline(tmp_path)
    empty = ValueError("LM Studio returned an empty response.")
    _wire(pipeline, (empty, "stop"))
    with pytest.raises(ValueError, match="empty response") as excinfo:
        pipeline.run(_PROMPT)
    assert not isinstance(excinfo.value, ReplyTruncatedError)


# ── the budget is pinned to the measured concepts ──────────────────────────────

def test_the_concept_budget_is_pinned_to_the_measured_replies():
    assert len(_MEASURED_CONCEPTS) == 64
    assert statistics.median(_MEASURED_CONCEPTS) == 3300.5
    assert max(_MEASURED_CONCEPTS) == CONCEPT_HEALTHY_MAX_TOKENS == 8302

    # Changing the budget means re-measuring: update the samples above too.
    assert CONCEPT_TOKENS == 12_000
    assert 1.3 <= CONCEPT_TOKENS / CONCEPT_HEALTHY_MAX_TOKENS <= 2.0
    assert CONCEPT_TOKENS + _MEASURED_MAX_PROMPT_TOKENS < _LOADED_CONTEXT
    assert CONCEPT_TOKENS < _GLOBAL_MAX_TOKENS


@pytest.mark.parametrize("configured,expected", [
    (None, CONCEPT_TOKENS),
    (_GLOBAL_MAX_TOKENS, CONCEPT_TOKENS),     # the global never raises it
    (15_000, CONCEPT_TOKENS),
    (8192, 8192),                             # a smaller config is kept
    (MagicMock(), CONCEPT_TOKENS),            # a test double's attribute
    (True, CONCEPT_TOKENS),
    (0, CONCEPT_TOKENS),
    (-1, CONCEPT_TOKENS),
])
def test_the_smallest_applicable_concept_cap_wins(configured, expected):
    assert concept_tokens(configured=configured) == expected


def test_the_concept_remedy_depends_on_where_the_cap_came_from():
    runaway = concept_truncated(CONCEPT_TOKENS, 0)
    assert isinstance(runaway, ValueError)
    assert "likely a runaway" in str(runaway) and "--no-think" in str(runaway)
    small = concept_truncated(4096, 9_000)
    assert "Raise lm_studio.max_tokens to 12,000" in str(small)
    assert "runaway" not in str(small)
