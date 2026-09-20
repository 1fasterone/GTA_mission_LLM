"""Unity QA/evaluation commands: playability / stress-test / fidelity / eval."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from src.unity_schema import UnitySchema

from .common import _cfg, app, console


def _mechanics_composition(schema: UnitySchema, cfg: dict, compose: bool):
    """Return deterministic composed units and the seam report for playtesting.

    A saved composed schema normally carries ``meta.wiring`` from the real
    pipeline. ``--compose`` reconstructs the plan and calls
    ``MicroComposer.plan_wiring`` instead; that dry-run executes the exact
    binding code used by emission without creating Unity files.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    saved_report = meta.get("wiring")
    wiring_report = dict(saved_report) if isinstance(saved_report, dict) else {}
    if not compose:
        return (), wiring_report

    from src.unity_compose import MicroComposer, subsystems_for
    from src.unity_pipeline import (_compose_palette_rgb, _infer_mission_scenes,
                                    _objective_scene_map)

    pipe_cfg = cfg.get("unity_pipeline", cfg.get("pipeline", {})) or {}
    features = list(schema.features or [])
    if "audio" not in features:
        # unity_pipeline.run() injects audio before composition. Mirror that
        # deterministic input here without mutating the loaded schema.
        features.append("audio")

    mission_scenes = meta.get("mission_scenes")
    if not isinstance(mission_scenes, dict):
        mission_scenes, _ = _infer_mission_scenes(schema)

    subsystems = subsystems_for(
        schema.game_type,
        features,
        locomotion=schema.locomotion,
        locomotion_modes=schema.locomotion_modes,
        flying_abilities=meta.get("flying_abilities"),
        capabilities=schema.capabilities,
        user_prompt=meta.get("user_prompt", ""),
        scenes=schema.scenes,
        entities=schema.entities,
        scripts=schema.scripts,
        genre=meta.get("genre", "universal"),
        audio_specs=schema.audio_specs,
        vfx_specs=schema.vfx,
        shaders=schema.shaders,
        animations=schema.animations,
        characters=schema.characters,
        missions=schema.missions,
        behavior_trees=schema.behavior_trees,
        environment=schema.environment,
        visual_style=schema.visual_style,
        palette_rgb=_compose_palette_rgb(schema),
        cover_points=meta.get("cover_points"),
    )
    composer = MicroComposer(
        schema.project_name,
        template_dir=pipe_cfg.get("unity_template_dir", "templates/unity"),
        project_config=cfg,
    )
    units = composer.plan(subsystems)
    escort_followed_entities = {
        entity.name for entity in schema.entities
        if entity.name and entity.behavior
        and any(getattr(atom, "atom", "") == "Follow" for atom in (entity.atoms or []))
    }
    reach_target_entities = {
        entity.name: entity.count for entity in schema.entities if entity.name
    }
    wiring_report = composer.plan_wiring(
        units,
        wiring_map=schema.wiring,
        objective_scenes=_objective_scene_map(schema, mission_scenes),
        escort_followed_entities=escort_followed_entities,
        reach_target_entities=reach_target_entities,
        game_type=schema.game_type,
    )
    return tuple(units), wiring_report


