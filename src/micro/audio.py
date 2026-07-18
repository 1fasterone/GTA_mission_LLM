"""Audio micro-generator — AudioCue + AudioBank ScriptableObjects + AudioManager."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, UNITY_EXTRA_INSTRUCTIONS,
)


AudioVariant = Literal[
    "2d_simple", "3d_spatial", "music_layers", "crossfade_zones", "categorical_mixer",
]
VARIANTS = list(AudioVariant.__args__)   # type: ignore[attr-defined]

AudioCategory = Literal["music", "sfx", "ambient", "voice"]
_ALL_CATEGORIES: tuple[AudioCategory, ...] = ("music", "sfx", "ambient", "voice")
CUE_KINDS = ("auto", "synth", "gen", "tts", "library")


def _snake(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_") or "cue"


class AudioCueSpec(BaseModel):
    """One sound the bank needs. The LLM fills id/category/descriptor (+kind/prompt
    when it knows); the resolver/generator turn it into clips."""
    id:         str  = "cue"
    category:   str  = "sfx"           # music | sfx | ambient | voice
    kind:       str  = "auto"          # auto | synth | gen | tts | library
    descriptor: str  = ""              # plain-English sound description
    archetype:  str  = ""              # optional explicit synth archetype
    gen_prompt: str  = ""              # prompt for the generative backend
    text:       str  = ""              # spoken line (voice cues)
    voice:      str  = "default"
    spatial:    bool = False           # 3D positional
    loop:       bool = False           # looping (music/ambient)
    variations: int  = 1               # how many clip variants to bank
    duration_s: float = 0.5

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v): return _snake(v)

    @field_validator("category", mode="before")
    @classmethod
    def _cat(cls, v):
        s = str(v).strip().lower()
        return s if s in AudioCategory.__args__ else "sfx"   # type: ignore[attr-defined]

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        s = str(v).strip().lower()
        return s if s in CUE_KINDS else "auto"

    @field_validator("variations", mode="before")
    @classmethod
    def _var(cls, v):
        try: return max(1, min(8, int(v)))
        except (TypeError, ValueError): return 1

    @field_validator("duration_s", mode="before")
    @classmethod
    def _dur(cls, v):
        try: return max(0.05, min(20.0, float(v)))
        except (TypeError, ValueError): return 0.5


class AudioSchema(CodeExtensibleSchema):
    name:                 str  = "AudioBank"
    variant:              str  = "categorical_mixer"
    categories:           list[AudioCategory] = Field(
        default_factory=lambda: list(_ALL_CATEGORIES))
    bank_clip_count_hint: int  = 16
    pool_size:            int  = 8
    mixer_groups:         list[str] = Field(default_factory=lambda: ["Master", "Music", "SFX"])
    layer_count:          int  = 3
    crossfade_seconds:    float = 1.5
    game_type:            str  = ""    # shooter|platformer|racing|puzzle|rpg|generic
    cues:                 list[AudioCueSpec] = Field(default_factory=list)
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


class AudioPipeline(UnityMicroPipeline):
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

    def __init__(self, config: dict):
        super().__init__(config)
        self.config = config or {}
        self._gen_seed = 0
        self._gen_prompt = ""
        self._with_clips = None        # None -> config audio.generate_clips (default on)

    # Captures seed/prompt for the deterministic content pass and adds an LLM-free
    # path (`use_llm=False`) that builds the bank from the game-type's required cues.
    def run(self, user_prompt: str, *, use_llm: bool = True, with_clips=None,
            seed=None, **kw):
        self._gen_seed = int(seed) if seed is not None else 0
        self._gen_prompt = user_prompt or ""
        self._with_clips = with_clips
        if not use_llm:
            return self._run_no_llm(user_prompt, **kw)
        return super().run(user_prompt, seed=seed, **kw)

    def _run_no_llm(self, user_prompt, *, variant=None, out=None, **_):
        import json
        variant = variant if variant in self.variants else self.default_variant
        schema = self.schema_cls(variant=variant)
        schema.meta = {"user_prompt": user_prompt}
        schema = self.default_fix(schema, variant)
        schema.name = to_pascal(schema.name or "AudioBank", "AudioBank")
        # Unload LM Studio if stable3 backend is active (no concept to translate
        # on the --no-llm path, but VRAM still needs to be freed before inference).
        self._pre_write(schema, variant)
        out_dir = Path(out) if out else (self.output_root / schema.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        n = self.write_files(schema, variant, out_dir)
        (out_dir / f"{schema.name}.schema.json").write_text(
            json.dumps(schema.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[audio] (no-LLM) {schema.name}: {len(schema.cues)} cues, {n} file(s) -> {out_dir}")
        return out_dir

    def _pre_write(self, schema, variant) -> None:
        """Hook called after the fixer and before write_files.

        When the backend is `stable3`:
          1. Translate the Step-2 concept brief → per-cue gen_prompt while
             LM Studio is still loaded (richer prompts, zero extra cost).
          2. Unload LM Studio to free VRAM before Stable Audio 3 loads its models.

        Silently no-ops for other backends (dsp / local / none).
        """
        cfg = (self.config or {}).get("audio", {})
        if str(cfg.get("backend", "dsp")).strip().lower() != "stable3":
            return

        # Step 3.5 — enrich gen_prompts while the project LLM is still loaded
        concept = (getattr(schema, "meta", None) or {}).get("concept", "")
        if concept and getattr(schema, "cues", None):
            self._step(3, "Translating concept → Stable Audio 3 prompts")
            try:
                from ..audio.concept_translator import translate_concept
                n = translate_concept(concept, schema.cues, self.llm, self._gen_seed)
                if n:
                    self._ok(f"gen_prompt enriched for {n}/{len(schema.cues)} cues")
                else:
                    self._warn("concept translation produced no prompts — using descriptors")
            except Exception as exc:  # noqa: BLE001
                self._warn(
                    f"concept translation failed ({type(exc).__name__}: {exc}) "
                    "— descriptors will be used as prompts"
                )

        # Step 3.6 — unload LM Studio so Stable Audio 3 has the full 8 GB
        self._step(3, "Unloading LM Studio model to free VRAM for Stable Audio 3")
        try:
            from ..audio.backends import unload_lm_studio
            ok = unload_lm_studio(self.config)
            if ok:
                self._ok("LM Studio model unloaded — VRAM available for Stable Audio 3")
            else:
                self._warn(
                    "Could not unload LM Studio via API — "
                    "manually unload the model to avoid CUDA OOM on 8 GB VRAM"
                )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"unload request failed ({type(exc).__name__}: {exc})")

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
        # Inject cues so the bank is never empty.
        # Primary path: LLM reads the concept and extracts diegetic sound sources
        # from the world's own nouns/verbs — no genre lookup, no preset list.
        # Fallback (--no-llm or LLM failure): genre preset via required_cues().
        meta    = getattr(schema, "meta", None) or {}
        concept = meta.get("concept", "")
        prompt  = meta.get("user_prompt", self._gen_prompt)
        llm     = getattr(self, "llm", None)

        pre_cues = meta.get("_pre_cues")  # injected by main pipeline before LM Studio unload
        injected = False
        if pre_cues:
            # Compose path: cues were extracted from the game concept in the main pipeline
            # while LM Studio was still loaded.  No live LLM needed here.
            have = {c.id for c in schema.cues}
            for rc in pre_cues:
                cid = rc.get("id", "")
                if cid and cid not in have:
                    schema.cues.append(AudioCueSpec(**rc))
                    have.add(cid)
            injected = bool(schema.cues)
        elif concept and llm is not None:
            # Standalone path: LLM still available; extract cues live.
            try:
                from ..audio.resolver import extract_cues
                diegetic = extract_cues(concept, llm, n=10, seed=self._gen_seed)
                if diegetic:
                    have = {c.id for c in schema.cues}
                    for rc in diegetic:
                        if rc["id"] not in have:
                            schema.cues.append(AudioCueSpec(**rc))
                            have.add(rc["id"])
                    injected = True
            except Exception:  # noqa: BLE001 — always degrade to preset
                pass

        if not injected:
            from ..audio.resolver import required_cues, game_type_from_prompt
            schema.game_type = schema.game_type or game_type_from_prompt(prompt)
            have = {c.id for c in schema.cues}
            for rc in required_cues(schema.game_type):
                if rc["id"] not in have:
                    schema.cues.append(AudioCueSpec(**rc))
                    have.add(rc["id"])
        seen: set[str] = set(); deduped = []
        for c in schema.cues:
            if c.id not in seen:
                seen.add(c.id); deduped.append(c)
        schema.cues = deduped
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

        # Content pass: synthesize/generate the actual clips into Resources/Audio/
        # so the bank ships with sound, not an empty list. Gated by --clips /
        # config audio.generate_clips (default on). Degrades, never hard-fails.
        clips_on = self._with_clips
        if clips_on is None:
            clips_on = bool(self.config.get("audio", {}).get("generate_clips", True))
        if clips_on and schema.cues:
            try:
                from ..audio import generate_bank
                manifest = generate_bank(
                    [c.model_dump() for c in schema.cues],
                    out_dir / "Resources", self.config, seed=self._gen_seed)
                count += sum(len(e["clips"]) for e in manifest["cues"]) + 1  # + manifest
                for w in manifest.get("warnings", []):
                    self._warn(w)
                self._ok(f"audio content: {manifest['cue_count']} cues, "
                         f"backend={manifest['backend']}")
            except Exception as exc:  # noqa: BLE001 - content must never break codegen
                self._warn(f"clip generation skipped ({type(exc).__name__}: {exc})")
        return count
