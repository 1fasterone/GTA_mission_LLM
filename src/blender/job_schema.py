"""
Pydantic schema for a single Blender job — the JSON contract the manager runs.

Mirrors the shape the user specified::

    {
      "asset_type": "character",
      "style": "realistic",
      "rig": true,
      "animations": ["walk", "run", "attack"],
      "lods": true,
      "textures": { "resolution": 2048, "bake": true },
      "geometry_nodes": "human_base",
      "output": { "fbx": "golem_01.fbx",
                  "textures": "golem_01_textures/",
                  "blend": "golem_01.blend" }
    }

Same "clamp unknown enum → safe default" robustness the Unity schema uses
(src/unity_schema.py): a hallucinated style/asset_type never raises, it falls
back to a sane value so a bad LLM token can't break the queue.
"""

from __future__ import annotations

import re
from typing import Literal, Optional, get_args

from pydantic import BaseModel, Field, field_validator


# ── Enums (kept loose; validators clamp) ──────────────────────────────────────

AssetType = Literal[
    "character", "creature", "prop", "weapon", "vehicle",
    "building", "environment", "terrain", "foliage", "material",
]
Style = Literal[
    "realistic", "stylized", "low_poly", "cartoon", "pixel_art", "retro", "sci_fi",
]
# How the asset's textures/maps get produced.
#   procedural — Blender builds the maps itself (procedural material → bake)
#   diffusion  — a diffusion model paints the albedo from an LLM prompt; Blender
#                bakes the remaining maps (normal/AO/roughness) and assigns them
#   none       — untextured / vertex-colour only
TextureSource = Literal["procedural", "diffusion", "none"]

_VALID_ASSET_TYPES: frozenset[str] = frozenset(get_args(AssetType))
_VALID_STYLES:      frozenset[str] = frozenset(get_args(Style))
_VALID_TEX_SOURCES: frozenset[str] = frozenset(get_args(TextureSource))

# Canonical animation clip names we know how to retarget onto a Rigify rig.
_KNOWN_ANIMATIONS: frozenset[str] = frozenset({
    "idle", "walk", "run", "jump", "attack", "die", "death",
    "hit", "crouch", "swim", "fly", "fall", "land", "dance",
})

# The two fulfilment routes the manager dispatches to. A job is "blender" unless
# its textures need a diffusion model, in which case the diffusion pre-pass runs
# first and hands its output back to the same Blender job for baking + export.
JOB_FULFILMENT_ROUTES = ("blender", "diffusion")

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.\-/]")


def _slug(s: str, fallback: str) -> str:
    s = _SLUG_RE.sub("_", str(s).strip())
    return s or fallback


class TextureSpec(BaseModel):
    """Texture/material settings for a job."""
    resolution: int           = 1024
    bake:       bool          = True
    source:     TextureSource = "procedural"
    # Free-text material brief from the LLM — drives procedural node setup AND,
    # when source == "diffusion", the constructed diffusion prompt.
    material_brief: str = ""

    @field_validator("resolution", mode="before")
    @classmethod
    def _clamp_res(cls, v) -> int:
        """Snap to a power-of-two in [256, 4096] — GPUs and bakers want POT."""
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 1024
        n = max(256, min(4096, n))
        # round down to nearest power of two
        pot = 256
        while pot * 2 <= n:
            pot *= 2
        return pot

    @field_validator("source", mode="before")
    @classmethod
    def _clamp_source(cls, v) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_TEX_SOURCES else "procedural"


class OutputSpec(BaseModel):
    """Where the job writes its artifacts (paths are relative to the job's
    output dir unless absolute)."""
    fbx:      str = "asset.fbx"
    textures: str = "textures/"
    blend:    str = "asset.blend"
    alembic:  str = "asset.abc"      # baked deformation (soft body / cloth) for Unity

    @field_validator("fbx", "blend", "alembic", mode="before")
    @classmethod
    def _clean_file(cls, v) -> str:
        return _slug(v, "asset")

    @field_validator("textures", mode="before")
    @classmethod
    def _clean_dir(cls, v) -> str:
        d = _slug(v, "textures")
        return d if d.endswith("/") else d + "/"