@app.command("unity-mechanics-playtest")
def unity_mechanics_playtest(
    schema_file: str = typer.Argument(..., help="Path to a Unity schema JSON file"),
    config: str = typer.Option("config.yaml", "--config", "-c",
                               help="Config file containing mechanics_playtest settings"),
    compose: bool = typer.Option(False, "--compose",
                                 help="Rebuild the deterministic composition/wiring plan "
                                      "without emitting Unity files"),
    output: str | None = typer.Option(None, "--output", "-o",
                                      help="Write the structured PLAYABILITY_REPORT JSON here"),
    json_output: bool = typer.Option(False, "--json",
                                     help="Print the structured PLAYABILITY_REPORT JSON"),
    strict: bool = typer.Option(False, "--strict",
                                help="Exit non-zero when FINAL_DECISION is FAIL"),
    max_seconds: int | None = typer.Option(None, "--max-seconds",
                                            help="Override the configured simulation horizon"),
) -> None:
    """Run the deterministic pre-IR mechanics playtest on a saved schema.

    This command adapts the existing UnitySchema and, when requested, the
    exact MicroComposer wiring plan into the engine-neutral evaluator. It does
    not call the LLM, invoke Unity, or generate project files.

      python main.py unity-mechanics-playtest MyGame.schema.json
      python main.py unity-mechanics-playtest MyGame.schema.json --compose --strict
      python main.py unity-mechanics-playtest MyGame.schema.json --json \
          --output output/MyGame/mechanics_playability.json
    """
    cfg = _cfg(config)
    mechanics_cfg = cfg.get("mechanics_playtest", {}) or {}
    if not isinstance(mechanics_cfg, dict):
        console.print("[yellow]ABSTAIN[/] mechanics_playtest config must be an object")
        raise typer.Exit(2)
    if mechanics_cfg.get("enabled", True) is False:
        console.print("[yellow]SKIP[/] mechanics playtest disabled by config")
        return

    configured_seconds = mechanics_cfg.get("max_seconds", 300)
    seconds = configured_seconds if max_seconds is None else max_seconds
    try:
        seconds = int(seconds)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("max_seconds must be an integer") from exc
    if seconds < 0:
        raise typer.BadParameter("max_seconds must be non-negative")

    data = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    schema = UnitySchema.model_validate(data)
    units, wiring_report = _mechanics_composition(schema, cfg, compose)

    from src.unity_mechanics_adapter import build_mechanics_playtest_input
    from src.unity_mechanics_playtest import evaluate_mechanics

    playtest_input = build_mechanics_playtest_input(
        schema,
        composed_units=units,
        wiring_report=wiring_report,
    )
    report = evaluate_mechanics(playtest_input, max_seconds=seconds)
    report_data = report.to_dict()
    if output:
        report_path = Path(output)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report_data, indent=2) + "\n",
                               encoding="utf-8")
        console.print(f"[green]OK[/]  mechanics report -> {report_path}")
    if json_output:
        console.print(json.dumps(report_data, indent=2), markup=False)
    else:
        console.print(report.render(), markup=False)

    gate_enabled = strict or bool(mechanics_cfg.get("gate", False))
    if gate_enabled and report.final_decision.get("decision") != "PASS":
        console.print("[red]FAIL[/] mechanics playtest gate failed")
        raise typer.Exit(1)


@app.command("unity-playability")
def unity_playability(
    schema_file: str = typer.Argument(..., help="Path to a Unity schema JSON file"),
    min_score:   int = typer.Option(0, "--min-score",
                                    help="Exit non-zero below this 0-100 score"),
    strict:     bool = typer.Option(False, "--strict",
                                    help="Exit non-zero on any playability ERROR"),
) -> None:
    """Grade a Unity schema on whether it is actually a GAME.

    Deterministic structural checks — no LLM, no Unity, no runtime. Every
    other command answers "did it compile"; this one answers whether the
    player has an avatar, an objective, and content they can reach, and
    whether the entities the brief named explicitly survived the build.

      python main.py unity-playability output/unity/DropZone/DropZone.schema.json
      python main.py unity-playability MyGame.schema.json --strict
    """
    from src.unity_playability import evaluate, gate

    data   = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    report = evaluate(UnitySchema.model_validate(data))
    # markup=False: findings carry schema field paths that rich reads as markup.
    console.print(report.render(), markup=False)
    reason = gate(report, min_score=min_score, fail_on_error=strict)
    if reason:
        console.print(f"[red]FAIL[/] {reason}")
        raise typer.Exit(1)


