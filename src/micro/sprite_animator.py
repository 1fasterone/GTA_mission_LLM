"""Sprite animator micro-generator — generates + wires 2D character frame
animation via a local ComfyUI backend (src/sprites/). Nondeterministic
generation happens once in _pre_write()/write_files() (after LM Studio
unloads, mirroring micro/audio.py's own VRAM-safety convention); everything
else — the C# frame-swap component, the manifest — is deterministic Step 4-7
codegen over the frozen frames.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


SpriteRole = Literal["player", "enemy", "npc", "prop"]
VARIANTS = list(SpriteRole.__args__)   # type: ignore[attr-defined]

# Fixed, deterministic action set per role — structural vocabulary, not LLM
# content (same "closed enum" discipline as ai.py's TransitionWhen). Only
# character_desc (appearance) is LLM-authored; which actions get frames is not.
_ACTIONS_BY_ROLE: dict[str, list[str]] = {
    "player": ["idle", "run", "jump", "punch_light", "punch_heavy", "throw", "hurt", "death"],
    "enemy":  ["idle", "run", "attack", "hurt", "death"],
    "npc":    ["idle"],
    "prop":   ["idle"],
}

_ACTION_POSE_HINTS: dict[str, str] = {
    "idle":        "standing idle, relaxed neutral pose, subtle breathing motion",
    "run":         "mid-run action pose, legs extended in a running stride, dynamic pose",
    "jump":        "airborne jumping pose, limbs extended, mid-air",
    "punch_light": "quick jab punch pose, one arm snapping forward",
    "punch_heavy": "big wind-up haymaker punch pose, exaggerated cartoon force",
    "throw":       "throwing motion pose, arm cocked back about to hurl an object",
    "hurt":        "staggering hit-reaction pose, recoiling off-balance",
    "death":       "dramatic cartoon defeat pose, comically collapsing",
    "attack":      "lunging attack pose, aggressive strike motion",
}


class SpriteAnimatorSchema(CodeExtensibleSchema):
    name:              str   = "SpriteAnimator"
    variant:           str   = "player"     # role: player | enemy | npc | prop
    entity_name:       str   = "Player"     # Resources path segment; should match the schema entity
    character_desc:    str   = ""           # LLM-authored appearance/style — the ONLY generative content
    # Each frame costs a real ComfyUI generation (tens of seconds on an 8 GB
    # card) — 3 keeps a full cast's generation time reasonable while still
    # giving a real (if modest) animation cycle. Raise per-project if the
    # brief calls for smoother motion and the time budget allows it.
    frames_per_action: int   = 3
    fps:               float = 8.0
    squash_stretch:    bool  = True
    description:       str   = "2D sprite frame animator"
    meta:              dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "player"


CONCEPT_PROMPT = """\
{theme_block}
Describe the VISUAL APPEARANCE of a 2D game character for this idea:

"{user_prompt}"

Role: {variant}

Cover in 1-2 paragraphs — appearance ONLY, not gameplay:
- Species/build, silhouette, proportions (cartoon-exaggerated is good)
- Color palette and material read (flat cel-shaded cartoon look)
- Distinctive costume/prop details that should stay consistent across every frame

Do NOT write code or JSON. Do NOT describe poses or actions — those are
handled separately, deterministically, per action.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
character_desc is the ONLY generative-content field here — a tight, concrete
visual description (2-4 sentences) an image model can act on directly: build,
colors, costume, silhouette. Every frame for this character reuses this text
verbatim, so keep it specific rather than vague ("stocky boxer in a faded red
vest, oversized cartoon gloves, round black nose" beats "a fighter").

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "entity_name": "MatchingEntityName",
  "character_desc": "Two to four concrete visual sentences.",
  "frames_per_action": 3,
  "fps": 8.0,
  "squash_stretch": true,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class SpriteAnimatorPipeline(UnityMicroPipeline):
    kind            = "sprite_animator"
    display_name    = "Unity 2D Sprite Frame Animator"
    schema_cls      = SpriteAnimatorSchema
    variants        = VARIANTS
    default_variant = "player"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "sprite_animator"

    _RESERVED_FIELD_NAMES = {
        "entityname", "framesperaction", "fps", "squashstretch",
        "_frames", "_currentaction", "_frameindex", "_frametimer",
        "_spriterenderer", "_rb2d", "_rb3d", "_lastpos", "_basescale",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System.Collections.Generic"}

    def __init__(self, config: dict):
        super().__init__(config)
        # The base MicroPipeline.__init__ doesn't keep this — write_files()
        # needs the full config dict to build a ComfyUIBackend (base_url,
        # steps, ...) and to unload LM Studio (audio.backends.unload_lm_studio
        # takes the whole config, same as micro/audio.py's own self.config).
        self.config = config or {}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "SpriteAnimator")
        if not schema.entity_name.strip():
            schema.entity_name = schema.name.replace("SpriteAnimator", "") or "Entity"
        schema.entity_name = schema.entity_name.strip().replace(" ", "")
        schema.frames_per_action = max(1, min(12, schema.frames_per_action))
        schema.fps = max(1.0, min(30.0, schema.fps))
        if not schema.character_desc.strip():
            schema.character_desc = (
                f"a colourful cartoon {variant} character, flat cel-shaded style, "
                "bold black outlines, exaggerated proportions"
            )
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def _pre_write(self, schema, variant) -> None:
        """Unload LM Studio before ComfyUI generation runs — same VRAM-safety
        convention as micro/audio.py's own _pre_write (an 8 GB card can't hold
        both the schema-fill LLM and an SDXL checkpoint resident at once)."""
        self._step(3, "Unloading LM Studio model to free VRAM for sprite generation")
        try:
            from ..audio.backends import unload_lm_studio
            ok = unload_lm_studio(self.config)
            if ok:
                self._ok("LM Studio model unloaded — VRAM available for ComfyUI")
            else:
                self._warn("Could not unload LM Studio via API — "
                            "manually unload the model to avoid CUDA OOM on 8 GB VRAM")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"unload request failed ({type(exc).__name__}: {exc})")

    def write_files(self, schema, variant, out_dir: Path) -> int:
        from ..sprites.content import SpriteCue, generate_sprite_bank

        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        resources_dir = out_dir / "Resources"
        resources_dir.mkdir(parents=True, exist_ok=True)

        actions = _ACTIONS_BY_ROLE.get(variant, ["idle"])
        cues: list[SpriteCue] = []
        for action in actions:
            pose = _ACTION_POSE_HINTS.get(action, action.replace("_", " "))
            prompt = f"{schema.character_desc}, {pose}, single character, full body, side view"
            for i in range(schema.frames_per_action):
                cues.append(SpriteCue(
                    entity_id=schema.entity_name, action=action, frame_index=i, prompt=prompt,
                ))

        self._step(4, f"Generating {len(cues)} sprite frames for {schema.entity_name} "
                      f"({len(actions)} actions x {schema.frames_per_action} frames) via ComfyUI")
        manifest = generate_sprite_bank(cues, resources_dir, self.config, seed=0)
        if manifest["placeholders"]:
            self._warn(f"{manifest['placeholders']}/{manifest['frame_count']} frames used a "
                       f"placeholder (ComfyUI unavailable or a generation failed) — see "
                       f"sprite_manifest.json warnings")
        else:
            self._ok(f"{manifest['generated']}/{manifest['frame_count']} frames generated")

        (resources_dir / "sprite_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("SpriteFrameAnimator.cs.j2").render(
                s=schema, variant=variant, actions=actions),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant, actions=actions),
            encoding="utf-8",
        )
        return 3
