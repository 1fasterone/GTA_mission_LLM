"""Unity QA/evaluation commands: playability / stress-test / fidelity / eval."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from src.unity_schema import UnitySchema

from .common import _cfg, app, console


@app.command("unity-vizconcept-board")
def unity_vizconcept_board(
    vizconcept_file: Path = typer.Option(
        ..., "--vizconcept-file", help="Visual concept prompt sent to ComfyUI"
    ),
    output: Path = typer.Option(..., "--output", "-o", help="Board PNG destination"),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Provenance manifest destination"
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
    checkpoint: str = typer.Option(
        "sd15ModelLab_realV001_1651730.safetensors", "--checkpoint"
    ),
    seed: int = typer.Option(978075547277410, "--seed"),
    width: int = typer.Option(768, "--width"),
    height: int = typer.Option(512, "--height"),
    steps: int = typer.Option(20, "--steps"),
    cfg_scale: float = typer.Option(8.0, "--cfg"),
    sampler: str = typer.Option("dpmpp_2m_sde_gpu", "--sampler"),
    scheduler: str = typer.Option("karras", "--scheduler"),
) -> None:
    """Generate Slice 1: vizconcept.txt -> canonical ComfyUI board."""
    from src.visual_reasoning_compiler import (
        ComfyVizConceptClient,
        VisualReasoningCompilerError,
    )

    if not vizconcept_file.is_file():
        console.print(f"[red]vizconcept file not found:[/] {vizconcept_file}")
        raise typer.Exit(2)
    if not config.is_file():
        console.print(f"[red]config file not found:[/] {config}")
        raise typer.Exit(2)
    try:
        cfg = _cfg(str(config))
        diffusion = ((cfg.get("blender") or {}).get("diffusion") or {})
        client = ComfyVizConceptClient(
            base_url=str(diffusion.get("base_url", "http://127.0.0.1:8188")),
            timeout=float(diffusion.get("timeout", 180)),
            poll_timeout=float(diffusion.get("poll_timeout", 900)),
        )
        manifest_path = manifest or output.with_suffix(".comfy.json")
        try:
            path = client.generate(
                vizconcept_file=vizconcept_file,
                output=output,
                manifest=manifest_path,
                checkpoint=checkpoint,
                seed=seed,
                width=width,
                height=height,
                steps=steps,
                cfg=cfg_scale,
                sampler=sampler,
                scheduler=scheduler,
            )
        finally:
            freed = client.free_models()
        console.print(f"[green]Visual reasoning board written[/] {path}")
        console.print(f"[green]Provenance manifest written[/] {manifest_path}")
        console.print(f"[green]Comfy models freed:[/] {freed}")
    except (OSError, ValueError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual reasoning board error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-import")
def unity_vizconcept_import(
    board_image: Path = typer.Option(
        ..., "--board-image", help="Existing PNG to import as the visual board"
    ),
    output: Path = typer.Option(..., "--output", "-o", help="Imported board destination"),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Import provenance manifest destination"
    ),
    vizconcept_file: Optional[Path] = typer.Option(
        None, "--vizconcept-file", help="Optional visual concept linked to this board"
    ),
) -> None:
    """Import a known-good render without regenerating it through ComfyUI."""
    from src.visual_reasoning_compiler import (
        VisualReasoningCompilerError,
        import_existing_board,
    )

    try:
        manifest_path = manifest or output.with_suffix(".import.json")
        path = import_existing_board(
            board_image=board_image,
            output=output,
            manifest=manifest_path,
            vizconcept_file=vizconcept_file,
        )
        console.print(f"[green]Visual reasoning board imported[/] {path}")
        console.print(f"[green]Import manifest written[/] {manifest_path}")
    except (OSError, ValueError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual reasoning board import error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-reason")
def unity_vizconcept_reason(
    board_image: Path = typer.Option(
        ..., "--board-image", help="Canonical Slice-1 board PNG"
    ),
    inventory_output: Path = typer.Option(
        ..., "--inventory-output", "--inventory",
        help="Image-derived visual inventory JSON destination",
    ),
    concept_output: Path = typer.Option(
        ..., "--concept-output", "--concept",
        help="Image-derived Concept.txt destination",
    ),
    context_output: Optional[Path] = typer.Option(
        None, "--context-output",
        help="Image-grounded Step-2 context destination (default: context.txt beside Concept)",
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-2 provenance manifest destination"
    ),
    board_manifest: Optional[Path] = typer.Option(
        None, "--board-manifest",
        help="Optional Slice-1 manifest to hash-check and link",
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """Run Slice 2: board PNG -> visual inventory + Concept.txt."""
    from src.backends import make_chat_client
    from src.visual_reasoning_compiler import (
        VisualReasoningCompilerError,
        reason_over_board,
    )

    if not board_image.is_file():
        console.print(f"[red]Visual reasoning board not found:[/] {board_image}")
        raise typer.Exit(2)
    if not config.is_file():
        console.print(f"[red]Config file not found:[/] {config}")
        raise typer.Exit(2)
    llm = None
    try:
        cfg = _cfg(str(config))
        llm = make_chat_client(cfg)
        manifest_path = manifest or concept_output.with_suffix(".reasoning.json")
        context_path = context_output or concept_output.parent / "context.txt"
        path = reason_over_board(
            board_image=board_image,
            inventory_output=inventory_output,
            concept_output=concept_output,
            manifest=manifest_path,
            board_manifest=board_manifest,
            context_output=context_path,
            llm=llm,
        )
        console.print(f"[green]Visual inventory written[/] {inventory_output}")
        console.print(f"[green]Image-derived Concept.txt written[/] {concept_output}")
        console.print(f"[green]Visual context written[/] {context_path}")
        console.print(f"[green]Slice-2 provenance manifest written[/] {path}")
    except (OSError, ValueError, RuntimeError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual reasoning error:[/] {exc}")
        raise typer.Exit(2) from exc
    finally:
        if llm is not None and hasattr(llm, "unload"):
            llm.unload()


@app.command("unity-vizconcept-spatial")
def unity_vizconcept_spatial(
    board_image: Path = typer.Option(
        ..., "--board-image", help="Canonical Slice-1 board PNG"
    ),
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Closed Slice-3 image IR JSON"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-6.1 image spatial evidence JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.1 provenance manifest destination"
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """Run Slice 6.1: board pixels -> frozen image-space evidence."""
    from src.backends import make_chat_client
    from src.visual_spatial_extraction import (
        VisualSpatialExtractionError,
        extract_image_spatial_evidence,
    )

    if not board_image.is_file():
        console.print(f"[red]Spatial board not found:[/] {board_image}")
        raise typer.Exit(2)
    if not closed_ir.is_file():
        console.print(f"[red]Closed image IR not found:[/] {closed_ir}")
        raise typer.Exit(2)
    if not config.is_file():
        console.print(f"[red]Config file not found:[/] {config}")
        raise typer.Exit(2)
    llm = None
    try:
        cfg = _cfg(str(config))
        llm = make_chat_client(cfg)
        manifest_path = manifest or output.with_suffix(".spatial.json")
        path = extract_image_spatial_evidence(
            board_image=board_image,
            closed_ir=closed_ir,
            output=output,
            manifest=manifest_path,
            llm=llm,
        )
        console.print(f"[green]Image spatial evidence written[/] {output}")
        console.print(f"[green]Slice-6.1 provenance manifest written[/] {path}")
    except (OSError, ValueError, RuntimeError, VisualSpatialExtractionError) as exc:
        console.print(f"[red]Image spatial extraction error:[/] {exc}")
        raise typer.Exit(2) from exc
    finally:
        if llm is not None and hasattr(llm, "unload"):
            llm.unload()


@app.command("unity-vizconcept-spatial-roles")
def unity_vizconcept_spatial_roles(
    schema_file: Path = typer.Option(
        ..., "--schema", help="Validated Unity .schema.json input"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-6.2 schema spatial-role JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.2 provenance manifest destination"
    ),
) -> None:
    """Run deterministic Slice 6.2: schema -> closed spatial roles."""
    from src.unity_spatial_roles import SpatialRoleError, write_spatial_roles

    if not schema_file.is_file():
        console.print(f"[red]Spatial-role schema not found:[/] {schema_file}")
        raise typer.Exit(2)
    try:
        path = write_spatial_roles(
            schema_path=schema_file,
            output=output,
            manifest=manifest,
        )
        console.print(f"[green]Schema spatial roles written[/] {output}")
        console.print(f"[green]Slice-6.2 provenance manifest written[/] {path}")
    except (OSError, ValueError, TypeError, SpatialRoleError) as exc:
        console.print(f"[red]Schema spatial-role error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-scenegraph")
def unity_scenegraph(
    schema_file: Path = typer.Argument(..., help="Validated Unity .schema.json input"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Slice-6.4 scene/relation graph JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.4 provenance manifest destination"
    ),
    spatial_roles: Optional[Path] = typer.Option(
        None, "--spatial-roles", help="Optional hash-linked Slice-6.2 role sidecar"
    ),
    dot: bool = typer.Option(False, "--dot", help="Also write a sibling Graphviz DOT view"),
    dot_output: Optional[Path] = typer.Option(
        None, "--dot-output", help="Graphviz DOT destination (implies --dot)"
    ),
) -> None:
    """Run deterministic Slice 6.4: schema -> scene/relation graph."""
    from src.scene_graph import SceneGraphError, write_scene_graph

    if not schema_file.is_file():
        console.print(f"[red]Scene-graph schema not found:[/] {schema_file}")
        raise typer.Exit(2)
    if spatial_roles is not None and not spatial_roles.is_file():
        console.print(f"[red]Spatial-role sidecar not found:[/] {spatial_roles}")
        raise typer.Exit(2)
    destination = output or schema_file.with_suffix(".scene-graph.json")
    dot_destination = dot_output or (
        destination.with_suffix(".dot") if dot else None
    )
    try:
        manifest_path = write_scene_graph(
            schema_path=schema_file,
            output=destination,
            manifest=manifest,
            spatial_roles_path=spatial_roles,
            dot_output=dot_destination,
        )
        console.print(f"[green]Scene/relation graph written[/] {destination}")
        console.print(f"[green]Slice-6.4 provenance manifest written[/] {manifest_path}")
        if dot_destination is not None:
            console.print(f"[green]Graphviz view written[/] {dot_destination}")
    except (OSError, ValueError, TypeError, SceneGraphError) as exc:
        console.print(f"[red]Scene-graph error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-spatial-fuse")
@app.command("unity-vizconcept-spatial-fuse")
def unity_spatial_fuse(
    schema_file: Path = typer.Option(
        ..., "--schema", help="Validated Unity .schema.json input"
    ),
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Closed Slice-3 image IR JSON"
    ),
    image_evidence: Path = typer.Option(
        ..., "--image-evidence", "--evidence",
        help="Frozen Slice-6.1 image spatial evidence JSON"
    ),
    spatial_roles: Path = typer.Option(
        ..., "--spatial-roles", "--roles",
        help="Frozen Slice-6.2 schema spatial roles JSON"
    ),
    evidence_manifest: Path = typer.Option(
        ..., "--evidence-manifest", help="Slice-6.1 evidence provenance manifest"
    ),
    roles_manifest: Path = typer.Option(
        ..., "--roles-manifest", help="Slice-6.2 roles provenance manifest"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-6.3 spatial fusion JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.3 provenance manifest destination"
    ),
) -> None:
    """Run deterministic Slice 6.3: image evidence + schema roles -> unit-free constraints."""
    from src.unity_spatial_fusion import SpatialFusionError, fuse_spatial_evidence

    for label, path in (
        ("schema", schema_file),
        ("closed image IR", closed_ir),
        ("image evidence", image_evidence),
        ("spatial roles", spatial_roles),
        ("evidence manifest", evidence_manifest),
        ("roles manifest", roles_manifest),
    ):
        if not path.is_file():
            console.print(f"[red]Spatial-fusion {label} not found:[/] {path}")
            raise typer.Exit(2)
    try:
        manifest_path = fuse_spatial_evidence(
            closed_ir=closed_ir,
            evidence=image_evidence,
            evidence_manifest=evidence_manifest,
            schema=schema_file,
            roles=spatial_roles,
            roles_manifest=roles_manifest,
            output=output,
            manifest=manifest,
        )
        console.print(f"[green]Hybrid spatial fusion written[/] {output}")
        console.print(f"[green]Slice-6.3 provenance manifest written[/] {manifest_path}")
    except (OSError, ValueError, TypeError, SpatialFusionError) as exc:
        console.print(f"[red]Spatial-fusion error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-spatial-realize")
def unity_spatial_realize(
    schema_file: Path = typer.Argument(..., help="Validated Unity .schema.json input"),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-6.5 world-layout JSON"
    ),
    footprints: Optional[Path] = typer.Option(
        None, "--footprints", help="Measured entity-footprint sidecar JSON"
    ),
    relation_graph: Optional[Path] = typer.Option(
        None, "--relation-graph", help="Hash-linked Slice-6.4 scene graph JSON"
    ),
    spatial_fusion: Optional[Path] = typer.Option(
        None, "--spatial-fusion", help="Hash-linked Slice-6.3 spatial fusion JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.5 provenance manifest destination"
    ),
) -> None:
    """Run deterministic Slice 6.5: frozen inputs -> Unity world layout."""
    from src.unity_spatial_realization import (
        SpatialRealizationError,
        write_spatial_realization,
    )

    if not schema_file.is_file():
        console.print(f"[red]Spatial-realization schema not found:[/] {schema_file}")
        raise typer.Exit(2)
    for label, path in (
        ("footprint", footprints),
        ("relation graph", relation_graph),
        ("spatial fusion", spatial_fusion),
    ):
        if path is not None and not path.is_file():
            console.print(f"[red]Spatial-realization {label} not found:[/] {path}")
            raise typer.Exit(2)
    try:
        manifest_path = write_spatial_realization(
            schema_path=schema_file,
            output=output,
            footprints_path=footprints,
            relation_graph_path=relation_graph,
            spatial_fusion_path=spatial_fusion,
            manifest=manifest,
        )
        console.print(f"[green]World layout written[/] {output}")
        console.print(f"[green]Slice-6.5 provenance manifest written[/] {manifest_path}")
    except (OSError, ValueError, TypeError, SpatialRealizationError) as exc:
        console.print(f"[red]Spatial-realization error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-spatial-validate")
def unity_spatial_validate(
    schema_file: Path = typer.Option(
        ..., "--schema", help="Validated Unity .schema.json input"
    ),
    world_layout: Path = typer.Option(
        ..., "--world-layout", help="Slice-6.5 world-layout JSON"
    ),
    spatial_roles: Path = typer.Option(
        ..., "--spatial-roles", help="Slice-6.2 schema spatial roles JSON"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-6.6 validation report JSON"
    ),
    spatial_fusion: Optional[Path] = typer.Option(
        None, "--spatial-fusion", help="Optional Slice-6.3 spatial fusion JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.6 provenance manifest destination"
    ),
) -> None:
    """Run deterministic Slice 6.6 spatial validation."""
    from src.unity_spatial_validation import (
        SpatialValidationError,
        write_spatial_validation,
    )

    for label, path in (
        ("schema", schema_file),
        ("world layout", world_layout),
        ("spatial roles", spatial_roles),
        ("spatial fusion", spatial_fusion),
    ):
        if path is not None and not path.is_file():
            console.print(f"[red]Spatial-validation {label} not found:[/] {path}")
            raise typer.Exit(2)
    try:
        manifest_path = write_spatial_validation(
            schema_path=schema_file,
            world_layout_path=world_layout,
            spatial_roles_path=spatial_roles,
            spatial_fusion_path=spatial_fusion,
            output=output,
            manifest=manifest,
        )
        report = json.loads(output.read_text(encoding="utf-8"))
        console.print(f"[green]Spatial validation written[/] {output}")
        console.print(f"[green]Slice-6.6 provenance manifest written[/] {manifest_path}")
        console.print(f"[green]Spatial validation status:[/] {report.get('status')}")
        if report.get("status") == "fail":
            raise typer.Exit(2)
    except (OSError, ValueError, TypeError, SpatialValidationError) as exc:
        console.print(f"[red]Spatial-validation error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-spatial-freeze")
def unity_spatial_freeze(
    project_root: Path = typer.Option(
        ..., "--project-root", help="Accepted generated Unity project directory"
    ),
    source_image: Path = typer.Option(
        ..., "--source-image", help="Original image supplied to the E2E run"
    ),
    reasoning_manifest: Path = typer.Option(
        ..., "--reasoning-manifest", help="Slice-2 image reasoning manifest"
    ),
    inventory_file: Path = typer.Option(
        ..., "--inventory", help="Slice-2 visual inventory JSON"
    ),
    concept_file: Path = typer.Option(
        ..., "--concept", help="Slice-2 image-derived Concept.txt"
    ),
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Slice-3 closed image IR JSON"
    ),
    reconciliation_manifest: Path = typer.Option(
        ..., "--reconciliation-manifest", help="Slice-3 reconciliation manifest"
    ),
    image_evidence: Path = typer.Option(
        ..., "--image-evidence", help="Slice-6.1 image spatial evidence JSON"
    ),
    image_evidence_manifest: Path = typer.Option(
        ..., "--image-evidence-manifest", help="Slice-6.1 evidence manifest"
    ),
    asset_fulfilment: Path = typer.Option(
        ..., "--asset-fulfilment", help="Slice-4 fulfilled asset JSON"
    ),
    asset_manifest: Path = typer.Option(
        ..., "--asset-manifest", help="Slice-4 asset fulfilment manifest"
    ),
    compile_manifest: Path = typer.Option(
        ..., "--compile-manifest", help="Slice-5 visual compile manifest"
    ),
    config: Optional[Path] = typer.Option(
        Path("config.yaml"), "--config", help="Config snapshot to hash"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Slice-6.7 freeze JSON destination"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.7 provenance manifest destination"
    ),
) -> None:
    """Freeze the complete lineage of an accepted deterministic spatial build."""
    from src.unity_spatial_freeze import SpatialFreezeError, freeze_spatial_build

    required = (
        ("project root", project_root),
        ("source image", source_image),
        ("reasoning manifest", reasoning_manifest),
        ("inventory", inventory_file),
        ("Concept", concept_file),
        ("closed IR", closed_ir),
        ("reconciliation manifest", reconciliation_manifest),
        ("image evidence", image_evidence),
        ("image evidence manifest", image_evidence_manifest),
        ("asset fulfilment", asset_fulfilment),
        ("asset manifest", asset_manifest),
        ("compile manifest", compile_manifest),
    )
    for label, path in required:
        if not path.exists():
            console.print(f"[red]Spatial-freeze {label} not found:[/] {path}")
            raise typer.Exit(2)
    if config is not None and not config.is_file():
        console.print(f"[red]Spatial-freeze config not found:[/] {config}")
        raise typer.Exit(2)
    try:
        path = freeze_spatial_build(
            project_root=project_root,
            source_image=source_image,
            reasoning_manifest=reasoning_manifest,
            inventory=inventory_file,
            concept=concept_file,
            closed_ir=closed_ir,
            reconciliation_manifest=reconciliation_manifest,
            image_evidence=image_evidence,
            image_evidence_manifest=image_evidence_manifest,
            asset_fulfilment=asset_fulfilment,
            asset_manifest=asset_manifest,
            compile_manifest=compile_manifest,
            config=config,
            output=output,
            manifest=manifest,
        )
        freeze_manifest = manifest or path.with_name(path.stem + ".manifest.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        console.print(f"[green]Spatial freeze written[/] {path}")
        console.print(f"[green]Slice-6.7 manifest written[/] {freeze_manifest}")
        console.print(f"[green]Lineage hash:[/] {data.get('lineage_sha256')}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            SpatialFreezeError) as exc:
        console.print(f"[red]Spatial-freeze error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-reconcile")
def unity_vizconcept_reconcile(
    inventory_file: Path = typer.Option(
        ..., "--inventory", help="Slice-2 visual inventory JSON"
    ),
    concept_file: Path = typer.Option(
        ..., "--concept", help="Slice-2 image-derived Concept.txt"
    ),
    reasoning_manifest: Path = typer.Option(
        ..., "--reasoning-manifest", help="Slice-2 provenance manifest"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Closed image IR JSON destination"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-3 reconciliation manifest destination"
    ),
) -> None:
    """Run deterministic Slice 3: reconcile the image-derived artifacts."""
    from src.visual_reasoning_compiler import VisualReasoningCompilerError
    from src.visual_reasoning_reconcile import reconcile_image_reasoning

    try:
        manifest_path = manifest or output.with_suffix(".reconcile.json")
        path = reconcile_image_reasoning(
            inventory_file=inventory_file,
            concept_file=concept_file,
            reasoning_manifest=reasoning_manifest,
            output=output,
            manifest=manifest_path,
        )
        console.print(f"[green]Closed image IR written[/] {output}")
        console.print(f"[green]Slice-3 reconciliation manifest written[/] {path}")
        console.print("[green]LLM calls in Slice 3:[/] 0")
    except (OSError, ValueError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual reconciliation error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-fulfill-assets")
def unity_vizconcept_fulfill_assets(
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Slice-3 closed image IR JSON"
    ),
    reconciliation_manifest: Path = typer.Option(
        ..., "--reconciliation-manifest",
        help="Slice-3 reconciliation manifest used to verify the IR hash",
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-4 asset fulfilment JSON destination"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-4 fulfilment manifest destination"
    ),
    lookups_dir: Path = typer.Option(
        Path("lookups"), "--lookups-dir", help="Frozen Unity catalog directory"
    ),
    asset_root: Optional[Path] = typer.Option(
        None, "--asset-root",
        help="Fallback root for catalog entries without an embedded root",
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """Run deterministic Slice 4: closed image IR -> frozen asset manifest."""
    from src.visual_asset_fulfilment import fulfil_image_assets
    from src.visual_reasoning_compiler import VisualReasoningCompilerError

    try:
        accepted_licenses = None
        if config.is_file():
            cfg = _cfg(str(config))
            accepted_licenses = ((cfg.get("sketchfab") or {}).get("accepted_licenses"))
        manifest_path = manifest or output.with_suffix(".fulfilment.json")
        path = fulfil_image_assets(
            closed_ir=closed_ir,
            reconciliation_manifest=reconciliation_manifest,
            output=output,
            manifest=manifest_path,
            lookups_dir=str(lookups_dir),
            asset_root=asset_root,
            accepted_licenses=accepted_licenses,
        )
        result = json.loads(output.read_text(encoding="utf-8"))
        console.print(f"[green]Slice-4 asset fulfilment written[/] {output}")
        console.print(f"[green]Slice-4 manifest written[/] {path}")
        console.print(f"[green]Fulfilled assets:[/] {len(result.get('assets', []))}")
        console.print(f"[yellow]Rejected assets:[/] {len(result.get('rejections', []))}")
        console.print("[green]LLM/network calls in Slice 4:[/] 0")
        if result.get("status") != "fulfilled":
            raise typer.Exit(1)
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual asset fulfilment error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-prepare-assets")
def unity_vizconcept_prepare_assets(
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Slice-3 closed image IR JSON"
    ),
    reconciliation_manifest: Path = typer.Option(
        ..., "--reconciliation-manifest",
        help="Slice-3 reconciliation manifest used to verify the IR hash",
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Preparation report destination"
    ),
    lookups_dir: Path = typer.Option(
        Path("lookups"), "--lookups-dir", help="Frozen Unity catalog directory"
    ),
    asset_root: Optional[Path] = typer.Option(
        None, "--asset-root",
        help="Fallback root for catalog entries without an embedded root",
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
) -> None:
    """Pre-Slice-4 Sketchfab freeze; failed requests remain catalog fallbacks."""
    from src.visual_asset_preparation import prepare_image_assets
    from src.visual_reasoning_compiler import VisualReasoningCompilerError

    try:
        cfg = _cfg(str(config)) if config.is_file() else {}
        sk_cfg = (cfg.get("sketchfab") or {})
        path = prepare_image_assets(
            closed_ir=closed_ir,
            reconciliation_manifest=reconciliation_manifest,
            output=output,
            lookups_dir=str(lookups_dir),
            asset_root=asset_root,
            sketchfab_config=sk_cfg,
            accepted_licenses=sk_cfg.get("accepted_licenses"),
        )
        result = json.loads(path.read_text(encoding="utf-8"))
        console.print(f"[green]Sketchfab preparation report written[/] {path}")
        console.print(f"[green]Frozen Sketchfab assets:[/] {result.get('frozen_count', 0)}")
        console.print(f"[yellow]Catalog fallbacks:[/] {result.get('fallback_count', 0)}")
        console.print("[green]LLM calls here:[/] 0")
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual asset preparation error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-compile")
def unity_vizconcept_compile(
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Slice-3 closed image IR JSON"
    ),
    image_evidence: Optional[Path] = typer.Option(
        None, "--image-evidence",
        help="Optional frozen Slice-6.1 image spatial evidence JSON; when supplied, "
             "the main Unity pipeline runs Slice-6.3 fusion",
    ),
    asset_fulfilment: Path = typer.Option(
        ..., "--asset-fulfilment", help="Slice-4 asset fulfilment JSON"
    ),
    asset_manifest: Path = typer.Option(
        ..., "--asset-manifest", help="Slice-4 fulfilment manifest"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="New Unity project output directory"
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
    lookups_dir: Path = typer.Option(
        Path("lookups"), "--lookups-dir", help="Frozen Unity catalog directory"
    ),
    no_think: bool = typer.Option(
        False, "--no-think", help="Disable reasoning on the single schema-fill call"
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the configured Step-3 model"
    ),
    compose: bool = typer.Option(
        True, "--compose/--no-compose",
        help="Emit deterministic micro-generators and audio (default: on)",
    ),
) -> None:
    """Run Slice 5: closed Concept + exact Slice-4 assets -> Unity project.

    The Concept is reused byte-for-byte, so the normal Concept call is skipped.
    The existing schema-fill call runs once; visual assets are then passed as
    explicit locked bindings into the generator. By default the deterministic
    micro composer is also run, which emits Assets/Micros and the audio bank;
    --no-compose is the legacy shell-only mode. This command never performs a
    live Sketchfab search or thumbnail/CLIP selection. Pass --image-evidence to
    carry the closed board composition through the deterministic 6.3–6.6 path.
    """
    from src.unity_pipeline import UnityPipeline
    from src.visual_reasoning_compile import load_visual_compile_plan
    from src.visual_reasoning_compiler import (
        VisualReasoningCompilerError, sha256_file,
    )
    from src.visual_reasoning_reconcile import CLOSED_FORMAT
    from src.visual_asset_fulfilment import FULFILMENT_FORMAT

    try:
        if output.exists():
            raise VisualReasoningCompilerError(
                f"refusing to compile into an existing output directory: {output}"
            )
        if image_evidence is not None and not image_evidence.is_file():
            raise VisualReasoningCompilerError(
                f"Slice-6.1 image evidence not found: {image_evidence}"
            )
        closed = json.loads(closed_ir.read_text(encoding="utf-8"))
        # The Slice-3 manifest is adjacent to the closed IR in the documented
        # run layout.  The fulfilment manifest independently records the
        # closed request fingerprint, so use that as the cross-stage check when
        # a caller keeps the manifest under another name.
        if not isinstance(closed, dict) or closed.get("format") != CLOSED_FORMAT:
            raise VisualReasoningCompilerError("Slice-5 closed IR has the wrong format")
        if closed.get("status") != "closed":
            raise VisualReasoningCompilerError("Slice-5 requires a closed Slice-3 IR")

        fulfilment = json.loads(asset_fulfilment.read_text(encoding="utf-8"))
        manifest = json.loads(asset_manifest.read_text(encoding="utf-8"))
        if not isinstance(fulfilment, dict) or fulfilment.get("format") != FULFILMENT_FORMAT:
            raise VisualReasoningCompilerError(
                "Slice-5 asset fulfilment has the wrong format"
            )
        if fulfilment.get("status") != "fulfilled":
            raise VisualReasoningCompilerError(
                "Slice-5 requires a fulfilled Slice-4 result; compile was not attempted"
            )
        if not isinstance(manifest, dict) or manifest.get("format") != FULFILMENT_FORMAT:
            raise VisualReasoningCompilerError(
                "Slice-5 asset manifest has the wrong format"
            )
        expected_output_hash = (
            ((manifest.get("outputs") or {}).get("asset_fulfilment") or {})
            .get("sha256")
        )
        if expected_output_hash != sha256_file(asset_fulfilment):
            raise VisualReasoningCompilerError(
                "Slice-4 asset manifest hash does not match asset fulfilment JSON"
            )
        request_fingerprint = closed.get("request_fingerprint")
        if not request_fingerprint or (
            fulfilment.get("closed_request_fingerprint") != request_fingerprint
            or manifest.get("closed_request_fingerprint") != request_fingerprint
        ):
            raise VisualReasoningCompilerError(
                "Slice-3 and Slice-4 request fingerprints do not match"
            )
        if manifest.get("fulfilment_fingerprint") != fulfilment.get(
                "fulfilment_fingerprint"):
            raise VisualReasoningCompilerError(
                "Slice-4 fulfilment fingerprint does not match its manifest"
            )

        inventory = ((closed.get("inventory") or {}).get("visible_elements") or [])
        by_label = {
            str(element.get("label")): element for element in inventory
            if isinstance(element, dict) and element.get("label")
        }
        records = []
        for asset in fulfilment.get("assets") or []:
            if not isinstance(asset, dict):
                raise VisualReasoningCompilerError(
                    "Slice-4 asset record is not an object"
                )
            label = str(asset.get("inventory_label") or "")
            if label not in by_label:
                raise VisualReasoningCompilerError(
                    f"Slice-4 asset label is absent from the closed inventory: {label!r}"
                )
            record = dict(asset)
            record["element"] = by_label[label]
            records.append(record)
        required_asset_labels = {
            str(request.get("inventory_label"))
            for request in (closed.get("asset_requests") or [])
            if isinstance(request, dict) and request.get("inventory_label")
        }
        if {str(record.get("inventory_label")) for record in records} != \
                required_asset_labels:
            raise VisualReasoningCompilerError(
                "Slice-4 did not provide one fulfilled record for every asset-worthy "
                "closed-inventory element"
            )

        concept = str(((closed.get("concept") or {}).get("text") or "")).strip()
        if not concept:
            raise VisualReasoningCompilerError("closed IR carries no Concept text")
        if not config.is_file():
            raise VisualReasoningCompilerError(f"config file not found: {config}")

        cfg = _cfg(str(config), model=model)
        asset_cfg = cfg.get("assets") or {}
        configured_root = asset_cfg.get("asset_root")
        plan = load_visual_compile_plan(
            closed_ir=closed_ir,
            asset_fulfilment=asset_fulfilment,
            asset_manifest=asset_manifest,
            lookups_dir=str(lookups_dir),
            asset_root=Path(configured_root) if configured_root else None,
        )
        pipe_cfg = dict(cfg.get("unity_pipeline") or {})
        pipe_cfg["unity_output_dir"] = str(output)
        pipe_cfg["lookups_dir"] = str(lookups_dir)
        cfg["unity_pipeline"] = pipe_cfg
        # Slice 4 already performed the only authorized marketplace operation.
        # Disable every live/thumbnail path for the Unity handoff; locked
        # bindings are consumed directly by the generator.
        sketchfab_cfg = dict(cfg.get("sketchfab") or {})
        sketchfab_cfg.update({
            "enabled": False,
            "auto_fetch": False,
            "vision_select": False,
            "semantic_select": False,
        })
        cfg["sketchfab"] = sketchfab_cfg

        project_root = UnityPipeline(cfg).run(
            concept,
            theme="none",
            compose=compose,
            blender_models=False,
            no_think=no_think,
            micro_code=False,
            precomputed_concept=concept,
            visual_compile_plan=plan,
            closed_image_ir=closed_ir if image_evidence is not None else None,
            image_spatial_evidence=image_evidence,
        )
        schema_path = project_root / f"{project_root.name}.schema.json"
        schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
        compile_manifest = output / "visual_compile.manifest.json"
        compile_manifest.write_text(json.dumps({
            "format": "visual-reasoning-compiler/compile/v1",
            "stage": "5_visual_compile",
            "status": "compiled",
            "authority": {
                "concept_source": "validated_slice_2_concept",
                "concept_llm_called_here": False,
                "schema_fill_calls": "existing_step_3_only",
                "network_called_here": False,
                "thumbnail_or_clip_selection_here": False,
            },
            "composition": {
                "enabled": compose,
                "micro_code_llm_calls": 0,
                "micros_root": str(project_root / "Assets" / "Micros")
                if compose else None,
                "micro_script_count": len(list(
                    (project_root / "Assets" / "Micros").rglob("*.cs")
                )) if compose and (project_root / "Assets" / "Micros").is_dir() else 0,
                "audio_clip_count": len(list(
                    (project_root / "Assets" / "Resources" / "Audio").rglob("*.wav")
                )) if compose and (project_root / "Assets" / "Resources" / "Audio").is_dir() else 0,
            },
            "inputs": plan["inputs"],
            "binding_report": (schema_data.get("meta") or {})
            .get("visual_reasoning_compiler"),
            "outputs": {
                "project_root": str(project_root),
                "schema": {
                    "path": str(schema_path),
                    "sha256": sha256_file(schema_path),
                },
            },
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        console.print(f"[green]Unity visual project written[/] {project_root}")
        console.print(f"[green]Slice-5 compile manifest written[/] {compile_manifest}")
        console.print(f"[green]Exact visual bindings:[/] {len(plan['assets'])}")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            RuntimeError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual Unity compile error:[/] {exc}")
        raise typer.Exit(2) from exc


@app.command("unity-vizconcept-e2e")
@app.command("unity-vizconcept")
def unity_vizconcept_e2e(
    source_image: Path = typer.Option(
        ..., "--image", "--source-image",
        help="Existing image to import as the canonical visual board",
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o",
        help="Visual run directory (default: output/visual_reasoning/<image>_e2e)",
    ),
    unity_output: Optional[Path] = typer.Option(
        None, "--unity-output",
        help="Unity output directory (default: output/unity/<run-directory-name>)",
    ),
    config: Path = typer.Option(Path("config.yaml"), "--config"),
    lookups_dir: Path = typer.Option(
        Path("lookups"), "--lookups-dir", help="Frozen Unity catalog directory"
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the configured Qwen/LLM model"
    ),
    no_think: bool = typer.Option(
        False, "--no-think", help="Disable reasoning on the Step-3 schema-fill call"
    ),
    compile_project: bool = typer.Option(
        True, "--compile/--no-compile",
        help="Continue through Unity compile, spatial acceptance, and 6.7 freeze",
    ),
    sketchfab_first: bool = typer.Option(
        True, "--sketchfab-first/--catalog-only",
        help="Freeze missing image assets from Sketchfab before deterministic Slice 4",
    ),
) -> None:
    """Run the complete imported-image visual compiler in one command.

    This is the persisted equivalent of the former eight-command handoff:
    import -> Slice 2 reasoning -> Slice 3 reconciliation -> Slice 4 asset
    fulfilment -> Slice 6.1 evidence -> Slice 5 Unity compile -> deterministic
    Slices 6.2-6.6 -> Slice 6.7 freeze.  When enabled, the explicit
    pre-Slice-4 preparation freezes Sketchfab candidates first; Slice 4 then
    reads frozen Sketchfab entries before falling back to the local catalog.
    The Unity compile composes the deterministic micro-generators and audio
    bank by default; no post-schema LLM call or live Sketchfab operation is
    permitted after Slice 4 begins.
    """
    from src.backends import make_chat_client
    from src.visual_asset_fulfilment import fulfil_image_assets
    from src.visual_reasoning_compiler import (
        VisualReasoningCompilerError,
        import_existing_board,
        reason_over_board,
    )
    from src.visual_reasoning_reconcile import reconcile_image_reasoning
    from src.visual_spatial_extraction import (
        VisualSpatialExtractionError,
        extract_image_spatial_evidence,
    )
    from src.unity_spatial_freeze import SpatialFreezeError, freeze_spatial_build

    if not source_image.is_file():
        console.print(f"[red]Visual source image not found:[/] {source_image}")
        raise typer.Exit(2)
    if not config.is_file():
        console.print(f"[red]Config file not found:[/] {config}")
        raise typer.Exit(2)
    if not lookups_dir.is_dir():
        console.print(f"[red]Unity lookups directory not found:[/] {lookups_dir}")
        raise typer.Exit(2)

    safe_stem = "".join(
        char if char.isalnum() or char in "-_" else "_"
        for char in source_image.stem
    ).strip("._-") or "visual"
    run_dir = output or (Path("output") / "visual_reasoning" / f"{safe_stem}_e2e")
    project_output = unity_output or (Path("output") / "unity" / run_dir.name)
    if run_dir.exists():
        console.print(
            f"[red]Refusing to overwrite visual run directory:[/] {run_dir}\n"
            "Choose a new --output directory."
        )
        raise typer.Exit(2)
    if compile_project and project_output.exists():
        console.print(
            f"[red]Refusing to overwrite Unity output directory:[/] {project_output}\n"
            "Choose a new --unity-output directory."
        )
        raise typer.Exit(2)

    board = run_dir / "board.png"
    board_manifest = run_dir / "board.import.json"
    inventory = run_dir / "visual_inventory.json"
    concept = run_dir / "Concept.txt"
    context = run_dir / "context.txt"
    reasoning_manifest = run_dir / "reasoning.json"
    closed_ir = run_dir / "closed_image_ir.json"
    reconciliation_manifest = run_dir / "reconciliation.json"
    asset_fulfilment = run_dir / "asset_fulfilment.json"
    asset_manifest = run_dir / "asset_fulfilment.manifest.json"
    asset_preparation = run_dir / "asset_preparation.json"
    image_evidence = run_dir / "image_spatial_evidence.json"
    image_evidence_manifest = run_dir / "image_spatial_evidence.manifest.json"

    llm = None
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        cfg = _cfg(str(config), model=model)
        llm = make_chat_client(cfg)

        console.print("[cyan]Visual E2E Slice 1:[/] importing canonical board")
        import_existing_board(
            board_image=source_image,
            output=board,
            manifest=board_manifest,
        )

        console.print("[cyan]Visual E2E Slice 2:[/] reasoning over board")
        reason_over_board(
            board_image=board,
            inventory_output=inventory,
            concept_output=concept,
            manifest=reasoning_manifest,
            board_manifest=board_manifest,
            context_output=context,
            llm=llm,
        )

        console.print("[cyan]Visual E2E Slice 3:[/] reconciling image IR")
        reconcile_image_reasoning(
            inventory_file=inventory,
            concept_file=concept,
            reasoning_manifest=reasoning_manifest,
            output=closed_ir,
            manifest=reconciliation_manifest,
        )

        configured_root = (cfg.get("assets") or {}).get("asset_root")
        if sketchfab_first:
            from src.visual_asset_preparation import prepare_image_assets

            console.print(
                "[cyan]Visual asset preparation:[/] Sketchfab-first freeze; "
                "unresolved requests fall back to the local catalog"
            )
            prepare_image_assets(
                closed_ir=closed_ir,
                reconciliation_manifest=reconciliation_manifest,
                output=asset_preparation,
                lookups_dir=str(lookups_dir),
                asset_root=Path(configured_root) if configured_root else None,
                sketchfab_config=(cfg.get("sketchfab") or {}),
                accepted_licenses=((cfg.get("sketchfab") or {}).get("accepted_licenses")),
            )
        console.print(
            "[cyan]Visual E2E Slice 4:[/] "
            + ("frozen Sketchfab first, local catalog fallback"
               if sketchfab_first else "local catalog only")
        )
        fulfil_image_assets(
            closed_ir=closed_ir,
            reconciliation_manifest=reconciliation_manifest,
            output=asset_fulfilment,
            manifest=asset_manifest,
            lookups_dir=str(lookups_dir),
            asset_root=Path(configured_root) if configured_root else None,
            accepted_licenses=((cfg.get("sketchfab") or {}).get("accepted_licenses")),
            allow_sketchfab=sketchfab_first,
        )
        fulfilled = json.loads(asset_fulfilment.read_text(encoding="utf-8"))
        if fulfilled.get("status") != "fulfilled":
            raise VisualReasoningCompilerError(
                "Slice 4 did not produce a fulfilled asset result"
            )

        console.print("[cyan]Visual E2E Slice 6.1:[/] extracting image-space evidence")
        extract_image_spatial_evidence(
            board_image=board,
            closed_ir=closed_ir,
            output=image_evidence,
            manifest=image_evidence_manifest,
            llm=llm,
        )

        if not compile_project:
            console.print(f"[green]Visual E2E precompile artifacts written[/] {run_dir}")
            return

        console.print(
            "[cyan]Visual E2E compile:[/] deterministic micros + audio + Slices 6.2-6.6"
        )
        unity_vizconcept_compile(
            closed_ir=closed_ir,
            image_evidence=image_evidence,
            asset_fulfilment=asset_fulfilment,
            asset_manifest=asset_manifest,
            output=project_output,
            config=config,
            lookups_dir=lookups_dir,
            no_think=no_think,
            model=model,
            compose=True,
        )

        compile_manifest = project_output / "visual_compile.manifest.json"
        if not compile_manifest.is_file():
            raise VisualReasoningCompilerError(
                "visual compile returned without a Unity project and compile manifest"
            )
        compile_data = json.loads(compile_manifest.read_text(encoding="utf-8"))
        project_ref = ((compile_data.get("outputs") or {}).get("project_root"))
        project_root = Path(str(project_ref)) if project_ref else Path()
        if not project_root.is_absolute() and not project_root.is_dir():
            project_root = Path.cwd() / project_root
        if not project_root.is_dir():
            raise VisualReasoningCompilerError(
                "visual compile returned a manifest without an existing Unity project"
            )

        console.print("[cyan]Visual E2E Slice 6.7:[/] freezing accepted lineage")
        freeze_path = freeze_spatial_build(
            project_root=project_root,
            source_image=source_image,
            reasoning_manifest=reasoning_manifest,
            inventory=inventory,
            concept=concept,
            closed_ir=closed_ir,
            reconciliation_manifest=reconciliation_manifest,
            image_evidence=image_evidence,
            image_evidence_manifest=image_evidence_manifest,
            asset_fulfilment=asset_fulfilment,
            asset_manifest=asset_manifest,
            compile_manifest=compile_manifest,
            config=config,
        )
        frozen = json.loads(freeze_path.read_text(encoding="utf-8"))
        console.print(f"[green]Visual E2E accepted and frozen[/] {project_root}")
        console.print(f"[green]Spatial freeze:[/] {freeze_path}")
        console.print(f"[green]Lineage hash:[/] {frozen.get('lineage_sha256')}")
    except (OSError, ValueError, RuntimeError, TypeError, KeyError,
            json.JSONDecodeError, VisualReasoningCompilerError,
            VisualSpatialExtractionError, SpatialFreezeError) as exc:
        console.print(f"[red]Visual E2E error:[/] {exc}")
        raise typer.Exit(2) from exc
    finally:
        if llm is not None and hasattr(llm, "unload"):
            llm.unload()


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
