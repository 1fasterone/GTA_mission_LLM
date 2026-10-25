"""Unity project pipeline commands: create / path / terrain-gen / scene /
resolve / validate / migrate."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Optional

import typer

from src.unity_briefparse import BriefParseError
from src.unity_generator  import UnityProjectGenerator
from src.unity_pipeline   import UnityPipeline
from src.unity_resolver   import UnityResolver
from src.unity_schema     import UnitySchema, normalization_issues
from src.unity_reference_image import ReferenceImageError
from src.visual_reasoning_compiler import VisualReasoningCompilerError

from .common import app, console, _cfg, _load_authored_scene, _scene_cache_dir, _unity_pipe_cfg


# ── Unity commands ─────────────────────────────────────────────────────────────

def _frozen_collection_replay(schema: UnitySchema) -> tuple[Path | None, dict[str, dict], list[dict]]:
    """Return the verified Step-3.5 collection inputs for a no-LLM replay.

    The collection record is compiler-owned.  Its model bytes and the
    entity-to-catalog selections must be intact before the generator runs;
    otherwise this command would quietly resolve a different catalog entry.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    enrichment = meta.get("llm_enrichment")
    collection = enrichment.get("sketchfab_collection") if isinstance(enrichment, dict) else None
    if collection is None:
        return None, {}, []
    if not isinstance(collection, dict):
        raise ValueError("frozen Sketchfab collection is not an object")
    from src.unity_sketchfab_collection import replay_collection_entries

    cache_dir, entries = replay_collection_entries(collection)
    allowed = {
        (str(entry.get("filename") or ""), str(entry.get("relpath") or ""),
         str(entry.get("file_sha256") or ""))
        for entry in entries
    }
    bindings = collection.get("entity_bindings", {})
    if not isinstance(bindings, dict):
        raise ValueError("frozen Sketchfab entity_bindings must be an object")
    locked: dict[str, dict] = {}
    for entity_name, selection in sorted(bindings.items()):
        if not isinstance(selection, dict):
            raise ValueError(f"frozen Sketchfab binding for {entity_name!r} is not an object")
        identity = (
            str(selection.get("filename") or ""),
            str(selection.get("relpath") or ""),
            str(selection.get("file_sha256") or ""),
        )
        if identity not in allowed or selection.get("source") != "sketchfab":
            raise ValueError(
                f"frozen Sketchfab binding for {entity_name!r} is not in its collection"
            )
        locked[str(entity_name)] = dict(selection)
    return cache_dir, locked, entries

def _prepare_viz_reasoning(
    source_image: Path,
    output: Optional[Path],
    cfg: dict,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """Persist visual evidence and return canonical board/context paths.

    The user's brief remains semantic authority.  The context carries only
    image-grounded appearance and concrete asset-search evidence into Step 2.
    """
    from src.backends import make_chat_client
    from src.visual_reasoning_compiler import import_existing_board, reason_over_board

    safe_stem = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in source_image.stem
    ).strip("._-") or "visual"
    run_dir = output or (
        Path("output") / "visual_reasoning" / f"{safe_stem}_unity_create"
    )
    if run_dir.exists():
        if not overwrite:
            raise VisualReasoningCompilerError(
                f"refusing to overwrite visual-reasoning directory: {run_dir}. "
                "Use --viz-overwrite to replace this generated run."
            )
        resolved_run = run_dir.resolve()
        protected = {
            Path.cwd().resolve(),
            Path("output").resolve(),
            (Path("output") / "visual_reasoning").resolve(),
        }
        if run_dir.is_symlink() or resolved_run in protected:
            raise VisualReasoningCompilerError(
                f"refusing unsafe --viz-overwrite target: {run_dir}"
            )
        if not run_dir.is_dir():
            raise VisualReasoningCompilerError(
                f"--viz-overwrite target is not a directory: {run_dir}"
            )
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            raise VisualReasoningCompilerError(
                f"could not replace visual-reasoning directory {run_dir}: {exc}"
            ) from exc

    board = run_dir / "board.png"
    board_manifest = run_dir / "board.import.json"
    inventory = run_dir / "visual_inventory.json"
    concept = run_dir / "Concept.txt"
    context = run_dir / "context.txt"
    reasoning_manifest = run_dir / "reasoning.json"

    console.print("[cyan]Visual Slice 1:[/] importing canonical board")
    import_existing_board(
        board_image=source_image,
        output=board,
        manifest=board_manifest,
    )
    console.print("[cyan]Visual Slice 2:[/] inventory + image-derived Concept")
    llm = make_chat_client(cfg)
    try:
        reason_over_board(
            board_image=board,
            inventory_output=inventory,
            concept_output=concept,
            manifest=reasoning_manifest,
            board_manifest=board_manifest,
            context_output=context,
            llm=llm,
        )
    finally:
        eject = getattr(llm, "unload", None)
        if callable(eject):
            eject()
    console.print(f"[green]Visual reasoning artifacts written[/] {run_dir}")
    console.print(
        "[dim]The text brief remains semantic authority; context.txt supplies "
        "image-grounded asset language to Step 2.[/]"
    )
    return board, context

