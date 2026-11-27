"""Step 3.5 enrichment calls carry an explicit token cap (src/llm_budgets.py).

Regression for a live run (neon_fugitive E2E, 2026-11-14): one NPC-dialog call
inherited `lm_studio.max_tokens: 88000`, ran to ~76,000 tokens over 53 minutes at
temperature 0, and was stopped by hand. Every small structured Step 3.5 call now
takes a budget sized from its own item count, and a reply the server cut off at
that cap (`finish_reason == "length"`) is rejected loudly instead of parsed.

The fake LLMs below are engineer-authored test doubles, never Pipeline LLM output.
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from src.audio.concept_translator import translate_concept as translate_audio_prompts
from src.audio.dialog_resolver import extract_dialog_lines
from src.audio.resolver import extract_cues
from src.llm_budgets import (
    AUDIO_CUES,
    AUDIO_PROMPTS,
    DIALOG_DEFAULT_LINES,
    DIALOG_LINES,
    ENTITY_QUERIES,
    MICRO_CODE_TOKENS,
    MODEL_BRIEFS,
    VISION_PICK_TOKENS,
    Budget,
    capped_chat,
)
from src.unity_compose import Subsystem
from src.unity_micro_code import CODE_KEYS, enrich_subsystems
from src.unity_schema import EntitySpec
from src.unity_sketchfab_collection import extract_model_briefs
from src.unity_sketchfab_translator import translate_concept as translate_entity_queries
from src.unity_sketchfab_vision import select_best_candidate

CONCEPT = "A rain-soaked cyberpunk city where a rogue enforcer hunts bounties on a hoverbike."
GLOBAL_SCHEMA_BUDGET = 88000   # config.yaml lm_studio.max_tokens on the machine that hit this

_DIALOG_REPLY = json.dumps([
    {"line_id": "greet", "text": "The neon never sleeps, hunter.", "emotion": "calm", "tags": ["greeting"]},
    {"line_id": "warn", "text": "Drones sweep the market at midnight.", "emotion": "fearful", "tags": ["warning"]},
])
_CUES_REPLY = json.dumps([
    {"source": "rain on tin roofs", "description": "steady rain drumming on corrugated tin roofs at night"},
    {"source": "hoverbike engine", "description": "low electric hum of a hoverbike engine idling nearby"},
])
_BRIEFS_REPLY = json.dumps({"style": "cyberpunk", "models": [
    {"name": "hoverbike", "query": "hoverbike", "category": "vehicle"},
    {"name": "street sign", "query": "street sign", "category": "prop"},
]})
_QUERIES_REPLY = json.dumps({"style": "cyberpunk", "entities": {"Hoverbike": "hoverbike", "Drone": "drone"}})
_PROMPTS_REPLY = json.dumps({"rain_on_tin_roofs": "Steady rain drumming on a tin roof at night."})
_VISION_REPLY = json.dumps({"pick": 2, "reason": "the second thumbnail is a hoverbike"})


class RecordingLLM:
    """Accepts the real client's keyword surface and records every max_tokens it receives.

    `finish_reason` is what the server would report for each reply; "length"
    means the reply was cut off at max_tokens.
    """

    def __init__(self, reply: str, finish_reason: str = "stop"):
        self.reply = reply
        self.finish_reason = finish_reason
        self.max_tokens: list[int | None] = []
        self.last_finish_reason: str | None = None

    def chat(self, system=None, user=None, temperature=None, thinking=True, seed=None,
             max_tokens=None, json_mode=False, json_schema=None, images=None):
        self.max_tokens.append(max_tokens)
        self.last_finish_reason = self.finish_reason
        return self.reply

    @staticmethod
    def extract_json(raw: str) -> dict:
        return json.loads(raw)


class _ThumbClient:
    def fetch_image_bytes(self, url):  # noqa: ARG002
        return b"\xff\xd8\xff fake jpeg"


def _candidates() -> list[dict]:
    return [{"uid": uid, "thumbnails": {"images": [{"url": f"https://x/{uid}.jpg", "width": 256}]}}
            for uid in ("a", "b", "c")]


def _cues() -> list[dict]:
    return [{"id": "rain_on_tin_roofs", "category": "ambient", "descriptor": "rain", "duration_s": 4.0},
            {"id": "engine_hum", "category": "sfx", "descriptor": "engine", "duration_s": 2.0}]


def _entities() -> list[EntitySpec]:
    return [EntitySpec(name="Hoverbike", kind="vehicle"), EntitySpec(name="Drone", kind="enemy")]


# Each case: (label, reply, call(llm) -> result, the cap it must send, is_failure(result)).
_CASES = [
    ("dialog lines", _DIALOG_REPLY,
     lambda llm: extract_dialog_lines(CONCEPT, llm, speaker="Fixer", count=12),
     DIALOG_LINES.tokens(12), lambda r: r == []),
    ("audio cues", _CUES_REPLY,
     lambda llm: extract_cues(CONCEPT, llm, n=10),
     AUDIO_CUES.tokens(10), lambda r: r == []),
    ("audio prompts", _PROMPTS_REPLY,
     lambda llm: translate_audio_prompts(CONCEPT, _cues(), llm),
     AUDIO_PROMPTS.tokens(2), lambda r: r == 0),
    ("model briefs", _BRIEFS_REPLY,
     lambda llm: extract_model_briefs(CONCEPT, llm, count=12),
     MODEL_BRIEFS.tokens(12), lambda r: r == ("", [])),
    ("entity queries", _QUERIES_REPLY,
     lambda llm: translate_entity_queries(CONCEPT, _entities(), llm),
     ENTITY_QUERIES.tokens(2), lambda r: r == 0),
    ("vision pick", _VISION_REPLY,
     lambda llm: select_best_candidate("Hoverbike", "vehicle", "a hoverbike", _candidates(),
                                       client=_ThumbClient(), llm=llm),
     VISION_PICK_TOKENS, lambda r: r is None),
]
_IDS = [case[0] for case in _CASES]


def test_budget_is_overhead_plus_per_item_clamped():
    budget = Budget(overhead=100, per_item=10, floor=150, ceiling=300)
    assert budget.tokens(0) == 150          # floor
    assert budget.tokens(-5) == 150         # negative counts are zero items
    assert budget.tokens(None) == 150       # type: ignore[arg-type]
    assert budget.tokens(10) == 200
    assert budget.tokens(1000) == 300       # ceiling


def test_measured_healthy_replies_fit_with_headroom():
    # (budget at the usual count, largest healthy completion measured 2026-11-12..14)
    measured = {
        "dialog lines @12": (DIALOG_LINES.tokens(12), 784),
        "audio cues @10": (AUDIO_CUES.tokens(10), 389),
        "entity queries @20": (ENTITY_QUERIES.tokens(20), 274),
        "model briefs @12": (MODEL_BRIEFS.tokens(12), 217),
        "vision pick": (VISION_PICK_TOKENS, 48),
    }
    for label, (cap, observed_max) in measured.items():
        assert cap >= 2.5 * observed_max, f"{label}: cap {cap} too close to observed {observed_max}"


def test_every_cap_is_far_below_the_schema_fill_budget():
    budgets = [DIALOG_LINES, AUDIO_CUES, AUDIO_PROMPTS, MODEL_BRIEFS, ENTITY_QUERIES]
    for budget in budgets:
        assert budget.tokens(10_000) == budget.ceiling
        assert budget.ceiling <= GLOBAL_SCHEMA_BUDGET // 10
    assert VISION_PICK_TOKENS <= 1000
    assert MICRO_CODE_TOKENS <= GLOBAL_SCHEMA_BUDGET // 10


def test_dialog_without_a_count_budgets_for_the_default_lines():
    llm = RecordingLLM(_DIALOG_REPLY)
    extract_dialog_lines(CONCEPT, llm, speaker="Fixer", count=0)
    assert llm.max_tokens == [DIALOG_LINES.tokens(DIALOG_DEFAULT_LINES)]


@pytest.mark.parametrize("label,reply,call,cap,is_failure", _CASES, ids=_IDS)
def test_each_enrichment_call_sends_its_own_cap(label, reply, call, cap, is_failure):
    llm = RecordingLLM(reply)
    result = call(llm)
    assert llm.max_tokens == [cap], label
    assert not is_failure(result), f"{label}: control reply should succeed"


@pytest.mark.parametrize("label,reply,call,cap,is_failure", _CASES, ids=_IDS)
def test_a_reply_cut_off_at_the_cap_is_rejected_not_parsed(label, reply, call, cap, is_failure, caplog):
    llm = RecordingLLM(reply, finish_reason="length")
    with caplog.at_level(logging.WARNING, logger="src.llm_budgets"):
        result = call(llm)
    assert is_failure(result), f"{label}: a truncated reply was parsed as complete"
    assert any("[llm.budget]" in r.getMessage() and f"{cap}-token cap" in r.getMessage()
               for r in caplog.records), label


def test_capped_chat_passes_the_cap_and_propagates_errors():
    llm = RecordingLLM("ok")
    assert capped_chat(llm, purpose="probe", max_tokens=123, system="s", user="u") == "ok"
    assert llm.max_tokens == [123]

    class Boom:
        def chat(self, **_kwargs):
            raise RuntimeError("offline")

    with pytest.raises(RuntimeError):
        capped_chat(Boom(), purpose="probe", max_tokens=1, system="s", user="u")


def test_micro_code_treats_a_truncated_reply_as_a_failed_attempt():
    code = {"update_extra": "if (_oxygen > 0f) { _oxygen -= Time.deltaTime; }"}
    sub = Subsystem(kind="controller", variant="swimming", target="player",
                    name="DiverController", options={})
    llm = RecordingLLM(json.dumps(code), finish_reason="length")
    enrich_subsystems([sub], llm=llm, concept="A diver runs out of air.", attempts=2)
    assert llm.max_tokens == [MICRO_CODE_TOKENS, MICRO_CODE_TOKENS]   # both attempts capped
    assert not any(sub.options.get(k) for k in CODE_KEYS)

    control = Subsystem(kind="controller", variant="swimming", target="player",
                        name="DiverController", options={})
    enrich_subsystems([control], llm=RecordingLLM(json.dumps(code)), concept="A diver runs out of air.")
    assert control.options.get("update_extra") == code["update_extra"]


# ── audit: no LLM call in src/ silently inherits the schema-fill budget ───────

# Calls that legitimately use the client-wide budget: the Step 2 concept of each
# pipeline, a prose reply that never reaches extract_json. Every Step 3 schema
# fill passes its FillBudget cap. Keyed by (file, enclosing function) -> count,
# so a new uncapped call inside one of these functions still fails the audit.
_UNCAPPED_ALLOWED = {
    ("src/unity_pipeline.py", "_run_impl"): 1,   # Unity Step 2 concept
    ("src/pipeline.py", "_run_impl"): 1,         # GTA5 Step 2 concept
    ("src/micro_pipeline.py", "run"): 1,         # standalone micro Step 2 concept
}


class _ChatCallFinder(ast.NodeVisitor):
    """Counts `.chat(...)` calls without `max_tokens`, by innermost enclosing function."""

    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = ["<module>"]
        self.found: dict[tuple[str, str], int] = {}

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "chat"
                and "max_tokens" not in {k.arg for k in node.keywords}):
            key = (self.rel, self.stack[-1])
            self.found[key] = self.found.get(key, 0) + 1
        self.generic_visit(node)


def _uncapped_chat_calls() -> dict[tuple[str, str], int]:
    root = Path(__file__).resolve().parents[1]
    found: dict[tuple[str, str], int] = {}
    for path in sorted((root / "src").rglob("*.py")):
        finder = _ChatCallFinder(path.relative_to(root).as_posix())
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.update(finder.found)
    return found


def test_no_llm_call_inherits_the_schema_fill_budget_unless_allowlisted():
    uncapped = _uncapped_chat_calls()
    unexpected = {key: n for key, n in uncapped.items() if n > _UNCAPPED_ALLOWED.get(key, 0)}
    assert not unexpected, (
        "LLM .chat() calls without max_tokens inherit lm_studio.max_tokens (the schema-fill "
        f"budget). Give them a cap from src/llm_budgets.py: {unexpected}")


def test_the_audit_sees_the_known_uncapped_calls():
    # Guards the audit itself: if it stopped finding calls, the test above would pass vacuously.
    assert _uncapped_chat_calls() == _UNCAPPED_ALLOWED