@app.command("micro-stress-test")
def micro_stress_test(
    kind:   list[str] = typer.Option(None, "--kind",
                                     help="Stress only this micro kind (repeatable). "
                                          "Default: every kind in the registry."),
    strict: bool = typer.Option(False, "--strict",
                                help="Exit non-zero if any kind found live poison "
                                     "or errored."),
) -> None:
    """Depth-test every micro-generator's sanitizer stack, one kind at a time.

    Builds a single-subsystem composed project per (kind, variant, backend)
    combination with an adversarial payload aimed at every sanitizer layer
    this project has — an invented constructor, a composed-sibling call with
    the wrong arity, an invented member on a real composed class, a
    GetComponent-family call with the wrong argument count, a backend-
    mismatched field reference, and extra_fields colliding with that kind's
    own reserved names — then renders through the real pipeline
    (MicroComposer.plan -> sanitize_subsystem_* -> emit, the same order
    unity_pipeline.py runs in --compose mode) and checks whether any of it
    survived as LIVE code. Every variant the kind declares, both backends
    where its schema has one.

    Deterministic and offline — no LLM, no Unity. See src/micro_stress.py's
    module docstring for what this can and cannot prove (in particular:
    harden_against_template already wholesale-strips any GameContext
    reference from micro-generator content, so the composed-sibling poison
    lines mostly confirm that net rather than uniquely prove this project's
    own arity-aware checks fired).

      python main.py micro-stress-test
      python main.py micro-stress-test --kind hud --kind saveload
      python main.py micro-stress-test --strict
    """
    from src.micro_stress import render_scorecard, run_all

    results = run_all(only=list(kind) if kind else None)
    console.print(render_scorecard(results))
    if strict and any(not r.ok for r in results):
        raise typer.Exit(1)


@app.command("unity-fidelity")
def unity_fidelity(
    schema_file: str = typer.Argument(..., help="Path to a Unity schema JSON file"),
    brief_file:  str = typer.Option(None, "--brief",
                                    help="Brief to grade against; defaults to "
                                         "schema.meta.user_prompt"),
    min_score:   int = typer.Option(0, "--min-score",
                                    help="Exit non-zero below this 0-100 score"),
    limit:       int = typer.Option(20, "--limit",
                                    help="Max losses to list"),
) -> None:
    """Grade how much of the brief's own vocabulary reached the build.

    Playability answers "is it a game". This answers "is it THE game that was
    asked for" — the two are independent, and a build can score 100 on one
    while losing every adjective in the prompt.

    Two kinds of loss. A MODIFIER gap is a word bound to a noun in the brief
    that appears nowhere in the output ("purple", "six legs"). An ASSET gap is
    an entity whose resolved model shares no vocabulary with the thing it is
    supposed to be — "commit node" resolved to marble-center-butterfly-low.fbx,
    where the entity name survived and the model is a marble run. Asset gaps
    need `schema.meta.entity_model_paths`, which the generator stamps.

      python main.py unity-fidelity output/unity/DropZone/DropZone.schema.json
      python main.py unity-fidelity MyGame.schema.json --brief prompts/hippos.txt
    """
    from src.unity_concept_fidelity import check_fidelity, render_scorecard

    data   = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    schema = UnitySchema.model_validate(data)
    meta   = schema.meta if isinstance(schema.meta, dict) else {}

    if brief_file:
        brief = Path(brief_file).read_text(encoding="utf-8")
    else:
        brief = str(meta.get("user_prompt", "") or "")
    if not brief.strip():
        # Abstain loudly with the remedy, never grade a build against nothing:
        # a 100/100 from an empty brief is the worst possible output here.
        console.print("[yellow]ABSTAIN[/] no brief to grade against — this "
                      "schema carries no meta.user_prompt. Pass --brief "
                      "<file> with the brief it was built from.")
        raise typer.Exit(2)

    paths  = meta.get("entity_model_paths")
    report = check_fidelity(
        brief, schema,
        entity_model_paths=paths if isinstance(paths, dict) else None)
    if not report.checked:
        # Never print 100/100 for a build nothing was checked against — an
        # unearned perfect score is worse than no score, because it is the one
        # output a reader will act on without reading.
        console.print("[yellow]ABSTAIN[/] nothing to check — the brief names no "
                      "concrete nouns and the schema has no entities to bind "
                      "them to. Fidelity NOT graded.")
        raise typer.Exit(2)
    console.print(render_scorecard(report, limit=limit), markup=False)
    if not isinstance(paths, dict):
        console.print("[yellow]NOTE[/]  no meta.entity_model_paths — modifiers "
                      "graded, assets NOT graded. Re-run the build to stamp it.")
    if min_score and report.score < min_score:
        console.print(f"[red]FAIL[/] concept fidelity {report.score}/100 "
                      f"below --min-score {min_score}")
        raise typer.Exit(1)


