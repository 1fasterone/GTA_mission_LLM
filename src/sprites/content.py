"""
Sprite bank orchestration — the nondeterministic-generation-then-freeze
sequence, mirrored on src/audio/content.py's generate_bank().

For a set of SpriteCue frames (entity + action + frame index), generates each
via a swappable backend (config `sprites.backend`: "openrouter" [default,
Gemini 2.5 Flash Image] or "comfyui" [local SDXL]), mattes the result if the
backend doesn't already return alpha (see _NATIVELY_TRANSPARENT_BACKENDS),
and writes PNGs + a manifest under
<resources_dir>/Sprites/<entity>/<action>/frame_NN.png — the exact folder
shape SpriteFrameAnimator.cs.j2's Resources.LoadAll<Sprite>() expects.

Unlike 3D models (many entities sharing a finite pre-made catalog, needing
real tiered resolution — see unity_entity_asset_resolver.py), each entity's
sprite frames are generated fresh for THAT entity — no catalog matching
needed, so this stays a flat generate-and-freeze loop. Never raises on a
generation failure: a failed frame gets an unmistakable placeholder and a
named warning (FAIL-LOUD, not FAIL-SILENT — see matte.solid_placeholder_png).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .comfyui_backend import ComfyUIBackend
from .openrouter_backend import OpenRouterBackend
from .matte import remove_background_to_png, solid_placeholder_png

# Backends that already return alpha-matted PNGs (native transparent-background
# support) — matting them again would be redundant work, not a correctness
# fix, since there's no chroma-key/opaque halo left to remove.
_NATIVELY_TRANSPARENT_BACKENDS = frozenset({"openrouter"})


def _make_backend(config: dict):
    name = str((config or {}).get("sprites", {}).get("backend", "openrouter")).lower()
    if name == "comfyui":
        return ComfyUIBackend(config)
    if name != "openrouter":
        print(f"[sprites] unknown sprites.backend={name!r} in config.yaml — falling back to openrouter")
    return OpenRouterBackend(config)


@dataclass
class SpriteCue:
    entity_id:   str          # output folder name — matches the entity this animator belongs to
    action:      str          # e.g. "idle", "run", "punch_light"
    frame_index: int          # 0-based within the action
    prompt:      str          # full generation prompt (character_desc + pose hint)
    width:       int = 1024
    height:      int = 1024


def _seed_for(entity_id: str, seed: int) -> int:
    """Same sha1-derived-seed shape as audio/content.py's cue seeding — one
    base seed PER ENTITY (not per frame/action), deliberately, so every frame
    of every action for one character shares a base seed: the closest thing to
    visual consistency a backend without a dedicated character-identity
    control can do from seed sharing alone (both backends layer real
    reference-image conditioning on top of this — see generate_sprite_bank)."""
    h = hashlib.sha1(f"{entity_id}:{seed}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) & 0x7FFFFFFF


def generate_sprite_bank(cues: list[SpriteCue], resources_dir: Path, config: dict,
                          seed: int = 0) -> dict:
    """Generate every cue, write PNGs under resources_dir/Sprites/, return a
    manifest dict (the caller writes it to resources_dir/sprite_manifest.json,
    mirroring audio_manifest.json).

    Per entity: the first successful frame is generated from scratch and
    becomes that entity's REFERENCE — every other frame for the same entity is
    then generated conditioned on the reference's raw bytes (img2img for
    ComfyUI, `input_references` for OpenRouter). Confirmed live (2026-08-27,
    ComfyUI/SDXL) that sharing only a seed + character_desc across independent
    generations was not enough — a character's costume/colors visibly drifted
    between actions. Chaining off one real reference image holds identity far
    better than seed-sharing alone.
    """
    backend = _make_backend(config)
    natively_transparent = backend.name in _NATIVELY_TRANSPARENT_BACKENDS
    sprites_dir = Path(resources_dir) / "Sprites"

    manifest_entities: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    generated = 0
    placeholders = 0

    by_entity: dict[str, list[SpriteCue]] = {}
    for cue in cues:
        by_entity.setdefault(cue.entity_id, []).append(cue)

    for entity_id, entity_cues in by_entity.items():
        entity_seed = _seed_for(entity_id, seed)
        reference_raw: bytes | None = None

        for cue in entity_cues:
            out_dir = sprites_dir / cue.entity_id / cue.action
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"frame_{cue.frame_index:02d}.png"
            frame_seed = entity_seed + cue.frame_index

            if reference_raw is None:
                raw = backend.generate(cue.prompt, seed=frame_seed,
                                        width=cue.width, height=cue.height)
                if raw is not None:
                    reference_raw = raw   # anchors every later frame for this entity
            else:
                raw = backend.generate_from_reference(
                    cue.prompt, seed=frame_seed, reference_png_bytes=reference_raw)

            if raw is not None:
                if natively_transparent:
                    png_bytes = raw
                    generated += 1
                else:
                    try:
                        png_bytes = remove_background_to_png(raw)
                        generated += 1
                    except Exception as exc:  # noqa: BLE001 — corrupt/unexpected image data
                        warnings.append(f"{cue.entity_id}/{cue.action}/{cue.frame_index}: "
                                         f"matting failed ({type(exc).__name__}: {exc}) — placeholder used")
                        png_bytes = solid_placeholder_png(cue.width, cue.height)
                        placeholders += 1
            else:
                warnings.append(f"{cue.entity_id}/{cue.action}/{cue.frame_index}: "
                                 f"generation failed or {backend.name} unreachable — placeholder used")
                png_bytes = solid_placeholder_png(cue.width, cue.height)
                placeholders += 1

            out_path.write_bytes(png_bytes)

            ent = manifest_entities.setdefault(cue.entity_id, {})
            ent[cue.action] = max(ent.get(cue.action, 0), cue.frame_index + 1)

    # Release any backend-held resources now that the whole batch is done —
    # a real step for ComfyUI (frees local VRAM before the next GPU-hungry
    # pipeline stage — see ComfyUIBackend.free_vram()'s docstring); a no-op
    # for OpenRouter, which holds nothing local to release.
    backend.free_vram()

    return {
        "seed":         seed,
        "backend":      backend.name,
        "checkpoint":   backend.checkpoint,
        "frame_count":  len(cues),
        "generated":    generated,
        "placeholders": placeholders,
        "entities":     manifest_entities,
        "warnings":     warnings,
    }
