"""Blender procedural 3D-model micro-generator: unity-model / unity-tools."""
from __future__ import annotations

from typing import Optional

import typer

from .common import app, console, _cfg


# ── Blender 3D-model micro-generator ──────────────────────────────────────────

def _parse_knobs(pairs: Optional[list[str]]) -> dict:
    """Parse repeatable `--knob key=value` flags into a param dict. Comma values
    become a list (for color), numeric strings become floats."""
    out: dict = {}
    for item in (pairs or []):
        if "=" not in item:
            console.print(f"[yellow]skip knob (need key=value):[/] {item}")
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if "," in v:
            try:
                out[k] = [float(x) for x in v.split(",")]
            except ValueError:
                out[k] = v
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


@app.command("unity-model")
def unity_model(
    prompt:  Optional[str]  = typer.Argument(None, help="Description, e.g. 'lifted matte-black pickup' or 'tall slim humanoid'"),
    variant: Optional[str]  = typer.Option(None, "--variant",
                                           help="car | plane | humanoid | bike | furniture | weapon  (inferred from prompt if omitted)"),
    knob:    Optional[list[str]] = typer.Option(None, "--knob", "-k",
                                  help="Override a param knob (repeatable): -k wheel_r=0.8 -k color=0.02,0.02,0.02"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    rig:     bool           = typer.Option(True,  "--rig/--no-rig",
                                           help="Build armature + skin-bind the parts (game-functional FBX)"),
    demo:    bool           = typer.Option(False, "--demo",
                                           help="Pose the rig after building (visual proof; leave off for rest-pose FBX)"),
    name:    Optional[str]  = typer.Option(None, "--name", help="Asset name (PascalCase; inferred from prompt if omitted)"),
    unity_project: Optional[str] = typer.Option(None, "--unity-project", "-u",
                                  help="Unity project root — wires FBX into scene via schema regeneration; "
                                       "falls back to Assets/BlenderAssets/ if no schema.json found"),
    role:    Optional[str]  = typer.Option(None, "--role",
                                           help="Override catalog kind for scene wiring: "
                                                "player | enemy | vehicle | prop  "
                                                "(inferred from variant if omitted; humanoid defaults to 'player')"),
    emit:    bool           = typer.Option(False, "--emit",
                                           help="Emit the bpy script only (for live MCP Blender); skip headless build"),
    no_llm:  bool           = typer.Option(False, "--no-llm",
                                           help="Deterministic keyword mapping only — no LM Studio call"),
    out:     Optional[str]  = typer.Option(None, "--out", help="Override Blender output directory"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m",
                                           help="Override LM Studio model for param mapping"),
) -> None:
    """Generate a procedural 3D model in Blender (car / plane / humanoid / bike /
    furniture / weapon), export a Unity-ready FBX, and wire it into a Unity project.

    When --unity-project is given and the project has a *.schema.json sidecar
    (written by unity-create), the generator is re-run with the new FBX as a
    model_override — this updates scene.unity, EntitySpawner.cs, and ModelSwap.cs
    so the primitive placeholder is swapped for the real mesh at Play.

    Bases and their knobs
    ---------------------
    car       — body_w, body_len, body_h, cabin_h, cabin_len, cabin_pos, wheel_r,
                wheel_w, ride, bed, wheel_pairs, rim, color
    plane     — fuselage_len, fuselage_r, wing_span, wing_chord, fin_h, color
    humanoid  — height, shoulder_w, hip_w, head_r, limb_r, color
    bike      — wheelbase, wheel_r, wheel_w, frame_h, handlebar_w, rim, wheels,
                engine, tank, pedals, suspension, color
    furniture — width, depth, height, top_t, leg_r, legs, seat, back, shelves,
                drawers, cushion, color
    weapon    — length, caliber, barrel_len, stock, muzzle, magazine, scope, grip,
                foregrip, color

    \b
    Examples:
      python main.py unity-model "a lifted matte-black pickup" --no-llm
      python main.py unity-model --variant plane -k wing_span=9 -k fin_h=1.8 --no-llm
      python main.py unity-model "tall slim humanoid" --variant humanoid --no-llm
      python main.py unity-model "dirt bike" --variant bike -k suspension=1 --no-llm
      python main.py unity-model "a wooden bookcase" --variant furniture --no-llm
      python main.py unity-model "a suppressed sniper rifle" --variant weapon --no-llm
      python main.py unity-model "red sports coupe" -u output/unity/TopDownRacingGame
      python main.py unity-model "tall enemy soldier" --variant humanoid --role enemy -u output/unity/TacticalShooter
      python main.py unity-model "motorbike" --emit
    """
    from src.micro import ModelPipeline
    ModelPipeline(_cfg(config, model)).run(
        prompt, variant=variant, knobs=_parse_knobs(knob), seed=seed, rig=rig,
        demo=demo, name=name, unity_project=unity_project, role=role, emit=emit,
        use_llm=not no_llm, out=out,
    )


@app.command("unity-tools")
def unity_tools(
    action: str = typer.Argument("list", help="list — enumerate every micro-generator"),
) -> None:
    """List available Unity micro-generators."""
    from src.micro import REGISTRY
    if action == "list":
        for kind, cls in REGISTRY.items():
            console.print(f"  [cyan]unity-{kind}[/]  —  {cls.display_name}")
            console.print(f"      variants: {', '.join(cls.variants)}")
    else:
        console.print(f"[red]Unknown action:[/] {action}")
        raise typer.Exit(1)