@app.command("unity-create")
def unity_create(
    prompt: Optional[str]  = typer.Argument(None,  help="Project idea in plain English"),
    config: str            = typer.Option("config.yaml", "--config", "-c"),
    file:   Optional[Path] = typer.Option(None, "--file", "-f",
                                          help="Read the prompt from a .txt file"),
    theme:       Optional[str] = typer.Option(None, "--theme",
                                              help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup",
                                              help="Second theme to fuse with --theme"),
    seed:        Optional[int] = typer.Option(None, "--seed",
                                              help="Seed for twist-seed selection (reproducibility)"),
    model:       Optional[str] = typer.Option(None, "--model", "-m",
                                              help="Override the configured LM Studio model for this run"),
    compose:     bool          = typer.Option(False, "--compose",
                                              help="Assemble micros into the project + wire them into the scene (combinatorial mode)"),
    geometry_only: bool        = typer.Option(False, "--geometry-only",
                                              help="Generate only the deterministic ProBuilder dungeon greybox (also enabled by explicit greybox/blockout prompts)"),
    greybox_plus: bool         = typer.Option(False, "--greybox-plus",
                                              help="Image-aware geometry-only Greybox+ planning contract; requires --viz and emits no final-art assets"),
    blender_models: bool       = typer.Option(False, "--blender-models",
                                              help="Build real models from the story in Blender (headless) and wire them into the scene via the Model Pack (replaces placeholders)"),
    route: Optional[Path]      = typer.Option(None, "--route",
                                              help="Route file from `unity-path` (routes.json) — compose the circuit around this authored/traced course instead of the parametric oval"),
    scene: Optional[Path]      = typer.Option(None, "--scene",
                                              help="Scene Compiler layout (layouts/<name>.json) or a scene file from `unity-scene` — the authored models AND their placement win over Sketchfab/Kenney"),
    reference_image: Optional[Path] = typer.Option(
        None, "--reference-image", "--image",
        help="Visual reference image sent to the Concept stage only; any PNG/JPEG/WEBP/GIF filename",
    ),
    viz: Optional[Path] = typer.Option(
        None, "--viz",
        help="Canonical PNG: create visual inventory, Concept, context.txt, and reasoning provenance; feed the context into Step 2 and the board into unity-create",
    ),
    viz_output: Optional[Path] = typer.Option(
        None, "--viz-output",
        help="Visual artifact directory (default: output/visual_reasoning/<image>_unity_create)",
    ),
    viz_overwrite: bool = typer.Option(
        False, "--viz-overwrite",
        help="Replace the existing visual-reasoning directory for this --viz run",
    ),
    models_dir: Optional[Path] = typer.Option(None, "--models-dir",
                                              help="--scene only: the Scene Compiler's models/ folder (default: the layout's own ../models sibling)"),
    no_think:    bool          = typer.Option(False, "--no-think",
                                              help="Disable LLM reasoning for every step (including the concept step, which normally reasons). Faster, but trades away concept creativity/quality — useful for timing/plumbing runs."),
    micro_code:  bool          = typer.Option(True, "--micro-code/--no-micro-code",
                                              help="With --compose: let the LLM author prompt-specific C# into each composed micro (one focused call per subsystem). --no-micro-code keeps the deterministic baseline bodies and is faster."),
    no_llm:      bool          = typer.Option(False, "--no-llm",
                                              help="Run the compiler with NO model at all: Steps 2-3 are replaced by a deterministic parse of the brief's own sections (TITLE/SCENES/NARRATIVE/SCRIPTS/ENTITIES/AUDIO/...). Requires a structured brief and abstains loudly if one section is missing. This is the control arm for measuring what the Pipeline LLM contributes — it is NOT a test of Step 3, which does not run."),
) -> None:
    """Generate a complete Unity project folder from a plain-English prompt.

    \b
    Inline prompt:
      python main.py unity-create "A 2D platformer with Cinemachine"

    Prompt with a visual reference:
      python main.py unity-create "A bright 3D FPS" --compose --viz reference.png

    Prompt from file (for long, structured briefs):
      python main.py unity-create --file prompts/neon_fugitive.txt
    """
    if theme and theme.lower() == "list":
        from src.themes import list_themes
        for t in list_themes():
            console.print(f"  [cyan]{t.name}[/]  —  {t.display}")
        raise typer.Exit(0)

    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}")
            raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()

    if reference_image is not None and viz is not None:
        console.print("[red]Error:[/] use either --viz or --image/--reference-image, not both")
        raise typer.Exit(1)
    if viz_output is not None and viz is None:
        console.print("[red]Error:[/] --viz-output requires --viz <PNG>")
        raise typer.Exit(1)
    if viz is not None and no_llm:
        console.print("[red]Error:[/] --viz runs visual reasoning and cannot be used with --no-llm")
        raise typer.Exit(1)
    if greybox_plus and viz is None:
        console.print("[red]Error:[/] --greybox-plus requires --viz <reference-image>")
        raise typer.Exit(1)
    if greybox_plus:
        geometry_only = True

    if not prompt and (reference_image is not None or viz is not None):
        # The Concept stage still needs a user message for the OpenAI-compatible
        # chat contract, but this carrier deliberately contributes no semantic
        # game brief. The image remains the only creative input.
        prompt = (
            "Create a complete game from the attached visual reference image. "
            "Treat the image as the only creative input."
        )

    if not prompt:
        console.print("[red]Error:[/] provide a prompt, use --file <path>, or pass --image/--viz <path>")
        console.print("  Example: python main.py unity-create \"A 2D platformer\"")
        console.print("  Example: python main.py unity-create --file prompts/neon_fugitive.txt")
        console.print("  Example: python main.py unity-create --image racing-reference.png")
        raise typer.Exit(1)

    route_data = None
    if route is not None:
        if not route.exists():
            console.print(f"[red]Error:[/] route file not found: {route}")
            raise typer.Exit(1)
        route_data = json.loads(route.read_text(encoding="utf-8"))
        if not route_data.get("path_points"):
            console.print(f"[red]Error:[/] {route} has no path_points — "
                          "generate it with: python main.py unity-path ...")
            raise typer.Exit(1)

    cfg = _cfg(config, model)

    scene_data = None
    if scene is not None:
        from src.unity_sceneinput import SceneInputError
        try:
            scene_data = _load_authored_scene(scene, models_dir, cfg)
        except SceneInputError as exc:
            console.print(f"[red]Error:[/] {exc}")
            raise typer.Exit(1)

    try:
        visual_context = None
        greybox_plus_reference = None
        if viz is not None:
            if greybox_plus:
                # Preserve the caller's source image as Greybox+ provenance.
                # `_prepare_viz_reasoning` returns a normalized visual board for
                # the Concept stage, which is not the same evidence artifact.
                greybox_plus_reference = viz
            reference_image, context_path = _prepare_viz_reasoning(
                viz, viz_output, cfg, overwrite=viz_overwrite
            )
            visual_context = context_path.read_text(encoding="utf-8")
        UnityPipeline(cfg).run(prompt, theme=theme, mashup_with=mashup_with, seed=seed, compose=compose, geometry_only=geometry_only, greybox_plus=greybox_plus, greybox_plus_reference_image=greybox_plus_reference, blender_models=blender_models, route=route_data, scene=scene_data, no_think=no_think, micro_code=micro_code, no_llm=no_llm, reference_image=reference_image, visual_context=visual_context)
    except VisualReasoningCompilerError as exc:
        console.print(f"[red]Visual reasoning error:[/] {exc}")
        raise typer.Exit(2)
    except ReferenceImageError as exc:
        console.print(f"[red]Reference image error:[/] {exc}")
        raise typer.Exit(2)
    except BriefParseError as exc:
        # Abstain, don't half-fill: the brief is too thin to compile offline and
        # the message names every missing section plus the remedy.
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2)


