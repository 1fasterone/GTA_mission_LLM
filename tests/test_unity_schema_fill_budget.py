"""The Unity Step 3 schema fill has a measured token cap, and a cut-off reply is never repaired.

Regression for neon E2E batch run 03 (2026-11-17). The fill inherited
`lm_studio.max_tokens: 88000`, which is above LM Studio's 47,360-token loaded
context, so nothing below the context wall could stop it. It looped for 21
minutes until it was killed by hand. A reply cut off like that would then reach
`extract_json`, whose bracket-closing repair turns it into JSON that validates
as a schema with no entities and no missions.

Test data, and who wrote it:
- `tests/fixtures/neon_fugitive_run03_runaway_fill.prediction.json` is a
  VERBATIM byte-range extraction of LM Studio's log of that reply. The
  Pipeline LLM wrote its `content`. LM Studio logged the killed generation as
  `finish_reason: "stop"`, so the `length` flag in these tests is set by the
  fake client, the same way the Step 3.5 budget tests set it.
- `_healthy_fill()` and every fake client below are engineer-authored test
  doubles, never Pipeline LLM output.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.llm_budgets import (
    SCHEMA_FILL_HEALTHY_MAX_TOKENS,
    SCHEMA_FILL_TOKENS,
    VISUAL_COMPILE_FILL_TOKENS,
    ReplyTruncatedError,
    schema_fill_tokens,
    schema_fill_truncated,
)
from src.lm_studio_client import LMStudioClient
from src.unity_failed_run import FAILED_RUN_DIRNAME, FailedRunReport
from src.unity_pipeline import UnityPipeline
from src.unity_prompts import UNITY_CONCEPT_SYSTEM, UNITY_SCHEMA_FILL_SYSTEM
from src.unity_schema import UnitySchema
from src.unity_schema_clamp import clamp_capped_lists

_FIXTURE = (Path(__file__).parent / "fixtures"
            / "neon_fugitive_run03_runaway_fill.prediction.json")
# Recorded in tests/fixtures/README.md. A different hash means the capture was
# edited or line-ending-converted, and this file no longer tests the live reply.
_FIXTURE_SHA256 = "a006cb450fa759682ad9a6f78aaa25a48d012b0697a9a9c8f22d6a26a28bbcb9"

_CONCEPT = "A rain-soaked cyberpunk city where a bounty hunter races a hoverbike. PC, 3D."
_PROMPT = "a bounty hunter chases fugitives through a neon city on a hoverbike"

# Unity Step 3 fills in the LM Studio server logs of 2026-11-14 and 2026-11-17:
# usage.completion_tokens of every "Generated prediction" whose reply is a
# UnitySchema fill, paired with its "Running chat completion" line. All 13
# finished with finish_reason=stop, in 4.4-6.6 minutes each.
_MEASURED_HEALTHY_FILLS = (8026, 7993, 8918, 7766, 6816, 7415, 10067,
                           7865, 9487, 7123, 7062, 8206, 8046)
_MEASURED_MAX_PROMPT_TOKENS = 9948    # largest prompt_tokens among those fills
_LOADED_CONTEXT = 47360               # `lms ps`, qwen3.5-9b on the E2E machine
_GLOBAL_MAX_TOKENS = 88000            # config.yaml lm_studio.max_tokens there


def _runaway_reply() -> str:
    prediction = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return prediction["choices"][0]["message"]["content"]


def _healthy_fill() -> str:
    return json.dumps({
        "project_name": "NeonBounty", "company_name": "TestCo",
        "game_type": "3D", "platform": "PC", "render_pipeline": "URP",
        "unity_version": "6000.0", "color_space": "Linear",
        "description": "A bounty hunter on a hoverbike",
        "features": ["input_system"], "scenes": ["Main", "Menu"],
        "scripts": [
            {"name": "GameManager", "type": "game_manager", "description": "GM"},
            {"name": "PlayerController", "type": "player", "description": "Riding"},
        ],
        "packages": [{"description": "universal render pipeline"}],
    })


class _FillServer:
    """Plays a scripted sequence of (reply, finish_reason) for the Step 3 fill.

    It sets `last_finish_reason` on the client the way `LMStudioClient.chat`
    does. Every other call (concept, Step 3.5) gets an ordinary reply that
    finished with "stop".
    """

    def __init__(self, client: LMStudioClient, *fills: tuple[str, str | None]):
        self.client = client
        self.fills = list(fills)
        self.fill_max_tokens: list[int | None] = []

    def __call__(self, *, system: str = "", user: str = "", max_tokens=None, **_kw) -> str:
        if system == UNITY_SCHEMA_FILL_SYSTEM:
            self.fill_max_tokens.append(max_tokens)
            reply, finish = self.fills.pop(0)
            self.client.last_finish_reason = finish
            return reply
        self.client.last_finish_reason = "stop"
        return _CONCEPT if system == UNITY_CONCEPT_SYSTEM else ""


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


def _wire(pipeline: UnityPipeline, *fills: tuple[str, str | None]) -> tuple[_FillServer, list[str]]:
    """Script the fill and record every text that reaches `extract_json`."""
    server = _FillServer(pipeline.llm, *fills)
    pipeline.llm.chat = MagicMock(side_effect=server)
    parsed: list[str] = []
    real_extract = pipeline.llm.extract_json

    def _spy(text: str) -> dict:
        parsed.append(text)
        return real_extract(text)

    pipeline.llm.extract_json = _spy
    # Stop the run once Steps 4-6 are reached: this test is about Step 3.
    pipeline.generator.write = MagicMock(side_effect=RuntimeError("stop after the fill"))
    return server, parsed


def _report(output_dir: Path) -> dict:
    reports = [p for p in (output_dir / FAILED_RUN_DIRNAME).glob("*.json")
               if not p.name.endswith(".schema.json")]
    assert len(reports) == 1, reports
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    FailedRunReport.model_validate(data)
    return data


# ── the live capture ───────────────────────────────────────────────────────────

def test_the_fixture_is_the_verbatim_capture():
    assert hashlib.sha256(_FIXTURE.read_bytes()).hexdigest() == _FIXTURE_SHA256
    reply = _runaway_reply()
    assert len(reply) == 118_392
    assert '"missions"' not in reply        # the loop never got that far


def test_without_the_guard_the_runaway_reply_validates_as_an_empty_game():
    """Precondition for the refusal tests: the hole is real, not hypothetical.

    `extract_json` bracket-closes the cut-off reply. The clamp collapses the
    looped `scripts` list. The result validates as a UnitySchema with no
    entities and no missions. If this ever stops validating, the refusal tests
    still hold, but they no longer prove that the refusal matters.
    """
    client = LMStudioClient("http://localhost:1234/v1")
    client.last_finish_reason = "length"
    repaired = client.extract_json(_runaway_reply())

    *written, closed = repaired["scripts"]
    assert closed == {}           # the repair closed the entry the cut opened
    names = Counter(script["name"] for script in written)
    assert sum(names.values()) == 235 and len(names) == 42
    assert sorted(name for name, n in names.items() if n > 20) == [
        "InputManager", "ObjectiveManager", "PhysicsManager", "RulesEngineManager",
        "SpriteAnimatorManager", "StateMachineManager", "TransitionManager",
        "TriggerManager", "WaypointManager"]

    clamp_capped_lists(repaired)
    schema = UnitySchema.model_validate(repaired)
    assert schema.project_name == "NeonFugitive"
    assert schema.entities == [] and schema.missions == []


# ── (a) a reply cut off at the cap is rejected and retried, never repaired ─────

def test_a_cut_off_fill_is_a_failed_attempt_and_the_colder_retry_runs(tmp_path):
    runaway = _runaway_reply()
    pipeline = _pipeline(tmp_path)
    server, parsed = _wire(pipeline, (runaway, "length"), (_healthy_fill(), "stop"))

    with pytest.raises(RuntimeError, match="stop after the fill"):
        pipeline.run(_PROMPT)

    assert server.fill_max_tokens == [SCHEMA_FILL_TOKENS, SCHEMA_FILL_TOKENS]
    assert runaway not in parsed                       # never parsed or repaired
    assert parsed == [_healthy_fill()]

    diagnostics = pipeline.run_diagnostics
    first, second = diagnostics.fill_attempts
    assert first["error_type"] == "ReplyTruncatedError"
    assert first["raw_response"] == runaway            # kept for diagnosis
    assert "30,000-token cap" in first["error"] and "118,392 chars" in first["error"]
    assert second["error"] is None
    assert diagnostics.schema_source == "schema_llm"
    assert diagnostics.schema.project_name == "NeonBounty"

    cut_off = [w for w in diagnostics.warnings if w.startswith("schema fill attempt 1:")]
    assert len(cut_off) == 1
    assert "likely a runaway" in cut_off[0] and cut_off[0].endswith("Retrying colder.")


def test_two_cut_off_fills_fail_loudly_with_nothing_parsed(tmp_path):
    runaway = _runaway_reply()
    pipeline = _pipeline(tmp_path)
    server, parsed = _wire(pipeline, (runaway, "length"), (runaway, "length"))

    with pytest.raises(RuntimeError, match="schema fill failed after 2 attempts") as excinfo:
        pipeline.run(_PROMPT)

    assert isinstance(excinfo.value.__cause__, ReplyTruncatedError)
    assert parsed == []
    report = _report(tmp_path)
    assert report["failure"]["category"] == "schema_fill"
    assert "ReplyTruncatedError" in report["failure"]["remedy"]
    assert [a["error_type"] for a in report["fill_attempts"]] == [
        "ReplyTruncatedError", "ReplyTruncatedError"]
    assert report["schema_artifact"]["available"] is False
    # Every cut-off attempt warns, the last one included, and only the first
    # announces a retry.
    warned = [w for w in report["warnings"] if "cut off at its" in w]
    assert len(warned) == 2
    assert warned[0].endswith("Retrying colder.")
    assert not warned[1].endswith("Retrying colder.")


# ── (b) a healthy reply passes unchanged ───────────────────────────────────────

@pytest.mark.parametrize("finish_reason", ["stop", None])
def test_a_healthy_fill_reaches_the_parser_byte_for_byte(tmp_path, finish_reason):
    # None: a backend that reports no finish_reason keeps today's behaviour.
    reply = _healthy_fill()
    pipeline = _pipeline(tmp_path)
    server, parsed = _wire(pipeline, (reply, finish_reason))

    with pytest.raises(RuntimeError, match="stop after the fill"):
        pipeline.run(_PROMPT)

    assert server.fill_max_tokens == [SCHEMA_FILL_TOKENS]
    assert parsed == [reply]
    (attempt,) = pipeline.run_diagnostics.fill_attempts
    assert attempt["raw_response"] == reply and attempt["error"] is None
    assert not any("cut off at its" in w for w in pipeline.run_diagnostics.warnings)


def test_a_config_below_the_budget_keeps_its_own_cap(tmp_path):
    pipeline = _pipeline(tmp_path, max_tokens=8192)
    server, _parsed = _wire(pipeline, (_healthy_fill(), "stop"))

    with pytest.raises(RuntimeError, match="stop after the fill"):
        pipeline.run(_PROMPT)

    assert server.fill_max_tokens == [8192]


# ── (c) the budget is pinned to the measured fills ─────────────────────────────

def test_the_fill_budget_is_pinned_to_the_measured_healthy_fills():
    assert len(_MEASURED_HEALTHY_FILLS) == 13
    assert statistics.median(_MEASURED_HEALTHY_FILLS) == 7993
    assert max(_MEASURED_HEALTHY_FILLS) == 10067
    # Those 13 are a subset of every healthy Unity fill in the logs since 2025-07
    # (n=330, max 10,661). That wider max is the one the budget is sized from,
    # and tests/test_schema_fill_budgets.py pins it.
    assert max(_MEASURED_HEALTHY_FILLS) <= SCHEMA_FILL_HEALTHY_MAX_TOKENS == 10661

    # Changing the budget means re-measuring: update the samples above too.
    assert SCHEMA_FILL_TOKENS == 30_000
    ratio = SCHEMA_FILL_TOKENS / SCHEMA_FILL_HEALTHY_MAX_TOKENS
    assert 2.5 <= ratio <= 3.5, ratio
    # The cap must be reached before the context wall, or it stops nothing.
    assert SCHEMA_FILL_TOKENS + _MEASURED_MAX_PROMPT_TOKENS < _LOADED_CONTEXT
    assert SCHEMA_FILL_TOKENS < _GLOBAL_MAX_TOKENS


@pytest.mark.parametrize("visual_compile,configured,expected", [
    (False, None, SCHEMA_FILL_TOKENS),
    (False, _GLOBAL_MAX_TOKENS, SCHEMA_FILL_TOKENS),   # the global never raises it
    (False, 8192, 8192),                               # a smaller config is kept
    (True, None, min(SCHEMA_FILL_TOKENS, VISUAL_COMPILE_FILL_TOKENS)),
    (True, _GLOBAL_MAX_TOKENS, min(SCHEMA_FILL_TOKENS, VISUAL_COMPILE_FILL_TOKENS)),
    (False, MagicMock(), SCHEMA_FILL_TOKENS),          # a test double's attribute
    (False, True, SCHEMA_FILL_TOKENS),
    (False, 0, SCHEMA_FILL_TOKENS),
    (False, -1, SCHEMA_FILL_TOKENS),
])
def test_the_smallest_applicable_cap_wins(visual_compile, configured, expected):
    assert schema_fill_tokens(visual_compile=visual_compile, configured=configured) == expected


def test_the_truncation_remedy_depends_on_where_the_cap_came_from():
    runaway = schema_fill_truncated(SCHEMA_FILL_TOKENS, 118_392)
    assert isinstance(runaway, ValueError)
    assert "30,000-token cap" in str(runaway) and "118,392 chars" in str(runaway)
    assert "likely a runaway" in str(runaway)
    assert "only lengthens the loop" in str(runaway)

    small = schema_fill_truncated(8192, 20_000)
    assert "Raise lm_studio.max_tokens to 30,000" in str(small)
    assert "runaway" not in str(small)


def test_extract_json_no_longer_prescribes_a_bigger_cap_for_a_confirmed_cut_off():
    client = LMStudioClient("http://localhost:1234/v1")
    client.last_finish_reason = "length"
    with pytest.raises(ValueError, match="CONFIRMED") as excinfo:
        client.extract_json('{"a": tru')     # unrepairable, so the error fires
    message = str(excinfo.value)
    assert "Raise lm_studio.max_tokens" not in message
    assert "runaway" in message
