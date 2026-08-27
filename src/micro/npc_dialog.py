"""NPC dialog micro-generator — Higgs Audio v2 TTS + multilingual NPC speech lines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


# ── Literals ───────────────────────────────────────────────────────────────────

NpcDialogVariant = Literal[
    "quest_giver", "vendor", "ambient_bark", "combat_chatter",
    "follower", "cutscene",
]
VARIANTS = list(NpcDialogVariant.__args__)   # type: ignore[attr-defined]

InteractionType = Literal["ambient", "quest", "vendor", "combat", "follower"]
AudioStyle      = Literal["natural", "stylized", "robotic", "whisper", "radio"]

INTERACTION_TYPES = ("ambient", "quest", "vendor", "combat", "follower")
AUDIO_STYLES      = ("natural", "stylized", "robotic", "whisper", "radio")

# Canonical set of ISO 639-1 codes recognised by the pipeline.
_VALID_ISO = {
    "en", "es", "fr", "de", "ja", "zh", "pt", "it",
    "ko", "ru", "ar", "hi", "nl", "pl", "sv", "tr",
    "cs", "da", "fi", "nb", "ro", "uk", "id", "vi", "th",
}

# Variant → sensible interaction type mapping (used in default_fix).
_VARIANT_INTERACTION: dict[str, str] = {
    "quest_giver":      "quest",
    "vendor":           "vendor",
    "ambient_bark":     "ambient",
    "combat_chatter":   "combat",
    "follower":         "follower",
    "cutscene":         "quest",
}

# Variant → suggested audio style.
_VARIANT_STYLE: dict[str, str] = {
    "quest_giver":    "natural",
    "vendor":         "natural",
    "ambient_bark":   "natural",
    "combat_chatter": "stylized",
    "follower":       "natural",
    "cutscene":       "natural",
}


def _snake(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_") or "line"


# ── Schema ─────────────────────────────────────────────────────────────────────

class DialogLine(BaseModel):
    """One NPC speech line — the atom of the dialog bank."""
    line_id:   str        = "line"
    text:      str        = ""
    emotion:   str        = "neutral"  # neutral|happy|sad|angry|fearful|whisper|calm|…
    language:  str        = "en"       # ISO 639-1
    voice_id:  str        = "default"
    tags:      list[str]  = Field(default_factory=list)
    higgs_prompt: str     = ""         # filled by resolver in default_fix()

    @field_validator("line_id", mode="before")
    @classmethod
    def _lid(cls, v): return _snake(v)

    @field_validator("text", "higgs_prompt", mode="before")
    @classmethod
    def _str(cls, v): return "" if v is None else str(v)

    @field_validator("emotion", mode="before")
    @classmethod
    def _emotion(cls, v):
        from ..audio.higgs import SUPPORTED_EMOTIONS
        s = str(v).strip().lower()
        return s if s in SUPPORTED_EMOTIONS else "neutral"

    @field_validator("language", mode="before")
    @classmethod
    def _lang(cls, v):
        c = str(v).strip().lower()[:2]
        return c if c in _VALID_ISO else "en"

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if not isinstance(v, list):
            return []
        return [str(t) for t in v if t]


class NpcDialogSchema(CodeExtensibleSchema):
    name:                 str              = "NpcDialog"
    variant:              str              = "quest_giver"
    speaker_name:         str              = "NPC"
    interaction_type:     str              = "quest"   # ambient|quest|vendor|combat|follower
    lines:                list[DialogLine] = Field(default_factory=list)
    multilingual_targets: list[str]        = Field(default_factory=list)
    audio_style:          str              = "natural" # natural|stylized|robotic|whisper|radio
    context_tags:         list[str]        = Field(default_factory=list)
    timing:               dict | None      = None      # line_id → beat time (seconds)
    prefab_path:          str              = ""        # Unity prefab / GTA ped model path
    description:          str              = "NPC dialog system"
    meta:                 dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "quest_giver"

    @field_validator("interaction_type", mode="before")
    @classmethod
    def _clamp_interaction(cls, v: str) -> str:
        return clamp_enum(v, INTERACTION_TYPES, "quest")

    @field_validator("audio_style", mode="before")
    @classmethod
    def _clamp_style(cls, v: str) -> str:
        return clamp_enum(v, AUDIO_STYLES, "natural")

    @field_validator("multilingual_targets", mode="before")
    @classmethod
    def _clamp_langs(cls, v: list) -> list:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for item in v:
            c = str(item).strip().lower()[:2]
            if c in _VALID_ISO and c not in out:
                out.append(c)
        return out


# ── Prompts ────────────────────────────────────────────────────────────────────

CONCEPT_PROMPT = """\
{theme_block}
Describe NPC dialog for this game idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Who the NPC is and what role they play in this world
- The mood, speech style, and any accent or mannerism
- What the NPC wants from the player: quest, sale, ambient atmosphere, combat taunts, or follower support
- Which languages the NPC might speak (ISO 639-1 codes)
- Whether timing is important (cutscenes, beat-precise cues)

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
INTERACTION_TYPE — ambient | quest | vendor | combat | follower
AUDIO_STYLE      — natural | stylized | robotic | whisper | radio
  Infer from the brief: "radio broadcast"/"vintage filter"/"old radio" → radio
                        "whisper"/"hushed"/"secret" → whisper
                        "robotic"/"synthetic"/"glitched" → robotic
                        "cinematic"/"dramatic"/"exaggerated" → stylized
                        anything else → natural