class BlenderJob(BaseModel):
    """One unit of work for the Blender Job Manager."""

    # Identity / provenance
    name:       str = "asset"
    asset_type: AssetType = "prop"
    style:      Style     = "stylized"
    # Plain-English description carried from the LLM story → drives procedural
    # parameters and the diffusion prompt. This is the "use the llm story" hook.
    description: str = ""

    # Capabilities (the "sanitizer" toggles)
    rig:            bool      = False
    animations:     list[str] = Field(default_factory=list)
    lods:           bool      = False
    lod_levels:     int       = 3
    geometry_nodes: str       = ""          # named generator, e.g. "human_base"
    detail:         int       = 3           # 1–5 LLM knob: subdiv/displace/instances
    soft_body:      bool      = False       # jiggle physics (Blender sim; Unity needs bake)
    color:          str       = ""          # primary hex from the project palette
    seed:           Optional[int] = None    # fixed seed for reproducibility (None → derive from name)

    textures: TextureSpec = Field(default_factory=TextureSpec)
    output:   OutputSpec  = Field(default_factory=OutputSpec)

    # Bookkeeping the builder sets; not authored by the LLM.
    meta: dict = Field(default_factory=dict)

    # ── validators ────────────────────────────────────────────────────────────

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, v) -> str:
        return _slug(v, "asset")

    @field_validator("asset_type", mode="before")
    @classmethod
    def _clamp_type(cls, v) -> str:
        t = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        synonyms = {"npc": "character", "human": "character", "person": "character",
                    "monster": "creature", "animal": "creature", "enemy": "character",
                    "gun": "weapon", "car": "vehicle", "tree": "foliage",
                    "plant": "foliage", "house": "building", "level": "environment"}
        t = synonyms.get(t, t)
        return t if t in _VALID_ASSET_TYPES else "prop"

    @field_validator("style", mode="before")
    @classmethod
    def _clamp_style(cls, v) -> str:
        s = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        synonyms = {"realism": "realistic", "photoreal": "realistic",
                    "lowpoly": "low_poly", "toon": "cartoon",
                    "pixel": "pixel_art", "scifi": "sci_fi", "stylised": "stylized"}
        s = synonyms.get(s, s)
        return s if s in _VALID_STYLES else "stylized"

    @field_validator("animations", mode="before")
    @classmethod
    def _clamp_anims(cls, v) -> list[str]:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for a in v[:24]:
            name = str(a).lower().strip().replace("-", "_").replace(" ", "_")
            name = {"running": "run", "walking": "walk", "dying": "die",
                    "death": "die", "attacking": "attack"}.get(name, name)
            if name in _KNOWN_ANIMATIONS and name not in out:
                out.append(name)
        return out

    @field_validator("lod_levels", mode="before")
    @classmethod
    def _clamp_lods(cls, v) -> int:
        try:
            return max(1, min(5, int(v)))
        except (TypeError, ValueError):
            return 3

    @field_validator("geometry_nodes", mode="before")
    @classmethod
    def _clean_geo(cls, v) -> str:
        return _slug(v, "") if v else ""

    @field_validator("detail", mode="before")
    @classmethod
    def _clamp_detail(cls, v) -> int:
        try:
            return max(1, min(5, int(v)))
        except (TypeError, ValueError):
            return 3

    @field_validator("color", mode="before")
    @classmethod
    def _norm_color(cls, v) -> str:
        """Normalise to #RRGGBB, or "" when absent/invalid."""
        if not v:
            return ""
        s = str(v).strip()
        if not s.startswith("#"):
            s = "#" + s
        s = s.upper()
        return s if re.match(r"^#[0-9A-F]{6}$", s) else ""

    @field_validator("seed", mode="before")
    @classmethod
    def _coerce_seed(cls, v):
        """Accept int/str/None; clamp to a non-negative 32-bit int or None."""
        if v is None or v == "":
            return None
        try:
            return abs(int(v)) % (2 ** 32)
        except (TypeError, ValueError):
            return None

    # ── derived ────────────────────────────────────────────────────────────────

    def needs_diffusion(self) -> bool:
        """True when this job's textures must come from a diffusion model rather
        than from Blender's procedural material baking."""
        return self.textures.source == "diffusion"

    def default_output(self, base: str) -> "BlenderJob":
        """Fill output paths from the job name if the LLM left them generic."""
        if self.output.fbx in ("asset.fbx", "asset"):
            self.output.fbx = f"{self.name}.fbx"
        if self.output.blend in ("asset.blend", "asset"):
            self.output.blend = f"{self.name}.blend"
        if self.output.alembic in ("asset.abc", "asset"):
            self.output.alembic = f"{self.name}.abc"
        if self.output.textures in ("textures/", "textures"):
            self.output.textures = f"{self.name}_textures/"
        self.meta.setdefault("output_base", base)
        return self
