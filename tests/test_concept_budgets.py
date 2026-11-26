"""The GTA5 and micro Step 2 concepts have measured caps, and a cut-off concept never reaches Step 3.

Until these caps both concept calls passed no `max_tokens`, so they inherited
the client-wide `lm_studio.max_tokens` (88,000 in the E2E config). The concept
is the one call per pipeline that reasons, so a reasoning loop there stopped
only at the context wall.

Test data, and who wrote it:
- `_GTA5_MEASURED` and `_MICRO_MEASURED` are numbers, not model text: the
  `usage.completion_tokens` of every healthy GTA5 / micro concept reply in the
  LM Studio server logs (qwen3.5-9b, finish_reason=stop, chronological). How
  a reply was classified is in src/llm_budgets.py above GTA5_CONCEPT.
- `gta5_concept_cutoff_in_reasoning.prediction.json` is a VERBATIM byte-range
  extraction of LM Studio's own server log (tests/fixtures/README.md). The
  Pipeline LLM wrote its reasoning, and the server itself reported
  `finish_reason: "length"` at the then-configured 4,096 tokens, with no brief
  written (no flag is faked here).
- Every concept, schema and fake client below is an engineer-authored test
  double, never Pipeline LLM output.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.llm_budgets import (
    GTA5_CONCEPT,
    MICRO_CONCEPT,
    ConceptBudget,
    ReplyTruncatedError,
)
from src.micro.npc_dialog import NpcDialogSchema
from src.micro_pipeline import MicroPipeline
from src.pipeline import MissionPipeline
from src.prompts import CONCEPT_SYSTEM as GTA5_CONCEPT_SYSTEM

_FIXTURES = Path(__file__).parent / "fixtures"
_GTA5_CUTOFF = _FIXTURES / "gta5_concept_cutoff_in_reasoning.prediction.json"
_GTA5_CUTOFF_SHA256 = "84b7241050bee9239bfa27e0fd526c86ac1e5aa8414086912bf7da44f15fa803"

_GLOBAL_MAX_TOKENS = 88000    # config.yaml lm_studio.max_tokens on the E2E machine
_LOADED_CONTEXT = 47360       # `lms ps`, qwen3.5-9b on that machine

# GTA5 mission concepts, 2026-05-14 .. 2026-09-13 (largest prompt: 948 tokens).
_GTA5_MEASURED = (
    2702, 2940, 3368, 3665, 3987, 3041, 3278, 3167, 3960, 3298, 3113, 3545,
    3186, 3962, 2215, 3811, 5211, 4290, 4350, 4092, 5668, 4674, 3592, 2680,
    6389, 4160, 4200, 2819, 5887, 4501, 5350, 3901, 6019, 4184, 4308, 5149,
    4729, 5031, 4099, 4503, 4142, 4523, 5592, 6225, 4601, 4581, 5623, 4566,
    5812, 3538, 5016, 5948, 5202, 3693, 4939, 5836, 4248, 5218, 6347, 6002,
)
# Micro concepts, 2026-05-28 .. 2026-10-26 (largest prompt: 602 tokens).
_MICRO_MEASURED = (
    2491, 2764, 2806, 3777, 2134, 3129, 2239, 2667, 3665, 4102, 3751, 3092,
    2113, 2205, 3847, 2041, 2630, 2673, 3328, 2160, 2524, 2790, 2755, 2951,
    3665, 2354, 2767, 2987, 2488, 3939, 2119, 2148, 2629, 1839, 2234, 3813,
    1786, 3180, 2658, 2477, 2605, 2220, 2870, 2575, 3778, 3558, 2773, 4030,
    4424, 5299, 3518, 3816,
)
# (budget, samples, median, largest prompt)
_MEASURED = {
    "gta5": (GTA5_CONCEPT, _GTA5_MEASURED, 4299, 948),
    "micro": (MICRO_CONCEPT, _MICRO_MEASURED, 2765.5, 602),
}

_SPENT = ("The model spent its entire token budget reasoning and never emitted a "
          "final answer. Raise lm_studio.max_tokens in config.yaml, or use a "
          "non-reasoning model for the schema step.")


class _ConceptServer:
    """Scripts the concept call; records the cap every call was sent with.

    `concept` is (reply or exception, finish_reason), set on the client the
    way `LMStudioClient.chat` sets `last_finish_reason`. The concept is the
    call that reasons (`thinking=True`). Every other call is the Step 3 fill,
    answered with `fill` and "stop".
    """

    def __init__(self, client, concept, fill: str = ""):
        self.client = client
        self.concept = concept
        self.fill = fill
        self.concept_max_tokens: list[int | None] = []
        self.fill_users: list[str] = []

    def __call__(self, *, system: str = "", user: str = "", max_tokens=None,
                 thinking=True, **_kw) -> str:
        if thinking:
            self.concept_max_tokens.append(max_tokens)
            reply, finish = self.concept
            self.client.last_finish_reason = finish
            if isinstance(reply, BaseException):
                raise reply
            return reply
        self.fill_users.append(user)
        self.client.last_finish_reason = "stop"
        return self.fill


# ── GTA5 mission concept (src/pipeline.py) ─────────────────────────────────────

_GTA5_PROMPT = "A heist at the docks"


def _gta5(config: dict, max_tokens: int = _GLOBAL_MAX_TOKENS) -> MissionPipeline:
    config = {**config, "lm_studio": {**config["lm_studio"], "max_tokens": max_tokens}}
    pipeline = MissionPipeline(config)
    pipeline.rag.resolve_model = MagicMock(return_value=None)
    pipeline.rag.resolve_audio = MagicMock(return_value=None)
    return pipeline


def _wire_gta5(pipeline: MissionPipeline, concept, fill: str = "") -> _ConceptServer:
    server = _ConceptServer(pipeline.llm, concept, fill)
    pipeline.llm.chat = MagicMock(side_effect=server)
    return server


@pytest.mark.parametrize("finish_reason", ["stop", None])
def test_the_gta5_concept_is_sent_with_its_measured_cap_and_reaches_step_3(
        gta_pipeline_config, gta_concept_text, gta_schema_json, finish_reason):
    # None: a backend that reports no finish_reason keeps today's behaviour.
    pipeline = _gta5(gta_pipeline_config)
    server = _wire_gta5(pipeline, (gta_concept_text, finish_reason), gta_schema_json)

    out = pipeline.run(_GTA5_PROMPT)

    assert out.exists()
    assert server.concept_max_tokens == [GTA5_CONCEPT.tokens]
    (fill_user,) = server.fill_users
    assert gta_concept_text in fill_user        # the whole concept, as written


def test_a_gta5_config_below_the_concept_budget_keeps_its_own_cap(
        gta_pipeline_config, gta_concept_text, gta_schema_json):
    pipeline = _gta5(gta_pipeline_config, max_tokens=4096)
    server = _wire_gta5(pipeline, (gta_concept_text, "stop"), gta_schema_json)

    pipeline.run(_GTA5_PROMPT)

    assert server.concept_max_tokens == [4096]


def test_a_gta5_concept_cut_off_in_its_answer_never_reaches_step_3(
        gta_pipeline_config, tmp_path):
    pipeline = _gta5(gta_pipeline_config)
    server = _wire_gta5(pipeline, ("The crew assembles for a high-stakes extraction at", "length"))

    with pytest.raises(ReplyTruncatedError, match="GTA5 Step 2 mission concept reply cut off "
                                                  "at its 9,500-token cap") as excinfo:
        pipeline.run(_GTA5_PROMPT)

    assert "after 50 chars" in str(excinfo.value)
    assert "likely a runaway" in str(excinfo.value)
    assert server.fill_users == []
    assert list(tmp_path.rglob("*.cs")) == []


def test_a_gta5_concept_cut_off_inside_its_reasoning_is_refused_with_the_runaway_remedy(
        gta_pipeline_config):
    # chat() raises on an empty answer with reasoning; finish_reason=length
    # says the cap did it, so the remedy is "runaway", not "raise max_tokens".
    pipeline = _gta5(gta_pipeline_config)
    spent = ValueError(_SPENT)
    server = _wire_gta5(pipeline, (spent, "length"))

    with pytest.raises(ReplyTruncatedError) as excinfo:
        pipeline.run(_GTA5_PROMPT)

    assert excinfo.value.__cause__ is spent
    assert "after 0 chars" in str(excinfo.value)
    assert "likely a runaway" in str(excinfo.value)
    assert "Raise lm_studio.max_tokens" not in str(excinfo.value)
    assert server.fill_users == []


def test_an_empty_gta5_concept_that_was_not_cut_off_keeps_its_own_error(gta_pipeline_config):
    pipeline = _gta5(gta_pipeline_config)
    empty = ValueError("LM Studio returned an empty response.")
    _wire_gta5(pipeline, (empty, "stop"))

    with pytest.raises(ValueError, match="empty response") as excinfo:
        pipeline.run(_GTA5_PROMPT)

    assert not isinstance(excinfo.value, ReplyTruncatedError)


# ── the captured GTA5 concept cut-off, through the real client ─────────────────

def _captured_cutoff() -> dict:
    return json.loads(_GTA5_CUTOFF.read_text(encoding="utf-8"))


def test_the_gta5_concept_fixture_is_the_verbatim_capture():
    assert hashlib.sha256(_GTA5_CUTOFF.read_bytes()).hexdigest() == _GTA5_CUTOFF_SHA256
    body = _captured_cutoff()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["message"]["content"] == ""
    assert "GTA5 mission writer" in choice["message"]["reasoning_content"][:200]
    usage = body["usage"]
    assert usage["completion_tokens"] == 4096
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 4095
    # The cap that cut it was below what a healthy GTA5 concept needs.
    assert usage["completion_tokens"] < GTA5_CONCEPT.healthy_max


def test_the_captured_gta5_concept_cut_off_is_refused_with_the_config_remedy(
        gta_pipeline_config, tmp_path):
    """Replays the 2026-05-14 capture through the real LMStudioClient.

    The config of that day (lm_studio.max_tokens: 4096) is the cap. The client
    raises on the empty answer, and the pipeline turns that into a refusal
    whose remedy is the right one: raise the config to the measured budget.
    """
    pipeline = _gta5(gta_pipeline_config, max_tokens=4096)
    posts: list[dict] = []

    def fake_post(url, json=None, **_kw):
        assert url.endswith("/chat/completions")
        posts.append(json)
        response = MagicMock()
        response.json.return_value = _captured_cutoff()
        return response

    with patch("httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.post.side_effect = fake_post
        with pytest.raises(ReplyTruncatedError) as excinfo:
            pipeline.run(_GTA5_PROMPT)

    (payload,) = posts                           # the fill was never requested
    assert payload["messages"][0]["content"] == GTA5_CONCEPT_SYSTEM
    assert payload["max_tokens"] == 4096
    message = str(excinfo.value)
    assert message.startswith("GTA5 Step 2 mission concept reply cut off at its "
                              "4,096-token cap (finish_reason=length) after 0 chars")
    assert "Raise lm_studio.max_tokens to 9,500" in message
    assert "runaway" not in message
    assert "spent its entire token budget reasoning" in str(excinfo.value.__cause__)
    assert list(tmp_path.rglob("*.cs")) == []


# ── micro concept (MicroPipeline.run, every standalone micro) ──────────────────

class _ProbeMicro(MicroPipeline):
    """The shared micro run, with the real NpcDialogSchema and no writer."""

    kind = "npc_dialog"
    display_name = "Probe"
    engine = "unity"
    schema_cls = NpcDialogSchema
    variants = ["ambient_bark", "quest_giver"]
    concept_prompt = "{user_prompt}{theme_block}{variant}"
    schema_prompt = "CONCEPT<{concept}>{schema_hints}{variant}"

    def write_files(self, schema, variant, out_dir):  # noqa: ARG002
        self.written = schema
        return 0


_MICRO_CONCEPT_TEXT = "An announcer barks race commentary from the grandstand speakers."


def _micro(max_tokens: int = _GLOBAL_MAX_TOKENS) -> _ProbeMicro:
    return _ProbeMicro({"lm_studio": {"base_url": "http://localhost:1234/v1",
                                      "model": "test", "max_tokens": max_tokens}})


def _healthy_dialog() -> str:
    return json.dumps({"name": "RaceAnnouncer", "variant": "ambient_bark",
                       "speaker_name": "RaceAnnouncer"})


def _wire_micro(micro: _ProbeMicro, concept) -> _ConceptServer:
    server = _ConceptServer(micro.llm, concept, _healthy_dialog())
    micro.llm.chat = MagicMock(side_effect=server)
    return server


def _run_micro(micro: _ProbeMicro, tmp_path: Path):
    return micro.run("A race announcer barks commentary.", variant="ambient_bark",
                     theme="none", out=str(tmp_path / "out"))


@pytest.mark.parametrize("finish_reason", ["stop", None])
def test_the_micro_concept_is_sent_with_its_measured_cap_and_reaches_step_3(
        tmp_path, finish_reason):
    micro = _micro()
    server = _wire_micro(micro, (_MICRO_CONCEPT_TEXT, finish_reason))

    _run_micro(micro, tmp_path)

    assert server.concept_max_tokens == [MICRO_CONCEPT.tokens]
    assert server.fill_users == [f"CONCEPT<{_MICRO_CONCEPT_TEXT}>ambient_bark"]
    assert micro.written.name == "RaceAnnouncer"


def test_a_micro_config_below_the_concept_budget_keeps_its_own_cap(tmp_path):
    micro = _micro(max_tokens=4096)
    server = _wire_micro(micro, (_MICRO_CONCEPT_TEXT, "stop"))

    _run_micro(micro, tmp_path)

    assert server.concept_max_tokens == [4096]


def test_a_micro_concept_cut_off_in_its_answer_never_reaches_step_3(tmp_path):
    micro = _micro()
    server = _wire_micro(micro, ("An announcer barks race", "length"))

    with pytest.raises(ReplyTruncatedError, match="micro Step 2 concept reply cut off at "
                                                  "its 8,000-token cap") as excinfo:
        _run_micro(micro, tmp_path)

    assert "after 23 chars" in str(excinfo.value)
    assert "likely a runaway" in str(excinfo.value)
    assert server.fill_users == []
    assert not (tmp_path / "out").exists()      # nothing written


def test_a_micro_concept_cut_off_inside_its_reasoning_is_refused_with_the_runaway_remedy(tmp_path):
    micro = _micro()
    spent = ValueError(_SPENT)
    server = _wire_micro(micro, (spent, "length"))

    with pytest.raises(ReplyTruncatedError) as excinfo:
        _run_micro(micro, tmp_path)

    assert excinfo.value.__cause__ is spent
    assert "after 0 chars" in str(excinfo.value)
    assert "Raise lm_studio.max_tokens" not in str(excinfo.value)
    assert server.fill_users == []


def test_a_micro_concept_cut_off_by_a_small_config_cap_names_the_config_remedy(tmp_path):
    micro = _micro(max_tokens=2000)
    _wire_micro(micro, (ValueError(_SPENT), "length"))

    with pytest.raises(ReplyTruncatedError) as excinfo:
        _run_micro(micro, tmp_path)

    assert "2,000-token cap" in str(excinfo.value)
    assert "Raise lm_studio.max_tokens to 8,000" in str(excinfo.value)


def test_an_empty_micro_concept_that_was_not_cut_off_keeps_its_own_error(tmp_path):
    micro = _micro()
    _wire_micro(micro, (ValueError("LM Studio returned an empty response."), "stop"))

    with pytest.raises(ValueError, match="empty response") as excinfo:
        _run_micro(micro, tmp_path)

    assert not isinstance(excinfo.value, ReplyTruncatedError)


# ── the budgets are pinned to the measured concepts ────────────────────────────

@pytest.mark.parametrize("pipeline", sorted(_MEASURED))
def test_each_concept_budget_is_about_1_5x_its_healthy_max_and_fits_the_context(pipeline):
    budget, samples, median, max_prompt = _MEASURED[pipeline]
    assert len(samples) >= 20, "too small a sample to size a budget from"
    assert statistics.median(samples) == median
    assert max(samples) == budget.healthy_max
    # Headroom over the longest healthy concept, and over a typical one.
    assert 1.3 <= budget.tokens / budget.healthy_max <= 2.0, pipeline
    assert budget.tokens / median >= 2.0, pipeline
    # The cap must be reached before the context wall, or it stops nothing.
    assert budget.tokens + max_prompt < _LOADED_CONTEXT
    assert budget.tokens < _GLOBAL_MAX_TOKENS


def test_the_concept_budget_values_are_pinned():
    # Changing one means re-measuring: update the samples above too.
    assert len(_GTA5_MEASURED) == 60 and len(_MICRO_MEASURED) == 52
    assert (GTA5_CONCEPT.tokens, MICRO_CONCEPT.tokens) == (9_500, 8_000)
    assert (GTA5_CONCEPT.healthy_max, MICRO_CONCEPT.healthy_max) == (6_389, 5_299)


@pytest.mark.parametrize("configured,expected", [
    (None, 9_500),
    (_GLOBAL_MAX_TOKENS, 9_500),           # the global never raises it
    (12_000, 9_500),
    (4096, 4096),                          # a smaller config is kept
    (MagicMock(), 9_500),                  # a test double's attribute
    (True, 9_500),
    (0, 9_500),
    (-1, 9_500),
])
def test_the_smallest_applicable_concept_cap_wins(configured, expected):
    assert GTA5_CONCEPT.cap(configured=configured) == expected


@pytest.mark.parametrize("budget", [GTA5_CONCEPT, MICRO_CONCEPT], ids=lambda b: b.purpose)
def test_the_concept_remedy_depends_on_where_the_cap_came_from(budget: ConceptBudget):
    runaway = budget.truncated(budget.tokens, 0)
    assert isinstance(runaway, ValueError)
    assert str(runaway).startswith(f"{budget.purpose} reply cut off at its {budget.tokens:,}-token cap")
    assert "after 0 chars" in str(runaway) and "likely a runaway" in str(runaway)
    assert f"at most {budget.healthy_max:,} tokens, reasoning included" in str(runaway)
    assert "re-measured in src/llm_budgets.py" in str(runaway)

    small = budget.truncated(4096, 1_234)
    assert "1,234 chars" in str(small)
    assert f"Raise lm_studio.max_tokens to {budget.tokens:,}" in str(small)
    assert "runaway" not in str(small)