EMOTION (per line) — neutral | happy | sad | angry | fearful | surprised | whisper | calm | excited
LANGUAGE (per line) — ISO 639-1 two-letter code (en, es, fr, de, ja, zh, ...)

MULTILINGUAL_TARGETS — list of ISO 639-1 codes for additional language variants ([] if English only)

Each line in "lines" must have:
  "line_id": "snake_case_unique_id",
  "text": "The actual spoken line, in character",
  "emotion": "one of the emotion labels above",
  "language": "en",
  "voice_id": "default | male_deep | female_soft | elderly_male | child | robotic | radio_announcer",
  "tags": ["quest", "intro"]

Write real, in-character lines for THIS prompt — this is what reaches the audio files.
Include 3-8 lines for the speaker.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "speaker_name": "Bartender",
  "interaction_type": "quest",
  "audio_style": "<natural|stylized|robotic|whisper|radio — infer from brief>",
  "multilingual_targets": [],
  "context_tags": ["bar", "noir", "mystery"],
  "prefab_path": "",
  "lines": [
    {{"line_id": "greet",   "text": "Sit down. We need to talk.", "emotion": "neutral", "language": "en", "voice_id": "male_deep", "tags": ["intro"]}},
    {{"line_id": "task",    "text": "There's a man. Goes by Three-Fingers. Find him.", "emotion": "calm", "language": "en", "voice_id": "male_deep", "tags": ["quest", "clue"]}},
    {{"line_id": "warning", "text": "Watch yourself. He doesn't like questions.", "emotion": "fearful", "language": "en", "voice_id": "male_deep", "tags": ["quest", "warning"]}}
  ],
  "description": "One sentence."
}}