@app.command("unity-path")
def unity_path(
    points: Optional[str]  = typer.Option(None, "--points", "-p",
                                          help="Point spec: 'x,z; x,z; ...' — 2, 3 or 4 numbers per point (x,z | x,y,z | x,y,z,bank°), meters"),
    file:   Optional[Path] = typer.Option(None, "--file", "-f",
                                          help="Text file containing the same point spec (';' or newline separated)"),
    image:  Optional[Path] = typer.Option(None, "--image", "-i",
                                          help="Top-down drawing of the course (dark line on light background, or inverted)"),
    track:  Optional[str]  = typer.Option(None, "--track",
                                          help="Generate a procedural race circuit instead of authoring points. Style: flowing | technical | high_speed | kart | random"),
    seed:   int            = typer.Option(42, "--seed",
                                          help="--track only: seed for the procedural circuit (reproducible)"),
    width:  float          = typer.Option(10.0, "--width", "-w",
                                          help="Track ribbon width in meters (path_width)"),
    world_size: float      = typer.Option(160.0, "--world-size",
                                          help="Image only: meters the drawing's larger extent maps to"),
    closed: Optional[bool] = typer.Option(None, "--closed/--open",
                                          help="Force a closed loop / open course (default: auto-detect near endpoints)"),
    out:    Path           = typer.Option(Path("routes.json"), "--out", "-o",
                                          help="Route file to write"),
) -> None:
    """Author a P1-normalized route file (<= 64 points) from TEXT points or an IMAGE.

    Deterministic, LLM-free. The emitted route already honors the P1
    invariants: path_points[0] is the start line, sits at the world origin,
    and the direction of travel heads +Z. Feed it to unity-create:

    \b
      python main.py unity-path --points "0,0; 60,0; 80,40; 40,80; 0,60" --closed
      python main.py unity-path --file mytrack.txt --width 12 -o routes.json
      python main.py unity-path --image sketch.png --world-size 200 -o routes.json
      python main.py unity-path --track flowing --seed 7 -o routes.json
      python main.py unity-create "desert rally at dusk" --route routes.json
    """
    from src.unity_pathinput import build_route, parse_points_text, trace_image
    from src.unity_trackgen import STYLES, generate_track_route

    sources = [s for s in (points, file, image, track) if s is not None]
    if len(sources) != 1:
        console.print("[red]Error:[/] provide exactly one of --points / --file / --image / --track")
        raise typer.Exit(1)

    if track is not None:
        # Procedural circuit: the generator emits the SAME canonical route dict
        # (build_route → normalize) as authored/traced input, so it flows into
        # `unity-create --route` unchanged.
        if track not in STYLES:
            console.print(f"[red]Error:[/] unknown --track style {track!r}; "
                          f"choose one of {', '.join(STYLES)}")
            raise typer.Exit(1)
        route = generate_track_route(style=track, seed=seed, width=width)
        source = route["source"]
    else:
        try:
            if image is not None:
                if not image.exists():
                    console.print(f"[red]Error:[/] image not found: {image}")
                    raise typer.Exit(1)
                pts = trace_image(str(image), world_size=world_size)
                source = f"image:{image.name}"
            else:
                spec = points if points is not None else None
                if spec is None:
                    assert file is not None, "exactly one of --points/--file/--image/--track is guaranteed above"
                    if not file.exists():
                        console.print(f"[red]Error:[/] file not found: {file}")
                        raise typer.Exit(1)
                    spec = file.read_text(encoding="utf-8")
                    source = f"text:{file.name}"
                else:
                    source = "text:inline"
                pts = parse_points_text(spec)
        except ImportError:
            console.print("[red]Error:[/] --image needs Pillow: pip install pillow")
            raise typer.Exit(1)
        except ValueError as exc:
            console.print(f"[red]Error:[/] {exc}")
            raise typer.Exit(1)

        route = build_route(pts, width=width, close=closed, source=source)

    out.write_text(json.dumps(route, indent=2), encoding="utf-8")
    n = len(route["path_points"])
    kind = "closed loop" if route["closed"] else "open course"
    console.print(f"[green]OK[/]  {source} -> [bold]{out}[/]  "
                  f"({n} points, {kind}, {width:g} m wide; P1 at origin heading +Z)")
    console.print(f"  next: python main.py unity-create \"your prompt\" --route {out}")


