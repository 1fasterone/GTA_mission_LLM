"""Save/Load micro-generator — PlayerPrefs, JSON file, slots, checkpoint, autosave."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, CodeField, lint_extensible, lint_fields, clamp_enum,
    UNITY_EXTRA_INSTRUCTIONS,
)


SaveLoadVariant = Literal[
    "json_file", "playerprefs_kv", "save_slots",
    "checkpoint", "autosave", "encrypted_json",
]
VARIANTS = list(SaveLoadVariant.__args__)   # type: ignore[attr-defined]

Storage    = Literal["file", "playerprefs"]
SlotNaming = Literal["numbered", "named"]


class SaveLoadSchema(CodeExtensibleSchema):
    name:              str             = "SaveSystem"
    variant:           str             = "json_file"
    storage:           Storage         = "file"
    slot_naming:       SlotNaming      = "numbered"
    slot_count:        int             = 3
    autosave_interval: float           = 60.0
    save_on_quit:      bool            = True
    save_on_pause:     bool            = True
    default_file_name: str             = "save.json"
    encryption_key:    str             = "change-me"
    data_fields:       list[CodeField] = Field(default_factory=list)
    description:       str             = "Save/load system"
    meta:              dict            = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "json_file"

    @field_validator("storage", mode="before")
    @classmethod
    def _clamp_storage(cls, v: str) -> str:
        return clamp_enum(v, ("file", "playerprefs"), "file")

    @field_validator("slot_naming", mode="before")
    @classmethod
    def _clamp_slot_naming(cls, v: str) -> str:
        return clamp_enum(v, ("numbered", "named"), "numbered")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity save/load system for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What game state needs to persist (level, score, position, inventory, flags)
- Where it is stored (a JSON file in persistentDataPath, or PlayerPrefs)
- When it saves (manually, on quit/pause, on a timer, at checkpoints)
- Any slots or light obfuscation/encryption

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
STORAGE      — file | playerprefs
SLOT_NAMING  — numbered | named

`data_fields` lists the values that make up the saved game state. Each is a plain
serializable field on a [System.Serializable] data class (NOT a SerializeField):
  {{"name": "level", "type": "int", "default": "1"}}
Use Unity-serializable types only (int, float, bool, string, Vector3, arrays/List of those).

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "storage": "file",
  "slot_naming": "numbered",
  "slot_count": 3,
  "autosave_interval": 60.0,
  "save_on_quit": true,
  "save_on_pause": true,
  "default_file_name": "save.json",
  "data_fields": [
    {{"name": "level", "type": "int", "default": "1"}},
    {{"name": "score", "type": "int", "default": "0"}},
    {{"name": "playerName", "type": "string", "default": "\\"\\""}}
  ],
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class SaveLoadPipeline(UnityMicroPipeline):
    kind            = "saveload"
    display_name    = "Unity Save/Load"
    schema_cls      = SaveLoadSchema
    variants        = VARIANTS
    default_variant = "json_file"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "saveload"

    _RESERVED_FIELD_NAMES = {
        "data", "filename", "slotcount", "autosaveinterval",
        "encryptionkey", "player",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System", "System.IO", "System.Text"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "SaveSystem")

        # Clamp numerics.
        if schema.slot_count < 1:           schema.slot_count = 1
        if schema.autosave_interval <= 0:   schema.autosave_interval = 60.0
        if not schema.default_file_name.strip():
            schema.default_file_name = "save.json"
        if not schema.encryption_key.strip():
            schema.encryption_key = "change-me"

        # Variant → storage coherence (variant always wins).
        if variant == "playerprefs_kv":
            schema.storage = "playerprefs"
        else:
            schema.storage = "file"

        # Never emit an empty data class — give a sensible default payload.
        if not schema.data_fields:
            schema.data_fields = [
                CodeField(name="level",      type="int",    default="1"),
                CodeField(name="score",      type="int",    default="0"),
                CodeField(name="playerName", type="string", default='""'),
            ]
        if variant == "checkpoint" and not any(
            f.name == "checkpointPosition" for f in schema.data_fields
        ):
            schema.data_fields.append(CodeField(name="checkpointPosition", type="Vector3"))

        # Dedupe data-class members (no MonoBehaviour reserved set applies here).
        schema.data_fields = lint_fields(schema.data_fields, reserved=set())

        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}Data.cs").write_text(
            env.get_template("SaveData.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("SaveSystem.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 3