Your extra_fields/extra_methods/awake_extra/update_extra render into ONE class:
{{"name"}}Player (e.g. "BartenderPlayer") — the scene-attachable component
DialogTrigger calls into. Inside it, `this` IS the speaker: you already have
`public event Action OnDialogEnd` (and OnLine/OnLineEnd), plus public
StartDialog()/StopDialog()/Advance(). There is NO `_player` field to check or
call through — that field lives on the separate Trigger class, not this one.
""" + UNITY_EXTRA_INSTRUCTIONS


# ── Pipeline ──────────────────────────────────────────────────────────────────

class NpcDialogPipeline(UnityMicroPipeline):
    kind            = "npc_dialog"
    display_name    = "NPC Dialog (Higgs Audio v2 TTS)"
    schema_cls      = NpcDialogSchema
    variants        = VARIANTS
    default_variant = "quest_giver"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "npc_dialog"

    _RESERVED_FIELD_NAMES = {
        "lines", "current", "index", "speaker", "audiosource",
        "dialogmanifest", "_lines", "_index", "_current",
    }
    _TEMPLATE_USINGS = {
        "UnityEngine", "System", "System.Collections",
        "System.Collections.Generic", "UnityEngine.Networking",
    }

    def __init__(self, config: dict):
        super().__init__(config)
        self.config      = config or {}
        self._gen_seed   = 0
        self._gen_prompt = ""
        self._with_audio = None   # None → config audio.generate_clips (default on)
        self._audio_style: str | None = None  # CLI override; applied in default_fix

    def run(self, user_prompt: str, *, use_llm: bool = True,
            with_audio=None, seed=None, audio_style: str | None = None, **kw):
        self._gen_seed    = int(seed) if seed is not None else 0
        self._gen_prompt  = user_prompt or ""
        self._with_audio  = with_audio
        self._audio_style = audio_style
        # Suppress auto-theme detection for NPC dialog — themes distort character
        # voices more than they help. Pass --theme <name> to opt in explicitly.
        if kw.get("theme") is None:
            kw["theme"] = "none"
        if not use_llm:
            return self._run_no_llm(user_prompt, **kw)
        return super().run(user_prompt, seed=seed, **kw)

    def _run_no_llm(self, user_prompt, *, variant=None, out=None, **_):
        variant = variant if variant in self.variants else self.default_variant
        schema  = self.schema_cls(variant=variant)
        schema.meta = {"user_prompt": user_prompt}
        schema  = self.default_fix(schema, variant)
        schema.name = to_pascal(schema.name or "NpcDialog", "NpcDialog")
        self._pre_write(schema, variant)
        out_dir = Path(out) if out else (self.output_root / schema.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        n = self.write_files(schema, variant, out_dir)
        (out_dir / f"{schema.name}.schema.json").write_text(
            json.dumps(schema.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8")
        print(f"[npc_dialog] (no-LLM) {schema.name}: "
              f"{len(schema.lines)} lines, {n} file(s) -> {out_dir}")
        return out_dir

    def _pre_write(self, schema, variant) -> None:
        """Step 3.5 — build per-line higgs_prompt while LM Studio is still loaded.

        The Higgs Audio v2 model is loaded later in write_files() (Step 6) only when
        `with_audio` is True. By enriching prompts here (while the project LLM is warm)
        we can guide Higgs without a second LLM call later.

        This hook also unloads LM Studio to free VRAM for Higgs inference.
        """
        cfg     = (self.config or {}).get("audio", {})
        backend = str(cfg.get("backend", "higgs")).strip().lower()
        if backend not in ("higgs",):
            return     # No VRAM management needed for non-Higgs backends

        concept = (getattr(schema, "meta", None) or {}).get("concept", "")
        llm     = getattr(self, "llm", None)

        # Fill higgs_prompt for lines that don't have one yet
        if concept and llm is not None and schema.lines:
            self._step(3, "Building per-line Higgs TTS prompts")
            enriched = 0
            for line in schema.lines:
                if not line.higgs_prompt:
                    line.higgs_prompt = (
                        f"{line.emotion} voice, {schema.audio_style} style, "
                        f"language {line.language}: {line.text}"
                    )
                    enriched += 1
            if enriched:
                self._ok(f"higgs_prompt filled for {enriched}/{len(schema.lines)} lines")

        # Unload LM Studio to free VRAM for Higgs
        self._step(3, "Unloading LM Studio to free VRAM for Higgs Audio v2")
        try:
            from ..audio.backends import unload_lm_studio
            ok = unload_lm_studio(self.config)
            if ok:
                self._ok("LM Studio unloaded — VRAM available for Higgs Audio v2")
            else:
                self._warn(
                    "Could not unload LM Studio — "
                    "manually unload the model to avoid CUDA OOM on 8 GB VRAM"
                )
        except Exception as exc:  # noqa: BLE001
            self._warn(f"unload request failed ({type(exc).__name__}: {exc})")

    def default_fix(self, schema, variant) -> NpcDialogSchema:
        from ..audio.dialog_resolver import (
            resolve_voice, resolve_emotion, resolve_language,
            default_lines, default_voice_for,
        )

        # CLI --audio-style override takes precedence over everything else
        _audio_style = getattr(self, "_audio_style", None)
        if _audio_style:
            schema.audio_style = clamp_enum(_audio_style, AUDIO_STYLES, "natural")

        schema.name     = to_pascal(schema.name, "NpcDialog")
        schema.variant  = variant

        # Variant → interaction_type / audio_style coherence
        if variant in _VARIANT_INTERACTION and not schema.interaction_type:
            schema.interaction_type = _VARIANT_INTERACTION[variant]
        if variant in _VARIANT_STYLE and schema.audio_style == "natural" \
                and not getattr(self, "_audio_style", None):
            schema.audio_style = _VARIANT_STYLE[variant]

        # Normalise language codes
        schema.multilingual_targets = [
            resolve_language(c) for c in schema.multilingual_targets
        ]

        # Normalise each line
        for line in schema.lines:
            line.emotion  = resolve_emotion(line.emotion)
            line.language = resolve_language(line.language)
            line.voice_id = resolve_voice(line.voice_id, self.config) if line.voice_id else \
                            default_voice_for(schema.interaction_type)
            # Fill higgs_prompt if still empty
            if not line.higgs_prompt:
                line.higgs_prompt = (
                    f"{line.emotion} {schema.audio_style} {line.language}: {line.text}"
                )

        # Inject lines if empty — primary: pre-extracted (compose) / LLM extraction, fallback: presets
        if not schema.lines:
            meta    = getattr(schema, "meta", {}) or {}
            concept = meta.get("concept", "")
            llm     = getattr(self, "llm", None)
            injected = False
            # Compose path: concept-specific lines pre-extracted by the main
            # pipeline while LM Studio was still loaded (mirrors audio meta._pre_cues),
            # so emit stays LLM-free but the dialog is concept-driven, not preset.
            pre_lines = meta.get("_pre_lines")
            if pre_lines:
                defv = default_voice_for(schema.interaction_type)
                schema.lines = [
                    DialogLine(
                        line_id=d["line_id"],
                        text=d["text"],
                        emotion=d.get("emotion", "neutral"),
                        language=schema.multilingual_targets[0]
                                 if schema.multilingual_targets else "en",
                        voice_id=defv,
                        tags=d.get("tags", []),
                        higgs_prompt=f"{d.get('emotion','neutral')} {schema.audio_style}: {d['text']}",
                    )
                    for d in pre_lines
                ]
                injected = True
            if not injected and concept and llm is not None:
                try:
                    from ..audio.dialog_resolver import extract_dialog_lines
                    raw_lines = extract_dialog_lines(
                        concept, llm,
                        speaker=schema.speaker_name,
                        interaction_type=schema.interaction_type,
                        seed=self._gen_seed,
                    )
                    if raw_lines:
                        defv = default_voice_for(schema.interaction_type)
                        schema.lines = [
                            DialogLine(
                                line_id=d["line_id"],
                                text=d["text"],
                                emotion=d.get("emotion", "neutral"),
                                language=schema.multilingual_targets[0]
                                         if schema.multilingual_targets else "en",
                                voice_id=defv,
                                tags=d.get("tags", []),
                                higgs_prompt=f"{d.get('emotion','neutral')} {schema.audio_style}: {d['text']}",
                            )
                            for d in raw_lines
                        ]
                        injected = True
                except Exception:  # noqa: BLE001
                    pass
            if not injected:
                defv = default_voice_for(schema.interaction_type)
                schema.lines = [
                    DialogLine(
                        line_id=d["line_id"],
                        text=d["text"],
                        emotion=d.get("emotion", "neutral"),
                        language="en",
                        voice_id=defv,
                        tags=d.get("tags", []),
                        higgs_prompt=f"{d.get('emotion','neutral')} {schema.audio_style}: {d['text']}",
                    )
                    for d in default_lines(schema.interaction_type)
                ]

        # Deduplicate line IDs
        seen: set[str] = set()
        deduped: list[DialogLine] = []
        for line in schema.lines:
            if line.line_id in seen:
                line.line_id = f"{line.line_id}_{len(deduped)}"
            seen.add(line.line_id)
            deduped.append(line)
        schema.lines = deduped

        # Prefab path default
        if not schema.prefab_path:
            schema.prefab_path = f"Prefabs/NPCs/{schema.speaker_name}"

        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def attach_class(self, schema) -> str:
        """The scene-attachable MonoBehaviour for npc_dialog is <name>Player — the
        component exposing StartDialog/Advance/StopDialog. The bare <name> is NOT a
        class (the templates only define <name>Player/Trigger/Data), so the default
        attach_class (schema.name) would make GameWiring emit a CS0246 reference."""
        return f"{schema.name}Player"

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env         = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # 1. DialogPlayer.cs — MonoBehaviour that sequences and plays lines
        (scripts_dir / f"{schema.name}Player.cs").write_text(
            env.get_template("DialogPlayer.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        # 2. DialogTrigger.cs — Proximity / interact trigger wrapper
        (scripts_dir / f"{schema.name}Trigger.cs").write_text(
            env.get_template("DialogTrigger.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        # 3. DialogData.json — runtime data (all lines, emotions, paths)
        data_dir = out_dir / "Data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / f"{schema.name}Data.json").write_text(
            env.get_template("DialogData.json.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 3

        # 4. README
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1

        # 5. Schema sidecar (for inspect / re-run)
        (out_dir / f"{schema.name}.schema.json").write_text(
            json.dumps(schema.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        count += 1

        # 6. Audio content pass — generate WAV files via Higgs Audio v2
        audio_on = self._with_audio
        if audio_on is None:
            # In --compose the unit is emitted with meta.skip_audio so codegen stays
            # fast + LLM/Higgs-free (the dedicated audio micro owns clip generation);
            # standalone `micro npc_dialog` still honours config audio.generate_clips.
            if (getattr(schema, "meta", None) or {}).get("skip_audio"):
                audio_on = False
            else:
                audio_on = bool(self.config.get("audio", {}).get("generate_clips", True))
        if audio_on and schema.lines:
            try:
                from ..audio.dialog_content import generate_dialog_bank
                manifest = generate_dialog_bank(
                    lines=[l.model_dump() for l in schema.lines],
                    speaker=schema.speaker_name,
                    resources_dir=out_dir / "Resources",
                    config=self.config,
                    multilingual_targets=schema.multilingual_targets,
                    seed=self._gen_seed,
                )
                wav_count = sum(len(e["clips"]) for e in manifest["lines"])
                count += wav_count + 1   # +1 for manifest JSON
                for w in manifest.get("warnings", []):
                    self._warn(w)
                self._ok(
                    f"dialog audio: {manifest['line_count']} lines, "
                    f"{wav_count} clips "
                    f"({'text-only' if wav_count == 0 else 'WAV'} output)"
                )
            except Exception as exc:  # noqa: BLE001 — audio never breaks codegen
                self._warn(f"dialog audio skipped ({type(exc).__name__}: {exc})")

        return count