@app.command("unity-terrain-gen")
def unity_terrain_gen(
    bounds: str            = typer.Option(..., "--bounds", "-b",
                                          help="Region bounds in meters: 'xmin,zmin,xmax,zmax'"),
    biome:  str             = typer.Option("grassland", "--biome",
                                          help="Ground texture set — see src/unity_terraingen.py BIOME_LAYERS "
                                               "(unknown biomes fall back to grass/dirt/rock)"),
    name:   str             = typer.Option("Region", "--name", "-n", help="Region name (Resources/scene folder name)"),
    seed:   Optional[int]   = typer.Option(None, "--seed",
                                          help="Explicit seed for reproducible terrain (default: derived from --name)"),
    resolution: int         = typer.Option(129, "--resolution", "-r",
                                          help="Heightmap resolution — must be 2^n + 1 (33/65/129/257/513/...)"),
    height_scale: float     = typer.Option(30.0, "--height-scale", help="Max terrain height in meters"),
    scatter_details: bool   = typer.Option(False, "--scatter-details",
                                          help="Also scatter a detail prefab across the region at Start"),
    scatter_prefab: str     = typer.Option("Rock", "--scatter-prefab", help="Resources/Terrain/<prefab> to scatter"),
    scatter_count: int      = typer.Option(20, "--scatter-count", help="How many detail instances to scatter"),
    out:    Path            = typer.Option(Path("output/unity/terrain"), "--out", "-o",
                                          help="Assets-root directory to write Terrain/<name>/ under"),
) -> None:
    """Bake a deterministic Unity Terrain (heightmap + splatmap) for one region — no LLM.

    Writes Assets/Terrain/<name>/{Resources,Scripts,README.md} under --out. Copy
    that Terrain/<name>/ folder straight into a project's Assets/ (or point
    --out at an existing project's Assets/ directly) and attach the generated
    <Name>TerrainBuilder to an empty GameObject.

    \b
      python main.py unity-terrain-gen --bounds "0,0,200,200" --biome forest -o MyGame/Assets
      python main.py unity-terrain-gen --bounds "0,0,120,120" --biome desert --seed 7 --scatter-details
    """
    from src.unity_terraingen import TerrainSpec, VALID_RESOLUTIONS, seed_for_region
    from src.micro.terrain import write_terrain_files

    try:
        parts = [float(p.strip()) for p in bounds.split(",")]
        if len(parts) != 4:
            raise ValueError("expected exactly 4 comma-separated numbers")
        xmin, zmin, xmax, zmax = parts
    except ValueError as exc:
        console.print(f"[red]Error:[/] --bounds {bounds!r} — {exc} (want \"xmin,zmin,xmax,zmax\")")
        raise typer.Exit(1)
    if xmax <= xmin or zmax <= zmin:
        console.print(f"[red]Error:[/] --bounds {bounds!r} is degenerate (xmax must be > xmin, zmax > zmin)")
        raise typer.Exit(1)
    if resolution not in VALID_RESOLUTIONS:
        console.print(f"[red]Error:[/] --resolution {resolution} must be one of {VALID_RESOLUTIONS} (2^n + 1)")
        raise typer.Exit(1)

    spec = TerrainSpec(
        name=name, bounds=(xmin, zmin, xmax, zmax), biome=biome.strip().lower(),
        seed=seed if seed is not None else seed_for_region(name),
        resolution=resolution, height_scale=height_scale,
    )
    n = write_terrain_files(spec, out, scatter_details=scatter_details,
                            scatter_prefab=scatter_prefab, scatter_count=scatter_count)
    console.print(f"[green]OK[/]  {name} ({biome}, seed {spec.seed}) -> "
                  f"[bold]{out / 'Terrain' / name}[/]  ({n} files)")
    console.print(f"  next: copy Terrain/{name}/ into your project's Assets/, "
                  f"attach {name}TerrainBuilder to an empty GameObject")


