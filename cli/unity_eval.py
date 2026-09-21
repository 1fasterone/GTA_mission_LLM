"""Unity QA/evaluation commands: playability / stress-test / fidelity / eval."""
from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

from src.unity_schema import UnitySchema

from .common import app, console


@app.command("unity-visual-baseline")
def unity_visual_baseline(
    prompt_file: Path = typer.Option(..., "--prompt-file",
                                     help="Unchanged natural-language brief used by both arms"),
    reference_image: Path = typer.Option(..., "--reference-image",
                                         help="Image supplied to Concept in the reference arm"),
    text_project: Path = typer.Option(..., "--text-project",
                                      help="Completed text-only Unity project directory"),
    image_project: Path = typer.Option(..., "--image-project",
                                       help="Completed reference-image Unity project directory"),
    config_file: Path = typer.Option(Path("config.yaml"), "--config",
                                     help="Frozen pipeline config used by both runs"),
    catalog_file: Path = typer.Option(
        Path("lookups/unity/asset_catalog.json"), "--catalog",
        help="Frozen catalog snapshot used by both runs"),
    output: Path = typer.Option(
        Path("output/visual_compiler/baseline.json"), "--output",
        help="Manifest destination"),
    model: str = typer.Option(..., "--model",
                              help="Pipeline model identifier used by both runs"),
    seed: int = typer.Option(..., "--seed",
                             help="Pipeline seed used by both runs"),
) -> None:
    """Capture hashes and lineage for the Phase 1 image-vs-text baseline.

    This command records two completed runs; it does not invoke the LLM or
    regenerate either project. Generate the arms first with the same prompt,
    config, catalog, model, and seed.
    """
    from src.visual_compiler_baseline import (BaselineArtifactError,
                                               build_baseline_manifest,
                                               write_baseline_manifest)

    try:
        manifest = build_baseline_manifest(
            prompt_file,
            reference_image=reference_image,
            text_project=text_project,
            image_project=image_project,
            config_file=config_file,
            catalog_file=catalog_file,
            repo_root=Path.cwd(),
            model=model,
            seed=seed,
        )
        path = write_baseline_manifest(manifest, output)
    except BaselineArtifactError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    console.print(
        f"[green]Baseline captured[/] {path} "
        f"(identity {manifest['identity']['hash']})"
    )


@app.command("unity-visual-baseline-import")
def unity_visual_baseline_import(
    concept_loop: Path = typer.Argument(
        ..., help="Existing output/concept_loop directory containing lineage.json"),
    output: Path = typer.Option(
        Path("output/visual_compiler/concept_loop.baseline.json"), "--output",
        help="Manifest destination"),
) -> None:
    """Import historical Concept-loop evidence without upgrading its status."""
    from src.visual_compiler_baseline import (BaselineArtifactError,
                                               build_concept_loop_manifest,
                                               write_baseline_manifest)

    try:
        manifest = build_concept_loop_manifest(concept_loop, repo_root=Path.cwd())
        path = write_baseline_manifest(manifest, output)
    except BaselineArtifactError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    strict = manifest["validity"]["strict_phase1"]
    status = "strict" if strict else "partial historical evidence"
    console.print(
        f"[green]Baseline imported[/] {path} ({status}; "
        f"identity {manifest['identity']['hash']})"
    )


@app.command("unity-visual-shopping-list")
def unity_visual_shopping_list(
    project: Path = typer.Argument(
        ..., help="Completed Unity project directory containing a schema"
    ),
    board_image: Path = typer.Option(
        None, "--board-image",
        help="Exact image used for the Concept; retained as provenance"
    ),
    output: Path = typer.Option(
        Path("output/visual_compiler/shopping_list.json"), "--output",
        help="Shopping-list sidecar destination"
    ),
) -> None:
    """Extract deterministic model-fulfilment demands from a Unity run.

    The command reads declared entities and resolver paths only. It never
    invokes the LLM, edits the schema, or treats an unresolved entity as a
    default prop.
    """
    from src.visual_compiler_shopping_list import (
        ShoppingListArtifactError,
        build_shopping_list,
        write_shopping_list,
    )

    try:
        manifest = build_shopping_list(
            project,
            board_image=board_image,
            repo_root=Path.cwd(),
        )
        path = write_shopping_list(manifest, output)
    except ShoppingListArtifactError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    console.print(
        f"[green]Shopping list captured[/] {path} "
        f"({len(manifest['entries'])} entries; "
        f"identity {manifest['identity']['hash']})"
    )


@app.command("unity-visual-board")
def unity_visual_board(
    concept_file: Path = typer.Argument(..., help="Immutable semantic Concept.txt"),
    output: Path = typer.Option(..., "--output", "-o", help="Board PNG destination"),
    manifest: Path = typer.Option(None, "--manifest",
                                  help="Comfy workflow/provenance manifest destination"),
    config_file: Path = typer.Option(Path("config.yaml"), "--config"),
    checkpoint: str = typer.Option(
        "sd15ModelLab_realV001_1651730.safetensors", "--checkpoint"),
    seed: int = typer.Option(978075547277410, "--seed"),
    width: int = typer.Option(768, "--width"),
    height: int = typer.Option(768, "--height"),
    steps: int = typer.Option(20, "--steps"),
    cfg: float = typer.Option(8.0, "--cfg"),
    sampler: str = typer.Option("dpmpp_2m_sde_gpu", "--sampler"),
    scheduler: str = typer.Option("karras", "--scheduler"),
) -> None:
    """Generate one canonical visual board through ComfyUI's headless API."""
    from src.visual_compiler_board import (
        ComfyBoardClient, VisualBoardError, concept_board_prompt,
    )

    if not concept_file.is_file():
        console.print(f"[red]Concept.txt not found:[/] {concept_file}")
        raise typer.Exit(2)
    try:
        concept = concept_file.read_text(encoding="utf-8")
        cfg_data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        diffusion_cfg = ((cfg_data.get("blender") or {}).get("diffusion") or {})
        client = ComfyBoardClient(
            base_url=str(diffusion_cfg.get("base_url", "http://127.0.0.1:8188")),
            timeout=float(diffusion_cfg.get("timeout", 180)),
            poll_timeout=float(diffusion_cfg.get("poll_timeout", 900)),
        )
        prompt = concept_board_prompt(concept)
        negative = (
        "text, watermark, logo, UI, HUD, letters, numbers, collage, grid, "
            "photorealistic, horror, gore, nsfw, deformed, blurry"
        )
        manifest_path = manifest or output.with_suffix(".comfy.json")
        path = client.generate(
            prompt=prompt, negative=negative, checkpoint=checkpoint, seed=seed,
            width=width, height=height, steps=steps, cfg=cfg,
            sampler_name=sampler, scheduler=scheduler, output=output,
            manifest=manifest_path,
        )
        freed = client.free_models()
        console.print(f"[green]Visual board written[/] {path}")
        console.print(f"[green]Comfy manifest written[/] {manifest_path}")
        console.print(f"[green]Comfy models freed:[/] {freed}")
    except (OSError, ValueError, VisualBoardError) as exc:
        console.print(f"[red]Visual board error:[/] {exc}")
        raise typer.Exit(2) from exc


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
