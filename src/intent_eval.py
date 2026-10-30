"""Prompt-level golden evaluation for intent, controller, mechanics and assets."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

from .mechanics import default_mechanic_registry
from .unity_compose import subsystems_for
from .unity_pipeline_stages import ExtractIntentStage, ResolveIntentStage
from .unity_schema import UnitySchema


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "intent" / "cases.json"
RUNS_ROOT = ROOT / "eval_runs"


def load_intent_cases(path: Path = CASES_PATH) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or len(cases) < 30:
        raise ValueError("intent evaluation requires at least 30 cases")
    categories = {category: 0 for category in ("structured", "natural", "regression")}
    for case in cases:
        category = case.get("category")
        if category not in categories:
            raise ValueError(f"unknown intent eval category {category!r}")
        categories[category] += 1
        if "expected" not in case:
            raise ValueError(f"intent eval {case.get('id')!r} has no golden output")
    if any(count < 10 for count in categories.values()):
        raise ValueError(f"intent eval cohorts are incomplete: {categories}")
    return cases


def evaluate_intent_case(case: dict[str, Any]) -> dict[str, Any]:
    seed = case.get("seed") or {}
    schema = UnitySchema(
        project_name="IntentEval",
        game_type=seed.get("game_type", "3D"),
        platform="pc",
        render_pipeline="URP",
        scenes=["Main"],
        locomotion=seed.get("locomotion", "walk"),
    )
    prompt = str(case["prompt"])
    _classified, ledger = ExtractIntentStage().run(prompt)
    resolution, _messages = ResolveIntentStage().run(
        schema, ledger, prompt=prompt, schema_source="schema_llm"
    )
    schema.meta["mechanic_registry"] = default_mechanic_registry().manifest(schema)
    subsystems = subsystems_for(
        schema.game_type,
        schema.features,
        locomotion=schema.locomotion,
        locomotion_modes=schema.locomotion_modes,
        flying_abilities=schema.meta.get("flying_abilities"),
        user_prompt=prompt,
        entities=schema.entities,
        genre=schema.meta.get("genre", "universal"),
        projectile_contract=schema.meta.get("physical_projectile_contract"),
        intent_exclusions=schema.meta.get("intent_exclusions"),
    )
    controller = next(sub.variant for sub in subsystems if sub.kind == "controller")
    mechanics = [item["name"] for item in schema.meta["mechanic_registry"]["enabled"]]
    return {
        "schema": {
            "game_type": schema.game_type,
            "locomotion": schema.locomotion,
            "genre": schema.meta.get("genre"),
            "exclusions": list(resolution.exclusions),
            "projectile": "physical_projectile_contract" in schema.meta,
        },
        "controller": controller,
        "mechanics": sorted(mechanics),
        "assets": sorted({str(entity.kind) for entity in schema.entities}),
    }


def run_intent_evals(*, write_history: bool = False,
                     run_date: date | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in load_intent_cases():
        actual = evaluate_intent_case(case)
        results.append({
            "id": case["id"],
            "category": case["category"],
            "pass": actual == case["expected"],
            "expected": case["expected"],
            "actual": actual,
        })
    if write_history:
        target = RUNS_ROOT / str(run_date or date.today())
        target.mkdir(parents=True, exist_ok=True)
        (target / "intent.json").write_text(
            json.dumps(results, indent=2) + "\n", encoding="utf-8"
        )
    return results