@app.command("unity-scene")
def unity_scene(
    layout: Path            = typer.Argument(..., help="Scene Compiler layout: layouts/<name>.json"),
    models_dir: Optional[Path] = typer.Option(None, "--models-dir", "-m",
                                              help="The Scene Compiler's models/ folder (default: the layout's own ../models sibling)"),
    config: str             = typer.Option("config.yaml", "--config", "-c"),
    convert: bool           = typer.Option(True, "--convert/--no-convert",
                                           help="Convert GLB/glTF items to FBX via headless Blender (cached). --no-convert leaves them for inspection only"),
    out:    Optional[Path]  = typer.Option(None, "--out", "-o",
                                           help="Write the canonical scene file here (default: print only)"),
) -> None:
    """Normalize a Scene Compiler layout into a scene file for `unity-create --scene`.

    Deterministic, LLM-free. Converts three.js coordinates to Unity, groups
    repeated models into one entity each with baked per-instance positions and
    yaws, and converts GLB items to FBX.

    \b
      python main.py unity-scene "C:/Users/me/Documents/Blender/layouts/MONACO.json"
      python main.py unity-scene layouts/MONACO.json -o scene.json
      python main.py unity-create --file prompts/race.txt --scene scene.json
    """
    from src.unity_sceneinput import (build_scene, convert_gltf_groups,
                                      describe, load_layout, SceneInputError)
    cfg = _cfg(config)
    try:
        root = models_dir if models_dir is not None else None
        if root is None:
            from src.unity_sceneinput import default_models_dir
            root = default_models_dir(layout)
        scene, warnings = build_scene(load_layout(layout), root,
                                      layout_path=layout)
        for w in warnings:
            console.print(f"[yellow]![/] {w}")
        if convert:
            for msg in convert_gltf_groups(scene, _scene_cache_dir(cfg),
                                           cfg.get("blender")):
                console.print(f"[dim]{msg}[/]")
    except SceneInputError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)

    for line in describe(scene):
        console.print(f"  {line}")
    n_inst = sum(g["count"] for g in scene["groups"])
    console.print(f"[green]OK[/]  {layout.name} -> {len(scene['groups'])} group(s), "
                  f"{n_inst} instance(s)")
    if out is not None:
        out.write_text(json.dumps(scene, indent=2), encoding="utf-8")
        console.print(f"  wrote [bold]{out}[/]")
        console.print(f"  next: python main.py unity-create \"your prompt\" --scene {out}")
    else:
        console.print(f"  next: python main.py unity-create \"your prompt\" --scene {layout}")


