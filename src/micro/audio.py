"""Audio micro-generator — AudioCue + AudioBank ScriptableObjects + AudioManager."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import MicroPipeline, to_pascal
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, UNITY_EXTRA_INSTRUCTIONS,
)


AudioVariant = Literal[
    "2d_simple", "3d_spatial", "music_layers", "crossfade_zones", "categorical_mixer",
]
VARIANTS = list(AudioVariant.__args__)   # type: ignore[attr-defined]

AudioCategory = Literal["music", "sfx", "ambient", "voice"]


class AudioSchema(CodeExtensibleSchema):
    name:                 str  = "AudioBank"
    variant:              str  = "categorical_mixer"
    categories:           list[AudioCategory] = Field(
        default_factory=lambda: ["music", "sfx", "ambient", "voice"])
    bank_clip_count_hint: int  = 16
    pool_size:            int  = 8
    mixer_groups:         list[str] = Field(default_factory=lambda: ["Master", "Music", "SFX"])
    layer_count:          int  = 3
    crossfade_seconds:    float = 1.5
    description:          str  = "Audio bank + manager"
    meta:                 dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "categorical_mixer"

    @field_validator("categories", mode="before")
    @classmethod
    def _clamp_categories(cls, v: list) -> list:
        if not isinstance(v, list):
            return ["sfx"]
        out: list[str] = []
        seen: set[str] = set()
        for item in v:
            s = str(item).strip().lower()
            if s in AudioCategory.__args__ and s not in seen:   # type: ignore[attr-defined]
                seen.add(s)
                out.append(s)
        return out or ["sfx"]


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity audio system for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Which categories matter (music, sfx, ambient, voice)
- Pool sizes, spatialisation, AudioMixer groups
- How the manager interacts with cues at runtime

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
CATEGORIES — any subset of: music | sfx | ambient | voice

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "categories": ["music", "sfx", "ambient", "voice"],
  "bank_clip_count_hint": 16,
  "pool_size": 8,
  "mixer_groups": ["Master", "Music", "SFX"],
  "layer_count": 3,
  "crossfade_seconds": 1.5,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class AudioPipeline(MicroPipeline):
    kind            = "audio"
    display_name    = "Unity Audio System"
    schema_cls      = AudioSchema
    variants        = VARIANTS
    default_variant = "categorical_mixer"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "audio"

    _RESERVED_FIELD_NAMES = {
        "bank", "poolsize", "defaultgroup", "_pool", "_music",
        "musicvolume", "sfxvolume", "ambientvolume", "voicevolume",
    }
    _TEMPLATE_USINGS = {
        "UnityEngine", "UnityEngine.Audio", "System.Collections.Generic",
    }

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "AudioBank")
        if schema.bank_clip_count_hint < 1: schema.bank_clip_count_hint = 1
        if schema.pool_size < 1:            schema.pool_size = 1
        if schema.layer_count < 1:          schema.layer_count = 1
        if schema.crossfade_seconds <= 0:   schema.crossfade_seconds = 0.5
        # Variant-specific category enforcement
        if variant == "music_layers" and "music" not in schema.categories:
            schema.categories.insert(0, "music")
        if not schema.mixer_groups:
            schema.mixer_groups = ["Master"]
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def attach_class(self, schema) -> str:
        # <name>.cs is the AudioBank ScriptableObject; the scene-attachable
        # MonoBehaviour is <name>AudioManager (composer wired the SO before —
        # "does not derive from MonoBehaviour" at runtime).
        return f"{schema.name}AudioManager"

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # 1. AudioCue (always)
        (scripts_dir / "AudioCue.cs").write_text(
            env.get_template("AudioCue.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        # 2. AudioBank SO (always)
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("AudioBank.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        # 3. AudioManager MonoBehaviour (always)
        (scripts_dir / f"{schema.name}AudioManager.cs").write_text(
            env.get_template("AudioManager.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 3

        if variant == "music_layers":
            (scripts_dir / f"{schema.name}MusicLayers.cs").write_text(
                env.get_template("MusicLayers.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1
        if variant == "crossfade_zones":
            (scripts_dir / f"{schema.name}CrossfadeZone.cs").write_text(
                env.get_template("CrossfadeZone.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