@app.command("unity-eval")
def unity_eval(
    case:  list[str] = typer.Option(None, "--case",
                                    help="Case name(s) to run; default all"),
    live:  bool = typer.Option(False, "--live",
                               help="Run the LIVE tier (full pipeline through "
                                    "the LLM). Deferred — no live cases ship yet"),
    update_floors: bool = typer.Option(False, "--update-floors",
                                       help="Re-lock each case's floor to what "
                                            "it scores now (after a deliberate fix)"),
    quiet: bool = typer.Option(False, "--quiet", "-q",
                               help="One line per case instead of full scorecards"),
) -> None:
    """Playability regression eval — replay captured schemas, grade, compare.

    Each case replays a filled schema through the deterministic tail (Steps
    4-6), grades the final schema AND the files the generator emitted, and
    compares the result to a floor locked in the case file. Exits non-zero when
    a case drops below its floor, loses a check it used to pass, or stops being
    able to grade an ERROR-class check at all.

      python main.py unity-eval
      python main.py unity-eval --case dropzone_arcade
      python main.py unity-eval --update-floors

    Logs land in evals/playability/_runs/<case>.{json,txt} — the JSON names the
    pipeline stage behind every finding.
    """
    from src.unity_eval import (CASES_DIR, EvalCaseError, floor_from_result,
                                load_cases, run_cases)

    if live:
        console.print("[yellow]--live is not wired yet[/]: the live tier runs "
                      "the full pipeline from brief text through LM Studio, "
                      "but no live cases ship in evals/playability/cases yet. "
                      "Run without --live for the replay tier.")
        raise typer.Exit(2)
    try:
        cases   = load_cases(only=case or None)
        results = run_cases(cases)
    except EvalCaseError as exc:
        console.print(f"[red]eval case error[/] {exc}")
        raise typer.Exit(2) from exc

    failed = [r for r in results if not r.ok]
    for r in results:
        tag = "[green]PASS[/]" if r.ok else "[red]FAIL[/]"
        console.print(f"\n{tag} [bold]{r.case.name}[/] "
                      f"[dim]({r.case.input_kind})[/]  "
                      f"score {r.score}/100  floor {r.case.floor_score}")
        if not quiet:
            # markup=False: findings carry schema field paths and C# generics
            # that rich would read as console markup.
            console.print(r.report.render(), markup=False)
        for reason in r.reasons():
            console.print(f"  [red]![/] {reason}", markup=False)
        if not r.ok:
            for hint in r.stage_hints():
                console.print(f"  [dim]look at:[/] {hint}", markup=False)

    if update_floors:
        for r in results:
            path = CASES_DIR / f"{r.case.name}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["floor"] = floor_from_result(r)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            console.print(f"[cyan]re-locked[/] {path.name} -> "
                          f"score {r.score}, "
                          f"{len(data['floor']['must_pass'])} must-pass checks")
        return

    console.print(f"\n{len(results) - len(failed)}/{len(results)} case(s) at or "
                  f"above their floor")
    if failed:
        raise typer.Exit(1)
