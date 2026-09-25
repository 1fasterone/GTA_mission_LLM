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
    try:
        cfg = _cfg(str(config))
        llm = make_chat_client(cfg)
        manifest_path = manifest or concept_output.with_suffix(".reasoning.json")
        path = reason_over_board(
            board_image=board_image,
            inventory_output=inventory_output,
            concept_output=concept_output,
            manifest=manifest_path,
            board_manifest=board_manifest,
            llm=llm,
        )
        console.print(f"[green]Visual inventory written[/] {inventory_output}")
        console.print(f"[green]Image-derived Concept.txt written[/] {concept_output}")
        console.print(f"[green]Slice-2 provenance manifest written[/] {path}")
    except (OSError, ValueError, RuntimeError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual reasoning error:[/] {exc}")
        raise typer.Exit(2) from exc


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
def unity_spatial_fuse(
    schema_file: Path = typer.Option(
        ..., "--schema", help="Validated Unity .schema.json input"
    ),
    closed_ir: Path = typer.Option(
        ..., "--closed-ir", help="Closed Slice-3 image IR JSON"
    ),
    image_evidence: Path = typer.Option(
        ..., "--image-evidence", help="Frozen Slice-6.1 image spatial evidence JSON"
    ),
    spatial_roles: Path = typer.Option(
        ..., "--spatial-roles", help="Frozen Slice-6.2 schema spatial roles JSON"
    ),
    output: Path = typer.Option(
        ..., "--output", "-o", help="Slice-6.3 spatial fusion JSON"
    ),
    manifest: Optional[Path] = typer.Option(
        None, "--manifest", help="Slice-6.3 provenance manifest destination"
    ),
) -> None:
    """Run deterministic Slice 6.3: image evidence + schema roles -> unit-free constraints."""
    from src.unity_spatial_fusion import SpatialFusionError, write_spatial_fusion

    for label, path in (
        ("schema", schema_file),
        ("closed image IR", closed_ir),
        ("image evidence", image_evidence),
        ("spatial roles", spatial_roles),
    ):
        if not path.is_file():
            console.print(f"[red]Spatial-fusion {label} not found:[/] {path}")
            raise typer.Exit(2)
    try:
        manifest_path = write_spatial_fusion(
            schema_path=schema_file,
            closed_ir_path=closed_ir,
            image_evidence_path=image_evidence,
            spatial_roles_path=spatial_roles,
            output=output,
            manifest=manifest,
        )
        console.print(f"[green]Spatial fusion written[/] {output}")
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
) -> None:
    """Run Slice 5: closed Concept + exact Slice-4 assets -> Unity project.

    The Concept is reused byte-for-byte, so the normal Concept call is skipped.
    The existing schema-fill call runs once; visual assets are then passed as
    explicit locked bindings into the generator. This command never performs a
    live Sketchfab search or thumbnail/CLIP selection. Pass --image-evidence to
    carry the closed board composition through the deterministic 6.3–6.6 path.
    """
    from src.unity_pipeline import UnityPipeline
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

        cfg = _cfg(str(config))
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
            no_think=no_think,
            precomputed_concept=concept,
            visual_asset_bindings=records,
            closed_image_ir=closed_ir if image_evidence is not None else None,
            image_spatial_evidence=image_evidence,
        )
        schema_path = project_root / f"{project_root.name}.schema.json"
        compile_manifest = output / "visual_compile.manifest.json"
        compile_manifest.write_text(json.dumps({
            "format": "visual-reasoning-compiler/unity-compile/v1",
            "stage": "5_unity_compile",
            "status": "compiled",
            "authority": {
                "concept_source": "validated_slice_2_concept",
                "concept_llm_called_here": False,
                "schema_fill_calls": "existing_step_3_only",
                "network_called_here": False,
                "thumbnail_or_clip_selection_here": False,
            },
            "inputs": {
                "closed_image_ir": {
                    "path": str(closed_ir), "sha256": sha256_file(closed_ir),
                },
                "asset_fulfilment": {
                    "path": str(asset_fulfilment),
                    "sha256": sha256_file(asset_fulfilment),
                },
                "asset_manifest": {
                    "path": str(asset_manifest),
                    "sha256": sha256_file(asset_manifest),
                },
            },
            "outputs": {
                "project_root": str(project_root),
                "schema": {
                    "path": str(schema_path),
                    "sha256": sha256_file(schema_path),
                },
            },
            "fulfilled_count": len(records),
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        console.print(f"[green]Unity visual project written[/] {project_root}")
        console.print(f"[green]Slice-5 compile manifest written[/] {compile_manifest}")
        console.print(f"[green]Exact visual bindings:[/] {len(records)}")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError,
            RuntimeError, VisualReasoningCompilerError) as exc:
        console.print(f"[red]Visual Unity compile error:[/] {exc}")
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
