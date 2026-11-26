"""The GTA5 mission fill and the micro fill have measured caps, and a cut-off reply is never repaired.

The same rule as the Unity Step 3 fill (tests/test_unity_schema_fill_budget.py),
applied to the other two Step 3 schema fills. Until now both sent no
`max_tokens`, so they inherited `lm_studio.max_tokens` (88,000), and both
handed a cut-off reply to `extract_json`, whose bracket-closing repair turns
it into JSON that validates.

That repair has happened on real replies. These fixtures are VERBATIM
byte-range extractions of LM Studio's own server log. The Pipeline LLM wrote
their `content`, and the server itself reported `finish_reason: "length"` (no
flag is faked here):
- `gta5_lighthouse_cutoff_fill.prediction.json` (2026-07-12) validates as a
  `MissionSchema` whose `vehicles` list was cut mid-entry, with no `objects`.
- `micro_npc_dialog_cutoff_fill.prediction.json` (2026-07-08) validates as an
  `NpcDialogSchema` holding only the lines written before the cut.
Both spent most of their 8,192 tokens reasoning, before #109 made fills send
`reasoning_effort=none`.

The healthy replies and fake clients below are engineer-authored test doubles,
never Pipeline LLM output.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.llm_budgets import (
    GTA5_FILL,
    MICRO_FILL,
    UNITY_FILL,
    FillBudget,
    ReplyTruncatedError,
)
from src.lm_studio_client import LMStudioClient
from src.micro.npc_dialog import NpcDialogSchema
from src.micro_pipeline import MicroPipeline
from src.mission_schema import MissionSchema
from src.pipeline import MissionPipeline
from src.prompts import SCHEMA_FILL_SYSTEM as GTA5_SCHEMA_FILL_SYSTEM

_FIXTURES = Path(__file__).parent / "fixtures"
_GTA5_CUTOFF = _FIXTURES / "gta5_lighthouse_cutoff_fill.prediction.json"
_MICRO_CUTOFF = _FIXTURES / "micro_npc_dialog_cutoff_fill.prediction.json"
# Recorded in tests/fixtures/README.md.
_SHA256 = {
    _GTA5_CUTOFF: "d777fd4f038932f380323885723f60746463c79d502bad9790469fbe0da13625",
    _MICRO_CUTOFF: "7c759f1787629909995a3c4bf438abbca447eb0d3cb727ba1a48685d87bce6cc",
}

_GLOBAL_MAX_TOKENS = 88000    # config.yaml lm_studio.max_tokens on the E2E machine
_LOADED_CONTEXT = 47360       # `lms ps`, qwen3.5-9b on that machine

# Healthy fills in every LM Studio server log from 2025-07 to 2026-11-17: each
# "Generated prediction" paired with its "Running chat completion", classified
# by top-level keys, counted when finish_reason=stop with at most 1 reasoning
# token. (n, median, max, largest prompt).
_MEASURED = {
    "unity": (UNITY_FILL, 330, 3666.5, 10661, 11484),
    "gta5": (GTA5_FILL, 53, 608, 1212, 4876),
    "micro": (MICRO_FILL, 142, 373, 1481, 2402),
}


def _prediction(path: Path) -> tuple[str, str]:
    """(content, finish_reason) of a captured LM Studio prediction."""
    choice = json.loads(path.read_text(encoding="utf-8"))["choices"][0]
    return choice["message"]["content"], choice["finish_reason"]


class _FillServer:
    """Plays a scripted sequence of (reply, finish_reason) for one Step 3 fill.

    It sets `last_finish_reason` on the client the way `LMStudioClient.chat`
    does. Every other call (the concept) gets `concept`, finished with "stop".
    """

    def __init__(self, client, fill_system: str | None, concept: str, *fills):
        self.client = client
        self.fill_system = fill_system
        self.concept = concept
        self.fills = list(fills)
        self.fill_max_tokens: list[int | None] = []

    def __call__(self, *, system: str = "", max_tokens=None, thinking=True, **_kw) -> str:
        is_fill = (system == self.fill_system) if self.fill_system else not thinking
        if is_fill:
            self.fill_max_tokens.append(max_tokens)
            reply, finish = self.fills.pop(0)
            self.client.last_finish_reason = finish
            return reply
        self.client.last_finish_reason = "stop"
        return self.concept


def _wire(pipeline, server: _FillServer) -> tuple[list[str], list[str]]:
    """Script the LLM; record what reaches `extract_json` and every warning."""
    pipeline.llm.chat = MagicMock(side_effect=server)
    parsed: list[str] = []
    real_extract = pipeline.llm.extract_json

    def _spy(text: str) -> dict:
        parsed.append(text)
        return real_extract(text)

    pipeline.llm.extract_json = _spy
    warned: list[str] = []
    pipeline._warn = warned.append
    return parsed, warned


# ── GTA5 mission fill ──────────────────────────────────────────────────────────

def _gta5(config: dict, max_tokens: int = _GLOBAL_MAX_TOKENS) -> MissionPipeline:
    config = {**config, "lm_studio": {**config["lm_studio"], "max_tokens": max_tokens}}
    pipeline = MissionPipeline(config)
    pipeline.rag.resolve_model = MagicMock(return_value=None)
    pipeline.rag.resolve_audio = MagicMock(return_value=None)
    return pipeline


def test_the_gta5_fixture_is_the_verbatim_capture():
    assert hashlib.sha256(_GTA5_CUTOFF.read_bytes()).hexdigest() == _SHA256[_GTA5_CUTOFF]
    content, finish = _prediction(_GTA5_CUTOFF)
    assert finish == "length" and len(content) == 1413


def test_without_the_guard_the_cut_off_gta5_reply_validates_as_a_mission():
    """Precondition: the repair really turns this reply into a mission."""
    client = LMStudioClient("http://localhost:1234/v1")
    content, client.last_finish_reason = _prediction(_GTA5_CUTOFF)
    repaired = client.extract_json(content)
    assert list(repaired) == ["mission_name", "location_name", "time_of_day",
                              "weather", "steps", "peds", "vehicles"]
    mission = MissionSchema.model_validate(repaired)
    assert mission.mission_name == "Lighthouse Perimeter Assault"
    assert len(mission.vehicles) == 2 and mission.objects == []


def test_a_cut_off_gta5_fill_is_refused_and_the_colder_retry_runs(
        gta_pipeline_config, gta_concept_text, gta_schema_json):
    cut_off, finish = _prediction(_GTA5_CUTOFF)
    pipeline = _gta5(gta_pipeline_config)
    server = _FillServer(pipeline.llm, GTA5_SCHEMA_FILL_SYSTEM, gta_concept_text,
                         (cut_off, finish), (gta_schema_json, "stop"))
    parsed, warned = _wire(pipeline, server)

    out = pipeline.run("A heist at the docks")

    assert out.exists()
    assert server.fill_max_tokens == [GTA5_FILL.tokens, GTA5_FILL.tokens]
    assert parsed == [gta_schema_json]          # the cut-off reply never parsed
    (warning,) = [w for w in warned if "cut off at its" in w]
    assert warning.startswith("schema fill attempt 1: GTA5 Step 3 mission fill reply")
    assert "3,600-token cap" in warning and "likely a runaway" in warning
    assert warning.endswith("Retrying colder.")


def test_two_cut_off_gta5_fills_fail_loudly_with_nothing_parsed(
        gta_pipeline_config, gta_concept_text):
    cut_off, finish = _prediction(_GTA5_CUTOFF)
    pipeline = _gta5(gta_pipeline_config)
    server = _FillServer(pipeline.llm, GTA5_SCHEMA_FILL_SYSTEM, gta_concept_text,
                         (cut_off, finish), (cut_off, finish))
    parsed, warned = _wire(pipeline, server)

    with pytest.raises(RuntimeError, match="schema fill failed after 2 attempts") as excinfo:
        pipeline.run("A heist at the docks")

    assert isinstance(excinfo.value.__cause__, ReplyTruncatedError)
    assert parsed == []
    cut = [w for w in warned if "cut off at its" in w]
    assert len(cut) == 2
    assert cut[0].endswith("Retrying colder.") and not cut[1].endswith("Retrying colder.")


@pytest.mark.parametrize("finish_reason", ["stop", None])
def test_a_healthy_gta5_fill_reaches_the_parser_byte_for_byte(
        gta_pipeline_config, gta_concept_text, gta_schema_json, finish_reason):
    # None: a backend that reports no finish_reason keeps today's behaviour.
    pipeline = _gta5(gta_pipeline_config)
    server = _FillServer(pipeline.llm, GTA5_SCHEMA_FILL_SYSTEM, gta_concept_text,
                         (gta_schema_json, finish_reason))
    parsed, warned = _wire(pipeline, server)

    pipeline.run("A heist at the docks")

    assert server.fill_max_tokens == [GTA5_FILL.tokens]
    assert parsed == [gta_schema_json]
    assert not any("cut off at its" in w for w in warned)


def test_a_gta5_config_below_the_budget_keeps_its_own_cap(
        gta_pipeline_config, gta_concept_text, gta_schema_json):
    pipeline = _gta5(gta_pipeline_config, max_tokens=2048)
    server = _FillServer(pipeline.llm, GTA5_SCHEMA_FILL_SYSTEM, gta_concept_text,
                         (gta_schema_json, "stop"))
    _wire(pipeline, server)

    pipeline.run("A heist at the docks")

    assert server.fill_max_tokens == [2048]


# ── micro fill (MicroPipeline.run, inherited by every micro) ───────────────────

class _DialogMicro(MicroPipeline):
    """The shared micro fill loop, with the real NpcDialogSchema and no writer."""

    kind = "npc_dialog"
    display_name = "Probe"
    engine = "unity"
    schema_cls = NpcDialogSchema
    variants = ["ambient_bark", "quest_giver"]
    concept_prompt = "{user_prompt}{theme_block}{variant}"
    schema_prompt = "{concept}{schema_hints}{variant}"

    def write_files(self, schema, variant, out_dir):  # noqa: ARG002
        self.written = schema
        return 0


def _micro(max_tokens: int = _GLOBAL_MAX_TOKENS) -> _DialogMicro:
    return _DialogMicro({"lm_studio": {"base_url": "http://localhost:1234/v1",
                                       "model": "test", "max_tokens": max_tokens}})


def _healthy_dialog() -> str:
    return json.dumps({"name": "RaceAnnouncer", "variant": "ambient_bark",
                       "speaker_name": "RaceAnnouncer"})


def _run_micro(micro: _DialogMicro, tmp_path: Path):
    return micro.run("A race announcer barks commentary.", variant="ambient_bark",
                     theme="none", out=str(tmp_path / "out"))


def test_the_micro_fixture_is_the_verbatim_capture():
    assert hashlib.sha256(_MICRO_CUTOFF.read_bytes()).hexdigest() == _SHA256[_MICRO_CUTOFF]
    content, finish = _prediction(_MICRO_CUTOFF)
    assert finish == "length" and len(content) == 989


def test_without_the_guard_the_cut_off_micro_reply_validates_as_a_dialog():
    """Precondition: the repair really turns this reply into a dialog micro."""
    client = LMStudioClient("http://localhost:1234/v1")
    content, client.last_finish_reason = _prediction(_MICRO_CUTOFF)
    dialog = NpcDialogSchema.model_validate(client.extract_json(content))
    assert dialog.name == "RaceAnnouncer" and dialog.variant == "ambient_bark"
    assert len(dialog.lines) == 3


def test_a_cut_off_micro_fill_is_refused_and_the_colder_retry_runs(tmp_path):
    cut_off, finish = _prediction(_MICRO_CUTOFF)
    micro = _micro()
    server = _FillServer(micro.llm, None, "An announcer.",
                         (cut_off, finish), (_healthy_dialog(), "stop"))
    parsed, warned = _wire(micro, server)

    _run_micro(micro, tmp_path)

    assert server.fill_max_tokens == [MICRO_FILL.tokens, MICRO_FILL.tokens]
    assert parsed == [_healthy_dialog()]        # the cut-off reply never parsed
    assert micro.written.lines == []            # the cut-off lines never shipped
    (warning,) = [w for w in warned if "cut off at its" in w]
    assert warning.startswith("schema fill attempt 1: micro Step 3 fill reply")
    assert "4,500-token cap" in warning and warning.endswith("Retrying colder.")


def test_two_cut_off_micro_fills_fail_loudly_with_nothing_parsed(tmp_path):
    cut_off, finish = _prediction(_MICRO_CUTOFF)
    micro = _micro()
    server = _FillServer(micro.llm, None, "An announcer.",
                         (cut_off, finish), (cut_off, finish))
    parsed, warned = _wire(micro, server)

    with pytest.raises(RuntimeError, match="schema fill failed after 2 attempts") as excinfo:
        _run_micro(micro, tmp_path)

    assert isinstance(excinfo.value.__cause__, ReplyTruncatedError)
    assert parsed == []
    assert not (tmp_path / "out").exists()      # nothing written
    cut = [w for w in warned if "cut off at its" in w]
    assert len(cut) == 2 and not cut[1].endswith("Retrying colder.")


@pytest.mark.parametrize("finish_reason", ["stop", None])
def test_a_healthy_micro_fill_reaches_the_parser_byte_for_byte(tmp_path, finish_reason):
    micro = _micro()
    server = _FillServer(micro.llm, None, "An announcer.", (_healthy_dialog(), finish_reason))
    parsed, warned = _wire(micro, server)

    _run_micro(micro, tmp_path)

    assert server.fill_max_tokens == [MICRO_FILL.tokens]
    assert parsed == [_healthy_dialog()]
    assert micro.written.name == "RaceAnnouncer"
    assert not any("cut off at its" in w for w in warned)


def test_a_micro_config_below_the_budget_keeps_its_own_cap(tmp_path):
    micro = _micro(max_tokens=1024)
    server = _FillServer(micro.llm, None, "An announcer.", (_healthy_dialog(), "stop"))
    _wire(micro, server)

    _run_micro(micro, tmp_path)

    assert server.fill_max_tokens == [1024]


# ── the budgets are pinned to the measured fills ───────────────────────────────

@pytest.mark.parametrize("fill", sorted(_MEASURED))
def test_each_fill_budget_is_about_3x_its_healthy_max_and_fits_the_context(fill):
    budget, n, _median, healthy_max, max_prompt = _MEASURED[fill]
    assert n >= 50, "too small a sample to size a budget from"
    assert budget.healthy_max == healthy_max
    ratio = budget.tokens / healthy_max
    assert 2.5 <= ratio <= 3.5, (fill, ratio)
    # The cap must be reached before the context wall, or it stops nothing.
    assert budget.tokens + max_prompt < _LOADED_CONTEXT
    assert budget.tokens < _GLOBAL_MAX_TOKENS


def test_the_budget_values_are_pinned():
    # Changing one means re-measuring: update _MEASURED above too.
    assert (UNITY_FILL.tokens, GTA5_FILL.tokens, MICRO_FILL.tokens) == (30_000, 3_600, 4_500)


@pytest.mark.parametrize("configured,extra,expected", [
    (None, (), 3_600),
    (_GLOBAL_MAX_TOKENS, (), 3_600),       # the global never raises it
    (2048, (), 2048),                      # a smaller config is kept
    (None, (1000,), 1000),                 # an extra cap applies too
    (MagicMock(), (), 3_600),              # a test double's attribute
    (True, (), 3_600),
    (0, (), 3_600),
])
def test_the_smallest_applicable_cap_wins(configured, extra, expected):
    assert GTA5_FILL.cap(configured=configured, extra=extra) == expected


@pytest.mark.parametrize("budget", [GTA5_FILL, MICRO_FILL, UNITY_FILL], ids=lambda b: b.purpose)
def test_the_truncation_remedy_depends_on_where_the_cap_came_from(budget: FillBudget):
    runaway = budget.truncated(budget.tokens, 12_345)
    assert isinstance(runaway, ValueError)
    assert str(runaway).startswith(f"{budget.purpose} reply cut off at its {budget.tokens:,}-token cap")
    assert "12,345 chars" in str(runaway) and "likely a runaway" in str(runaway)
    assert "reasoning_effort=none" in str(runaway)

    small = budget.truncated(budget.tokens // 4, 500)
    assert f"Raise lm_studio.max_tokens to {budget.tokens:,}" in str(small)
    assert "runaway" not in str(small)