@app.command("unity-resolve")
def unity_resolve(
    description:   str = typer.Argument(...,      help="e.g. 'universal render pipeline'"),
    unity_version: str = typer.Argument("6000.0", help="6000.0 | 6000.1 | 2022.3 | 2021.3"),
    config:        str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Resolve one Unity package description to a Package Manager ID + version."""
    cfg      = _cfg(config)
    resolver = UnityResolver(lookups_dir=_unity_pipe_cfg(cfg).get("lookups_dir", "lookups"))
    pkg_id, version = resolver.resolve_package(description, unity_version)
    if pkg_id:
        console.print(f"[cyan]{description}[/] -> [bold]{pkg_id}[/] "
                      f"[dim]{version or '(no version)'}[/]")
    else:
        console.print(f"[yellow]No match found for[/] [cyan]{description}[/]")


@app.command("unity-validate")
def unity_validate(
    schema_file:    str  = typer.Argument(..., help="Path to a Unity schema JSON file"),
    config:         str  = typer.Option("config.yaml", "--config", "-c"),
    blender_models: bool = typer.Option(False, "--blender-models",
                                        help="Build Blender FBX models (player/enemy/vehicle) "
                                             "and wire them into the regenerated project"),
    compose:        bool = typer.Option(False, "--compose",
                                        help="Recompile a project that was originally built "
                                             "with `unity-create --compose`: reconstruct + "
                                             "rewrite Assets/Micros/** (deterministic — see "
                                             "the command's own docstring for what this can "
                                             "and cannot recover)."),
    spatial_fusion_file: Optional[str] = typer.Option(
        None,
        "--spatial-fusion",
        help="Replay a captured closed spatial-fusion artifact through the "
             "deterministic generation path",
    ),
) -> None:
    """Recompile a saved Unity schema to a project folder without re-running the LLM.

    \b
    `--compose` note: `subsystems_for()` and `MicroComposer.plan()/emit()` are
    pure functions of the schema, so this reconstructs Assets/Micros/** and the
    scene wiring byte-for-byte deterministically — no LLM call. Per-micro C#
    BODIES are a different story: the original `unity-create --compose` run's
    `enrich_subsystems` step writes its output into `Subsystem.options` in
    memory only. Since this schema.json's write, that output round-trips
    through `schema.microgens[].input` (merge_authored_code reads it back as
    the no-LLM fallback) — but a schema saved by an OLDER run predates that
    persistence and only carries whatever the schema-authoring step itself
    proposed for `schema.microgens`, which is not the same content and does
    not cover every composed kind. Any subsystem this recompile cannot find
    code for is reported below and falls back to its deterministic baseline
    body — still correct, just not concept-specific.
    """
    cfg      = _cfg(config)
    pipe_cfg = _unity_pipe_cfg(cfg)
    data     = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    schema   = UnitySchema.model_validate(data)
    # Compiler-owned projectile metadata is derived from the frozen raw brief,
    # not authored by the schema model.  Refresh it on replay so a saved
    # pre-Slice-4 schema observes current deterministic lowering in the same
    # way as a fresh unity-create run.
    from src.unity_projectile_contract import apply_physical_projectile_contract

    _saved_meta = schema.meta if isinstance(schema.meta, dict) else {}
    for msg in apply_physical_projectile_contract(
        schema, str(_saved_meta.get("user_prompt", ""))
    ):
        console.print(f"  [magenta]FIX[/]  {msg}")
    spatial_fusion: dict | None = None
    spatial_replay_world: dict | None = None
    # The command is also called directly by CLI regression tests. Typer's
    # OptionInfo sentinel is not a path when the new optional argument is
    # omitted from such a direct call.
    if isinstance(spatial_fusion_file, str):
        from src.unity_spatial_fusion import (
            SpatialFusionError,
            replay_frozen_spatial_fusion,
        )

        try:
            frozen_fusion = json.loads(
                Path(spatial_fusion_file).read_text(encoding="utf-8")
            )
            spatial_fusion = replay_frozen_spatial_fusion(schema, frozen_fusion)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SpatialFusionError) as exc:
            raise typer.BadParameter(
                f"invalid frozen spatial fusion: {exc}"
            ) from exc
        console.print(
            "[green]OK[/]  replayed frozen spatial fusion with no LLM or network"
        )
    _acceptance = cfg.get("acceptance", {}) or {}
    _normalization_issues = normalization_issues(schema)
    if (_normalization_issues
            and _acceptance.get("reject_lossy_normalization", False)):
        _fields = ", ".join(
            str(issue.get("field", "unknown")) for issue in _normalization_issues
        )
        raise typer.BadParameter(
            "schema records lossy normalization for: " + _fields
            + "; correct the source IR or disable acceptance.reject_lossy_normalization"
        )
    resolver = UnityResolver(lookups_dir=pipe_cfg.get("lookups_dir", "lookups"))
    schema   = resolver.resolve_schema(schema)
    schema   = resolver.inject_required_packages(schema)
    # A saved schema carries the previous layout output. Replaying the
    # deterministic spatial contract here is required for `unity-validate` to
    # observe layout/compiler fixes made after that schema was authored (and is
    # what a fresh unity-create run already did in Step 5). Authored Scene
    # Compiler transforms remain protected by compute_layout's authored-scene
    # boundary; generated transforms are rebuilt, never hand-edited.
    if spatial_fusion is None:
        from src.unity_layout import compute_layout
        for msg in compute_layout(schema):
            console.print(f"  [cyan]LAYOUT[/]  {msg}")
    else:
        from src.scene_graph import build_scene_graph
        from src.unity_spatial_realization import (
            SpatialRealizationError,
            realize_spatial_layout,
        )
        from src.unity_spatial_roles import derive_spatial_roles

        spatial_roles = derive_spatial_roles(schema)
        spatial_graph = build_scene_graph(schema, spatial_roles)
        try:
            spatial_replay_world = realize_spatial_layout(
                schema,
                relation_graph=spatial_graph,
                spatial_fusion=spatial_fusion,
            )
        except SpatialRealizationError as exc:
            raise typer.BadParameter(
                f"frozen spatial fusion could not be realized: {exc}"
            ) from exc
        for msg in spatial_replay_world["layout"]["messages"]:
            console.print(f"  [cyan]LAYOUT[/]  {msg}")
    deps     = resolver.build_manifest_deps(
        schema, inject_mcp_bridge=pipe_cfg.get("inject_mcp_bridge", True)
    )

    # Mission -> scene inference (see _infer_mission_scenes's docstring).
    # `unity-validate` skips `_check_consistency`/Step 5 entirely, so this
    # deterministic, LLM-free pass must be re-run here too or a saved schema
    # recompiled through this command would never get the fix a fresh
    # `unity-create` run already applies — SEAM 8's per-scene wiring (below,
    # only when --compose) and EntitySpawner's per-scene entity groups both
    # depend on schema.meta["mission_scenes"] being set.
    from src.unity_pipeline import (_infer_mission_scenes, _objective_scene_map,
                                    _reconcile_scene_specs)
    _mission_scenes, _unmapped_missions = _infer_mission_scenes(schema)
    if isinstance(schema.meta, dict):
        schema.meta["mission_scenes"] = _mission_scenes
    if _unmapped_missions:
        _titles = {m.id: m.title for m in schema.missions}
        console.print(
            "[yellow]WARN[/]  mission_scenes: could not infer a scene for "
            "mission(s) " + ", ".join(_titles.get(mid, mid) for mid in _unmapped_missions)
            + " — their objectives keep the global (ungated) SEAM 8 binding")
    if _mission_scenes:
        schema, _mission_scene_fixes = _reconcile_scene_specs(schema)
        for msg in _mission_scene_fixes:
            console.print(f"  [magenta]FIX[/]  {msg}")

    model_overrides: dict = {}
    if blender_models:
        from src.unity_pipeline import UnityPipeline
        concept = schema.meta.get("concept", "") if schema.meta else ""
        model_overrides = UnityPipeline(cfg)._blender_model_overrides(schema, concept)

    if not compose and isinstance(schema.meta, dict) and schema.meta.get("wiring"):
        console.print(
            "[yellow]WARN[/]  schema.meta['wiring'] is set — this project looks like it "
            "was built with `--compose`, but `unity-validate` was run without `--compose`. "
            "Assets/Micros/** will NOT be regenerated and will go stale/frozen at whatever "
            "was already on disk. Re-run with --compose to reconstruct them."
        )

    template_dir = pipe_cfg.get("unity_template_dir", "templates/unity")
    gen      = UnityProjectGenerator(template_dir=template_dir)
    output_dir = pipe_cfg.get("unity_output_dir", "output/unity")
    _asset_cfg = cfg.get("assets", {}) or {}
    _sketchfab_cfg = dict(cfg.get("sketchfab", {}) or {})
    if _sketchfab_cfg.get("auto_fetch"):
        _sketchfab_cfg["auto_fetch"] = False
        console.print(
            "[yellow]WARN[/]  disabled sketchfab.auto_fetch for deterministic "
            "unity-validate replay"
        )
    if _sketchfab_cfg.get("vision_select"):
        _sketchfab_cfg["vision_select"] = False

    try:
        _collection_cache, _collection_bindings, _collection_entries = _frozen_collection_replay(schema)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid frozen Sketchfab collection: {exc}") from exc
    if _collection_cache is not None:
        console.print(
            f"[green]OK[/]  verified {len(_collection_entries)} frozen Sketchfab asset(s) "
            f"and {len(_collection_bindings)} entity binding(s) for replay"
        )

    composed_units = None
    composer = None
    subsystems: list = []
    if compose:
        from src.unity_compose import (MicroComposer, drop_owned_scripts,
                                       refresh_cover_points,
                                       refresh_projectile_visuals,
                                       subsystems_for)
        from src.unity_micro_code import CODE_KEYS, merge_authored_code
        from src.unity_pipeline import _compose_palette_rgb
        from src.unity_projectile_contract import PHYSICAL_PROJECTILE_META_KEY

        _meta = schema.meta if isinstance(schema.meta, dict) else {}
        composer = MicroComposer(schema.project_name, template_dir=template_dir,
                                 project_config=cfg)
        subsystems = subsystems_for(
            schema.game_type, schema.features, locomotion=schema.locomotion,
            capabilities=schema.capabilities, user_prompt=_meta.get("user_prompt", ""),
            scenes=schema.scenes, entities=schema.entities, scripts=schema.scripts,
            genre=_meta.get("genre", "universal"), audio_specs=schema.audio_specs,
            vfx_specs=schema.vfx, shaders=schema.shaders, animations=schema.animations,
            characters=schema.characters, missions=schema.missions,
            behavior_trees=schema.behavior_trees, environment=schema.environment,
            visual_style=schema.visual_style, render_pipeline=schema.render_pipeline,
            palette_rgb=_compose_palette_rgb(schema),
            projectile_contract=_meta.get(PHYSICAL_PROJECTILE_META_KEY),
        )
        # No-LLM fallback only — see the docstring above for what this can and
        # cannot recover relative to the original `enrich_subsystems` call.
        for msg in merge_authored_code(subsystems, schema.microgens):
            console.print(f"  [magenta]FIX[/]  {msg}")
        _baseline = sorted(sub.kind for sub in subsystems
                           if not any(sub.options.get(k) for k in CODE_KEYS))
        if _baseline:
            console.print(
                f"[yellow]WARN[/]  no authored code recovered for: {', '.join(_baseline)} — "
                "these subsystem(s) will use their deterministic baseline body, not "
                "whatever the original --compose run's enrich_subsystems authored."
            )

        composed_units = composer.plan(subsystems)
        before = len(schema.scripts)
        schema.scripts = drop_owned_scripts(schema.scripts, composed_units)
        if before != len(schema.scripts):
            console.print(f"  [magenta]FIX[/]  composition owns {before - len(schema.scripts)} "
                          "subsystem(s) — dropped duplicate built-in/name-colliding scripts")
        # Same stamp unity_pipeline.run() leaves after its own drop_owned_scripts
        # call (see that comment) — without it, a `unity-playability` run against
        # THIS command's rewritten schema sidecar (below) would report the
        # ERROR-level "no player script" false positive again, because this
        # recompile just erased the same type="player" script that check reads.
        if isinstance(schema.meta, dict):
            schema.meta["composed_player_controller"] = any(
                u.kind == "controller" and u.target == "player" for u in composed_units)
        deps = {**deps, **composer.module_deps(composed_units)}
        if any(u.kind in ("input", "controller") for u in composed_units):
            _pkg_id, _pkg_ver = resolver.resolve_package("input system", schema.unity_version)
            if _pkg_id and _pkg_id not in deps:
                deps[_pkg_id] = _pkg_ver or "1.8.2"

    root, count = gen.write(schema, deps, output_dir,
                            composed_units=composed_units,
                            model_overrides=model_overrides or None,
                             strict_assets=bool(_asset_cfg.get("strict", True)),
                             asset_cfg=_asset_cfg, sketchfab_cfg=_sketchfab_cfg,
                             project_config=cfg,
                             locked_asset_bindings=_collection_bindings or None,
                             spatial_fusion=spatial_fusion)

    if spatial_fusion is not None and spatial_replay_world is not None:
        (root / f"{schema.project_name}.spatial-fusion.json").write_text(
            json.dumps(spatial_fusion, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (root / f"{schema.project_name}.world-layout.json").write_text(
            json.dumps(spatial_replay_world, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        console.print(
            "[green]OK[/]  wrote frozen spatial replay evidence -> "
            "*.spatial-fusion.json, *.world-layout.json"
        )

    if _collection_cache is not None:
        from src.unity_sketchfab_collection import copy_collection_into_project
        copied = copy_collection_into_project(
            _collection_entries, _collection_cache, root / "Assets",
        )
        if copied != len(_collection_entries):
            raise RuntimeError(
                "verified frozen Sketchfab collection could not be copied into the replay"
            )

    if compose and composer is not None and composed_units is not None:
        has_game_manager = any(s.type == "game_manager" for s in (schema.scripts or []))
        # Names entities resolve_escort_follow (Step 5, in the ORIGINAL
        # unity-create run that produced this saved schema) gave a real
        # Follow-atom behavior to — SEAM 8b's honest signal that this entity
        # actually walks toward the player, not just a name an escort
        # objective happens to mention. unity-validate skips Step 5 itself,
        # but that resolution is already baked into the loaded schema's
        # entities, so reading it back here (not re-running the resolver)
        # is exactly what unity_pipeline.run() does at this same point.
        # Missing before now: a schema with `escort` objectives recompiled
        # via `unity-validate --compose` silently lost SEAM 8b entirely.
        escort_followed_entities = {
            e.name for e in schema.entities
            if e.name and e.behavior
            and any(getattr(a, "atom", "") == "Follow" for a in (e.atoms or []))
        }
        # Every named entity, spawn count included — SEAM 8c's ground truth
        # for whether a `reach` objective's target_ref is a real, locatable
        # GameObject it can attach a relay to (see unity_compose.py's
        # WireReachTargetRelay pass). Same set unity_pipeline.run() builds.
        reach_target_entities = {e.name: e.count for e in schema.entities if e.name}
        # Same re-point unity_pipeline.run() does at this point: gen.write()
        # may have re-laid out the scene with measured footprints and re-baked
        # schema.meta["cover_points"]; the cover_shooter unit was built before
        # that. Before this call the recompile never handed the AI ANY cover
        # points — subsystems_for() above is not given the table.
        for msg in refresh_cover_points(
                subsystems, schema.entities,
                schema.meta.get("cover_points") if isinstance(schema.meta, dict) else None):
            console.print(f"  [magenta]FIX[/]  {msg}")
        for msg in refresh_projectile_visuals(
                subsystems,
                schema.meta.get(PHYSICAL_PROJECTILE_META_KEY)
                if isinstance(schema.meta, dict) else None):
            console.print(f"  [magenta]FIX[/]  {msg}")
        composer.emit(subsystems, root / "Assets",
                      has_game_manager=has_game_manager, wiring=schema.wiring,
                      objective_scenes=_objective_scene_map(
                          schema, schema.meta.get("mission_scenes")
                          if isinstance(schema.meta, dict) else None),
                      escort_followed_entities=escort_followed_entities,
                      reach_target_entities=reach_target_entities,
                      game_type=schema.game_type)
        if isinstance(schema.meta, dict):
            schema.meta["wiring"] = composer.wiring_report
        console.print(f"[green]OK[/]  composed {len(composed_units)} micro subsystem(s) "
                     "-> Assets/Micros")

    # Write schema sidecar so unity-model --unity-project can find and re-inject models.
    try:
        (root / f"{schema.project_name}.schema.json").write_text(
            schema.model_dump_json(indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        from src.unity_build_provenance import write_build_provenance
        provenance = write_build_provenance(
            schema,
            project_root=root,
            manifest_deps=deps,
            config=cfg,
            template_dir=template_dir,
            lookups_dir=pipe_cfg.get("lookups_dir", "lookups"),
            source_schema_path=Path(schema_file),
            status="replayed_unverified",
        )
        console.print(
            f"[yellow]WARN[/]  replay provenance is unverified -> {provenance.name}; "
            "run the configured spatial/playability acceptance path before certification"
        )
    except OSError as exc:
        console.print(f"[red]ERROR[/] could not write build provenance: {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]OK[/]  {count} files -> {root}")


@app.command("unity-migrate")
def unity_migrate(
    schema_file: str         = typer.Argument(..., help="Path to a Unity .schema.json file"),
    in_place:    bool        = typer.Option(False, "--in-place", "-i",
                                            help="Overwrite the source file"),
    output:      Optional[str] = typer.Option(None, "--output", "-o",
                                              help="Write migrated schema to this path"),
    dry_run:     bool        = typer.Option(False, "--dry-run",
                                            help="Print the migrated JSON without writing"),
) -> None:
    """Upgrade a data-first (v1) schema.json to a capability-aware (v2) schema.

    Synthesises a `capabilities` block from the existing `features` list and
    `locomotion` field.  Idempotent — already-migrated schemas pass through unchanged.

    \b
    Examples:
      python main.py unity-migrate MyGame.schema.json
      python main.py unity-migrate MyGame.schema.json --in-place
      python main.py unity-migrate MyGame.schema.json --output MyGame.v2.schema.json
    """
    from src.capability_resolver import migrate_schema_v1_to_v2

    src = Path(schema_file)
    if not src.exists():
        console.print(f"[red]Error:[/] file not found: {src}")
        raise typer.Exit(1)

    data    = json.loads(src.read_text(encoding="utf-8"))
    updated = migrate_schema_v1_to_v2(data)

    if "capabilities" not in data:
        added_caps = updated.get("capabilities", {})
        console.print(f"[green]Migrated[/] — added capabilities block: "
                      f"{list(added_caps.keys())}")
    else:
        console.print("[yellow]Already v2[/] — capabilities block already present, no changes")

    if dry_run:
        console.print(json.dumps(updated, indent=2))
        return

    if output:
        dest = Path(output)
    elif in_place:
        dest = src
    else:
        dest = src.with_suffix("").with_suffix(".v2.schema.json")

    dest.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    console.print(f"[green]Written[/] → {dest}")
