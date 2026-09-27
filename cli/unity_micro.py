"""Unity micro-generator subcommands: unity-audio, unity-npc-dialog, and the
17 simple `unity-<kind>` generators sharing one CLI shape."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Optional

import typer

from .common import app, console, _cfg


# ── Unity micro-generator subcommands ─────────────────────────────────────────

def _run_micro(kind: str, prompt: Optional[str], file: Optional[Path],
               variant: Optional[str], theme: Optional[str],
               mashup_with: Optional[str], seed: Optional[int],
               out: Optional[str], config: str, model: Optional[str] = None) -> None:
    from src.micro import REGISTRY
    if kind not in REGISTRY:
        console.print(f"[red]Error:[/] unknown micro kind: {kind}")
        raise typer.Exit(1)

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

    if not prompt:
        console.print("[red]Error:[/] provide a prompt or use --file <path>")
        raise typer.Exit(1)

    pipeline_cls = REGISTRY[kind]
    pipeline_cls(_cfg(config, model)).run(
        prompt,
        variant=variant,
        theme=theme,
        mashup_with=mashup_with,
        seed=seed,
        out=out,
    )


def _register_micro_command(kind: str, variant_help: str, docstring: str) -> None:
    """Register `unity-<kind>` as a typer command wrapping `_run_micro`.

    Every simple micro-generator (one that just takes prompt/--file/--variant/
    --theme/--mashup/--seed/--out/--config/--model, with no bespoke flags) shares
    this exact CLI shape — defining it once here instead of duplicating a ~15-line
    typer command per kind keeps that shape from drifting (e.g. --variant help
    text quietly falling out of sync with the kind's real Literal variants).
    Kinds with extra flags (unity-audio, unity-npc-dialog, unity-model, ...)
    stay as their own bespoke @app.command functions below.
    """
    def _cmd(
        prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
        file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
        variant: Optional[str]  = typer.Option(None, "--variant", help=variant_help),
        theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
        mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
        seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
        out:     Optional[str]  = typer.Option(None, "--out", help="Override output directory"),
        config:  str            = typer.Option("config.yaml", "--config", "-c"),
        model:   Optional[str]  = typer.Option(None, "--model", "-m",
                                               help="Override the configured LM Studio model for this run"),
    ) -> None:
        _run_micro(kind, prompt, file, variant, theme, mashup_with, seed, out, config, model=model)

    _cmd.__doc__  = docstring
    _cmd.__name__ = f"unity_{kind}"
    app.command(f"unity-{kind}")(_cmd)


@app.command("unity-audio")
def unity_audio(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="2d_simple|3d_spatial|music_layers|crossfade_zones|categorical_mixer"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
    no_llm:  bool           = typer.Option(False, "--no-llm", help="Skip the LLM; build the bank from the game-type's required cues (deterministic)"),
    clips:   bool           = typer.Option(True, "--clips/--no-clips", help="Generate actual .wav clips into Resources/Audio/"),
    unity:   Optional[str]  = typer.Option(None, "--unity", "-u", help="Overlay generated clips + manifest into this Unity project's Assets/Resources/"),
) -> None:
    """Generate a Unity audio bank and a deferred Stable Audio 3 batch.

    With audio.defer_stable3 enabled (the default), this writes the prompts and
    Unity-readable pending manifest without loading Stable Audio.  Render it later
    with `unity-audio-batch`.  Set audio.defer_stable3: false for the legacy inline
    worker, or audio.backend: dsp for immediate deterministic synth output."""
    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}"); raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()
    if not prompt:
        console.print("[red]Error:[/] provide a prompt or use --file <path>"); raise typer.Exit(1)

    from src.micro import AudioPipeline
    cfg = _cfg(config, model)
    out_dir = AudioPipeline(cfg).run(
        prompt, variant=variant, theme=theme, mashup_with=mashup_with, seed=seed,
        out=out, use_llm=not no_llm, with_clips=clips)

    deferred = (
        str((cfg.get("audio", {}) or {}).get("backend", "dsp")).strip().lower() == "stable3"
        and bool((cfg.get("audio", {}) or {}).get("defer_stable3", True))
    )
    if unity and clips and not deferred:
        import shutil
        src_res = Path(out_dir) / "Resources"
        if src_res.exists():
            dst_res = Path(unity) / "Assets" / "Resources"
            shutil.copytree(src_res, dst_res, dirs_exist_ok=True)
            console.print(f"  [green]overlay[/] -> {dst_res}")
    elif unity and clips and deferred:
        console.print(
            "  [cyan]Stable Audio 3 deferred:[/] run unity-audio-batch "
            f"{Path(out_dir) / 'audio_batch.json'} --unity {unity}"
        )


@app.command("unity-audio-batch")
def unity_audio_batch(
    batch: Path = typer.Argument(..., help="audio_batch.json emitted by unity-create or unity-audio"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    unity: Optional[Path] = typer.Option(
        None, "--unity", "-u", help="Unity project root; inferred when batch is under Assets/Micros/audio"
    ),
) -> None:
    """Render a deferred Stable Audio 3 batch in its standalone environment.

    The compiler's virtualenv stays free of Stable Audio dependencies.  This
    command launches audio.stable3_python (or the current interpreter), reuses
    each loaded Stable Audio model across the batch, writes the final manifest,
    and overlays clips into the Unity project when a project root is available.
    """
    if not batch.is_file():
        console.print(f"[red]Error:[/] batch file not found: {batch}")
        raise typer.Exit(1)
    cfg = _cfg(config)
    audio_cfg = cfg.get("audio", {}) or {}
    worker_python = str(audio_cfg.get("stable3_python") or sys.executable)
    repo_root = Path(__file__).resolve().parents[1]
    request = {
        "batch_path": str(batch.resolve()),
        "config": cfg,
        "unity": str(unity.resolve()) if unity else None,
    }
    with tempfile.TemporaryDirectory(prefix="stable3-batch-") as temp_dir:
        request_path = Path(temp_dir) / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        console.print(
            f"[cyan]Stable Audio 3 standalone batch:[/] {batch} "
            f"(python={worker_python})"
        )
        result = subprocess.run(
            [worker_python, "-m", "src.audio.stable3_batch", "--request", str(request_path)],
            cwd=repo_root,
            check=False,
        )
    if result.returncode:
        console.print(
            f"[red]Error:[/] Stable Audio 3 batch exited with code {result.returncode}"
        )
        raise typer.Exit(result.returncode)
    console.print("[green]OK[/] Stable Audio 3 batch complete")


@app.command("unity-npc-dialog")
def unity_npc_dialog(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English NPC description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="quest_giver|vendor|ambient_bark|combat_chatter|follower|cutscene"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
    no_llm:     bool           = typer.Option(False, "--no-llm", help="Skip the LLM; use preset dialog lines for the interaction type"),
    audio:      bool           = typer.Option(True,  "--audio/--no-audio", help="Generate WAV clips via Higgs Audio v2 TTS"),
    audio_style: Optional[str] = typer.Option(None,  "--audio-style", help="Override audio style: natural|stylized|robotic|whisper|radio"),
) -> None:
    """Generate NPC dialog lines + WAV clips using Higgs Audio v2 TTS.

    Produces DialogPlayer.cs + DialogTrigger.cs + DialogData.json + WAV files
    under Resources/Audio/Dialog/<speaker>/.

    Default backend: Higgs Audio v2 (config.yaml audio.backend: higgs).
    Degrades gracefully: no Higgs → text-only JSON with subtitle support.
    Set --no-audio to skip clip generation (text + C# only)."""
    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}"); raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()
    if not prompt:
        console.print("[red]Error:[/] provide a prompt or use --file <path>"); raise typer.Exit(1)

    from src.micro import NpcDialogPipeline
    NpcDialogPipeline(_cfg(config, model)).run(
        prompt, variant=variant, theme=theme, mashup_with=mashup_with, seed=seed,
        out=out, use_llm=not no_llm, with_audio=audio, audio_style=audio_style)


# ── Simple micro-generators (share the exact CLI shape above) ────────────────
# kind -> (--variant help text, command docstring). Keep each kind's variant
# list in sync with its Literal in src/micro/<kind>.py — this is the one place
# it's declared for the CLI now, instead of N near-duplicate command functions.
_SIMPLE_MICROS: dict[str, tuple[str, str]] = {
    "controller": (
        "first_person|third_person|top_down|2d_platformer|2d_topdown|platformer_2_5d|"
        "vehicle_car|vehicle_bike|flying|swimming|aircraft|fighting|pointer",
        "Generate a focused Unity character controller (drop-in script + optional input asset).",
    ),
    "camera": (
        "follow|follow_2d|follow_2_5d|orbit|first_person|cinemachine_3p|cinemachine_topdown|"
        "rts_pan_zoom|vehicle_chase|cinematic_dolly|board_framing",
        "Generate a focused Unity camera rig (Cinemachine or plain MonoBehaviour).",
    ),
    "physics": (
        "rigidbody_2d|rigidbody_3d|kinematic_character|ragdoll|vehicle_wheels|soft_body_stub|"
        "trigger_zone|destructible_object|buoyancy",
        "Generate a focused Unity physics/collider body.",
    ),
    "hud": (
        "health_bar|ammo_counter|minimap|crosshair|interaction_prompt|score_panel|"
        "pause_menu|dialogue_box|full_hud_bundle|racing_hud",
        "Generate a focused Unity Canvas / UI Toolkit HUD element.",
    ),
    "input": (
        "keyboard_mouse|gamepad|touch|dual_stick|racing_wheel|vr_xr|pointer",
        "Generate a Unity Input System action map + InputReader ScriptableObject.",
    ),
    "ai": (
        "navmesh_patrol|chase_player|fsm_guard|behaviour_tree_lite|flocking|cover_shooter|"
        "flying_swarm|waypoint_follow",
        "Generate a Unity AI behaviour (NavMesh patrol/chase/FSM/flocking; falls back to "
        "direct 2D seeking with no NavMesh when composed into a 2D project).",
    ),
    "spawner": (
        "wave_spawner|continuous_spawner|object_pool|trigger_spawner|point_spawner|radial_spawner",
        "Generate a Unity spawner (waves, object pool, trigger/point/radial).",
    ),
    "saveload": (
        "json_file|playerprefs_kv|save_slots|checkpoint|autosave|encrypted_json",
        "Generate a Unity save/load system (JSON file, PlayerPrefs, slots, checkpoint, autosave).",
    ),
    "inventory": (
        "slot_based|grid|weight_based|equipment|hotbar|unlimited",
        "Generate a Unity inventory system (slot/grid/weight/equipment/hotbar).",
    ),
    "dialogue": (
        "linear|branching|typewriter|cutscene|barks|localized",
        "Generate a Unity dialogue system (linear/branching/typewriter/cutscene/barks).",
    ),
    "menu": (
        "main_menu|pause_menu|settings_menu|game_over|level_select|confirm_dialog",
        "Generate a Unity menu (main/pause/settings/game-over/level-select/confirm).",
    ),
    "interactable": (
        "pickup|door|lever|button_switch|npc_talk|readable",
        "Generate a Unity interactable (pickup/door/lever/switch/npc/readable).",
    ),
    "transition": (
        "instant|fade|loading_bar|async_additive|trigger_door",
        "Generate a Unity scene transition (instant/fade/loading bar/async/trigger door).",
    ),
    "health": (
        "simple|regenerating|shielded|armor|lives",
        "Generate a Unity health system (simple/regenerating/shielded/armor/lives).",
    ),
    "objectives": (
        "tracker",
        "Generate a Unity mission objective tracker (progress + completion events).",
    ),
    "behavior_tree": (
        "tree",
        "Generate a minimal Unity behavior-tree interpreter (Sequence/Selector; "
        "Action/Condition leaves are SendMessage/RegisterCondition hooks, not autonomous AI).",
    ),
    "statemachine": (
        "player_fsm|enemy_fsm|game_fsm|object_fsm",
        """Generate a Unity FSM (player_fsm/enemy_fsm/game_fsm/object_fsm).

        States and transitions are derived from the prompt concept — not generic defaults.

        \b
        Examples:
          python main.py unity-statemachine "underwater alien that stalks the player"
          python main.py unity-statemachine "deep-sea boss with 3 phases" --variant enemy_fsm
        """,
    ),
    "trigger": (
        "zone_enter|zone_exit|timer_trigger|distance_trigger|compound|click_zone",
        """Generate a Unity event trigger zone (enter/exit/timer/distance/compound).

        Events fired are derived from the prompt — spawns enemies, starts dialogue, etc.

        \b
        Examples:
          python main.py unity-trigger "ambush zone that spawns enemies when player enters"
          python main.py unity-trigger "boss arena seal: lock door + start music" --variant zone_enter
        """,
    ),
    "animator": (
        "humanoid|vehicle|creature|object|ui",
        """Generate a Unity Animator parameter driver (humanoid/vehicle/creature/object/ui).

        Animator params and clip names are derived from the prompt concept.

        \b
        Examples:
          python main.py unity-animator "scuba diver with swim, dive, surface, and drowning states"
          python main.py unity-animator "giant enemy crab — walk, attack, pinch, dead" --variant creature
        """,
    ),
    "shader": (
        "color_cycle|dissolve|hit_flash|uv_scroll|outline_pulse",
        """Generate a Unity runtime shader/material controller (color cycle/dissolve/hit flash/UV scroll/outline pulse).

        Drives shader properties via MaterialPropertyBlock — never touches the shared Material asset.

        \b
        Examples:
          python main.py unity-shader "enemy flashes white when hit"
          python main.py unity-shader "collectible pulses a glowing outline" --variant outline_pulse
        """,
    ),
    "lighting": (
        "three_point|outdoor_sun|interior_practical|flat_2d|stylized_flood",
        """Generate a Unity lighting rig + environment-lighting guard.

        Owns the light rig (key/fill/rim/practical/flood) AND RenderSettings ambient,
        skybox and reflections — the runtime guard that stops materials rendering
        black on Play. Always composed by `unity-create --compose`.

        \b
        Examples:
          python main.py unity-lighting "neon-lit rain-slick alley at midnight" --variant interior_practical
          python main.py unity-lighting "harsh desert noon, long hard shadows" --variant outdoor_sun
        """,
    ),
    "vfx": (
        "particle_system|vfx_sequence|screen_fx|impact_fx",
        """Generate a Unity VFX controller (particle system/scripted sequence/screen fx/impact fx).

        \b
        Examples:
          python main.py unity-vfx "explosion burst when the barrel is shot"
          python main.py unity-vfx "screen flashes red and fades when the player takes damage" --variant screen_fx
        """,
    ),
}
for _kind, (_variant_help, _doc) in _SIMPLE_MICROS.items():
    _register_micro_command(_kind, _variant_help, _doc)
del _kind, _variant_help, _doc
