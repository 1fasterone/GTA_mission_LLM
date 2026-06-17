"""
Pydantic schema models for Unity project generation.
Two stages:
  UnitySchema (raw) — plain English descriptions, no package IDs
  UnitySchema (resolved) — package IDs + versions filled by RAG/lookup
"""

from __future__ import annotations
import re
from typing import Literal, Optional, get_args
from pydantic import BaseModel, Field, field_validator, model_validator

from .micro.code_fields import CodeField, CodeMethod


GameType      = Literal["2D", "2.5D", "3D", "VR", "top_down", "isometric"]
Platform      = Literal["pc", "mobile", "console", "webgl", "all"]
RenderPipeline = Literal["URP", "HDRP", "builtin"]
UnityVersion  = Literal["6000.0", "6000.1", "6000.4", "2022.3", "2021.3"]
ScriptType    = Literal[
    "player", "camera", "game_manager", "enemy",
    "collectible", "ui_manager", "audio_manager", "health",
    "dialogue_manager", "mesh_builder", "procedural_mesh",
    "level_generator", "material_palette", "custom"
]
ScriptAttach  = Literal["player", "camera", "game_manager", "systems"]
VisualStyle   = Literal["cartoon", "realistic", "low_poly", "stylized", "pixel_art", "retro"]
FeatureFlag   = Literal[
    "physics", "ui_toolkit", "input_system", "cinemachine",
    "tilemaps", "navmesh", "audio", "networking",
    "addressables", "localization", "timeline", "animation_rigging"
]

_VALID_GAME_TYPES:    frozenset[str] = frozenset(get_args(GameType))
_VALID_PLATFORMS:     frozenset[str] = frozenset(get_args(Platform))
_VALID_PIPELINES:     frozenset[str] = frozenset(get_args(RenderPipeline))
_VALID_VERSIONS:      frozenset[str] = frozenset(get_args(UnityVersion))
_VALID_SCRIPT_TYPES:  frozenset[str] = frozenset(get_args(ScriptType))
_VALID_FEATURES:      frozenset[str] = frozenset(get_args(FeatureFlag))
_VALID_VISUAL_STYLES: frozenset[str] = frozenset(get_args(VisualStyle))

# ── Hex color helpers ──────────────────────────────────────────────────────────

_HEX_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


def _norm_hex(v: str, default: str = "#FFFFFF") -> str:
    """Normalise a CSS hex color to #RRGGBB uppercase, or return default."""
    s = str(v).strip()
    if not s.startswith('#'):
        s = '#' + s
    s = s.upper()
    return s if _HEX_RE.match(s) else default


# Style-specific default palettes (applied when LLM omits color_palette)
_DEFAULT_PALETTES: dict[str, dict[str, str]] = {
    "cartoon":   {"name": "Cartoon",   "primary": "#4A90E2", "secondary": "#7ED321",
                  "accent": "#F5A623", "background": "#87CEEB", "trim": "#FFFFFF"},
    "realistic": {"name": "Realistic", "primary": "#8B6355", "secondary": "#5D8A5E",
                  "accent": "#C19A6B", "background": "#4A7FA5", "trim": "#BDC3C7"},
    "low_poly":  {"name": "LowPoly",   "primary": "#E74C3C", "secondary": "#27AE60",
                  "accent": "#F1C40F", "background": "#2980B9", "trim": "#ECF0F1"},
    "stylized":  {"name": "Stylized",  "primary": "#9B59B6", "secondary": "#1ABC9C",
                  "accent": "#E67E22", "background": "#2C3E50", "trim": "#F0F0F0"},
    "pixel_art": {"name": "PixelArt",  "primary": "#3498DB", "secondary": "#2ECC71",
                  "accent": "#E74C3C", "background": "#1A1A2E", "trim": "#FFFFFF"},
    "retro":     {"name": "Retro",     "primary": "#FF6B35", "secondary": "#004E89",
                  "accent": "#F7C59F", "background": "#1A1A2E", "trim": "#EBEBEB"},
}

TriggerType   = Literal["on_start", "on_enter_zone", "on_enemy_death", "on_pickup", "on_timer", "manual"]
AudioCategory = Literal["music", "sfx", "ambient", "voice"]
MeshType      = Literal["terrain", "room", "road", "water", "custom"]

_VALID_TRIGGERS:   frozenset[str] = frozenset(get_args(TriggerType))
_VALID_CATEGORIES: frozenset[str] = frozenset(get_args(AudioCategory))
_VALID_MESH_TYPES: frozenset[str] = frozenset(get_args(MeshType))


class StoryBeat(BaseModel):
    id:          str
    label:       str
    description: str
    trigger:     str = "manual"

    @field_validator("trigger", mode="before")
    @classmethod
    def normalise_trigger(cls, v: str) -> str:
        t = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        return t if t in _VALID_TRIGGERS else "manual"


class AudioTrack(BaseModel):
    name:        str
    description: str
    loop:        bool = True
    category:    str  = "music"

    @field_validator("category", mode="before")
    @classmethod
    def normalise_category(cls, v: str) -> str:
        c = str(v).lower().strip()
        return c if c in _VALID_CATEGORIES else "music"


class ProceduralMesh(BaseModel):
    name:        str
    description: str
    mesh_type:   str = "custom"
    resolution:  int = 32

    @field_validator("mesh_type", mode="before")
    @classmethod
    def normalise_mesh_type(cls, v: str) -> str:
        t = str(v).lower().strip()
        return t if t in _VALID_MESH_TYPES else "custom"

    @field_validator("resolution", mode="before")
    @classmethod
    def clamp_resolution(cls, v) -> int:
        try:
            return max(8, min(128, int(v)))
        except (TypeError, ValueError):
            return 32


EntityShape     = Literal["cube", "sphere", "capsule", "cylinder", "quad", "plane"]
EntityPlacement = Literal["single", "line", "ring", "grid", "scatter", "path"]
EntityColorRole = Literal["primary", "secondary", "accent", "trim", "background"]

_VALID_ENTITY_SHAPES:     frozenset[str] = frozenset(get_args(EntityShape))
_VALID_ENTITY_PLACEMENTS: frozenset[str] = frozenset(get_args(EntityPlacement))
_VALID_ENTITY_ROLES:      frozenset[str] = frozenset(get_args(EntityColorRole))


class EntitySpec(BaseModel):
    """A prompt-derived group of scene objects: N primitives placed by a rule.

    This is the Tier-2 vocabulary that lets a concept's *content* reach the
    scene — "6 rival cars", "a ring of buildings", "a crowd of spheres" — instead
    of the one hardcoded player+enemy the base scene emits. A deterministic
    bootstrap turns each EntitySpec into `count` GameObjects at the given layout.
    """
    name:       str   = "Entities"
    kind:       str   = "prop"          # free label: enemy, building, crowd, pickup…
    count:      int   = 1
    shape:      str   = "cube"
    placement:  str   = "single"
    color_role: str   = "accent"
    spacing:    float = 2.0             # units between instances (line/grid/ring)
    radius:     float = 8.0             # ring/scatter radius
    scale:      float = 1.0
    tag:        str   = "Untagged"      # e.g. "Enemy" so AI/health find them
    behavior:   str   = ""              # optional: name of a script to attach
    # placement == "path": world-space [x, y, z] points (y = elevation!). The
    # spawner lays the instances along this polyline AND emits a "Waypoints"
    # container of empty GameObjects at the points — winding roads, patrol
    # routes, and racing lines that waypoint-following AI can consume.
    path_points: list[list[float]] = Field(default_factory=list)

    @field_validator("path_points", mode="before")
    @classmethod
    def _clamp_path(cls, v) -> list:
        """Coerce to ≤ 64 [x, y, z] float triples; drop anything malformed."""
        if not isinstance(v, list):
            return []
        out: list[list[float]] = []
        for p in v[:64]:
            try:
                nums = [float(x) for x in list(p)[:3]]
            except (TypeError, ValueError):
                continue
            while len(nums) < 3:
                nums.append(0.0)
            out.append([max(-2000.0, min(2000.0, n)) for n in nums])
        return out

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        try:
            return max(0, min(200, int(v)))   # cap so a hallucinated 99999 can't bomb the scene
        except (TypeError, ValueError):
            return 1

    @field_validator("shape", mode="before")
    @classmethod
    def _clamp_shape(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in _VALID_ENTITY_SHAPES else "cube"

    @field_validator("placement", mode="before")
    @classmethod
    def _clamp_placement(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in _VALID_ENTITY_PLACEMENTS else "single"

    @field_validator("color_role", mode="before")
    @classmethod
    def _clamp_role(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in _VALID_ENTITY_ROLES else "accent"

    @field_validator("spacing", "radius", "scale", mode="before")
    @classmethod
    def _clamp_floats(cls, v) -> float:
        try:
            return max(0.05, min(500.0, float(v)))
        except (TypeError, ValueError):
            return 1.0


class EnvironmentSpec(BaseModel):
    """Scene mood — time of day drives the sun's angle/color; fog sets the tone.
    Lets "a mountain road at sunset" actually look like sunset."""
    time_of_day:   str = "day"           # dawn | day | sunset | night
    fog:           Optional[bool]  = None   # None → visual-style default
    fog_color:     Optional[str]   = None   # #RRGGBB; None → palette background
    sun_intensity: Optional[float] = None   # None → time-of-day preset

    @field_validator("time_of_day", mode="before")
    @classmethod
    def _clamp_tod(cls, v: str) -> str:
        s = str(v).lower().strip()
        synonyms = {"morning": "dawn", "sunrise": "dawn", "noon": "day",
                    "midday": "day", "dusk": "sunset", "evening": "sunset",
                    "midnight": "night", "dark": "night"}
        s = synonyms.get(s, s)
        return s if s in ("dawn", "day", "sunset", "night") else "day"

    @field_validator("fog_color", mode="before")
    @classmethod
    def _norm_fog_color(cls, v):
        if v is None or str(v).strip() == "":
            return None
        return _norm_hex(str(v))

    @field_validator("sun_intensity", mode="before")
    @classmethod
    def _clamp_sun(cls, v):
        if v is None or str(v).strip() == "":
            return None
        try:
            return max(0.0, min(3.0, float(v)))
        except (TypeError, ValueError):
            return None


class ColorPalette(BaseModel):
    """Six named hex colors that drive material generation and scene lighting."""
    name:       str = "Stylized"
    primary:    str = "#9B59B6"   # player / protagonist objects
    secondary:  str = "#1ABC9C"   # ground / terrain / environment
    accent:     str = "#E67E22"   # enemies / collectibles / highlights
    background: str = "#2C3E50"   # sky / camera clear color
    trim:       str = "#F0F0F0"   # UI / trim / neutral geometry

    @field_validator("primary", "secondary", "accent", "background", "trim", mode="before")
    @classmethod
    def normalise_hex(cls, v: str) -> str:
        return _norm_hex(v)


class UnityPackage(BaseModel):
    description: str                    # e.g. "universal render pipeline"
    package_id:  Optional[str] = None   # e.g. "com.unity.render-pipelines.universal"
    version:     Optional[str] = None   # e.g. "14.0.11"


class UnityScript(BaseModel):
    name:        str        # class name, e.g. "PlayerController"
    type:        ScriptType # drives which template to use
    description: str        # plain English: "first-person character controller"

    # ── Custom-script channel (type == "custom") ──────────────────────────────
    # When a concept isn't covered by any fixed template (drift assist, headlight
    # toggle, acid rain…), the LLM authors the behavior itself as typed C#
    # fragments — same proven channel the micro generators use (code_fields.py).
    # The generator lints them and renders CustomBehaviour.cs.j2. `attach_to`
    # picks which scene GameObject hosts the component.
    attach_to:          str              = "systems"
    extra_usings:       list[str]        = Field(default_factory=list)
    extra_fields:       list[CodeField]  = Field(default_factory=list)
    awake_extra:        str              = ""
    update_extra:       str              = ""
    fixed_update_extra: str              = ""
    extra_methods:      list[CodeMethod] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def normalise_type(cls, v: str) -> str:
        # Unknown types used to clamp to "player" (rendering a duplicate fixed
        # template under an invented name). They now become "custom" so the
        # LLM-authored fragments carry the behavior instead.
        t = str(v).lower().strip()
        return t if t in _VALID_SCRIPT_TYPES else "custom"

    @field_validator("attach_to", mode="before")
    @classmethod
    def normalise_attach(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in frozenset(get_args(ScriptAttach)) else "systems"


class UnitySchema(BaseModel):
    project_name:    str
    company_name:    str          = "MyCompany"
    description:     str          = ""
    game_type:       GameType
    platform:        Platform
    locomotion:      Literal["walk", "drive", "fly", "swim"] = "walk"
    render_pipeline: RenderPipeline
    unity_version:   UnityVersion = "6000.0"
    color_space:     Literal["Linear", "Gamma"] = "Linear"
    visual_style:    str          = "stylized"
    scenes:          list[str]        = Field(default_factory=lambda: ["Main", "MainMenu"])
    features:        list[str]        = Field(default_factory=list)
    packages:        list[UnityPackage] = Field(default_factory=list, max_length=20)
    scripts:           list[UnityScript]       = Field(default_factory=list, max_length=24)
    story_beats:       list[StoryBeat]         = Field(default_factory=list)
    audio_tracks:      list[AudioTrack]        = Field(default_factory=list)
    procedural_meshes: list[ProceduralMesh]    = Field(default_factory=list)
    entities:          list[EntitySpec]        = Field(default_factory=list, max_length=40)
    environment:       Optional[EnvironmentSpec] = None
    color_palette:     Optional[ColorPalette]  = None
    meta:              dict                    = Field(default_factory=dict)   # theme/seed bookkeeping

    @field_validator("game_type", mode="before")
    @classmethod
    def normalise_game_type(cls, v: str) -> str:
        """Accept '2d'/'2D'/'2.5d'/'3d' etc."""
        t = str(v).strip()
        # normalise casing
        upper = t.upper()
        if upper in {"2D", "2.5D", "3D", "VR"}:
            return upper
        lower = t.lower()
        if lower in {"top_down", "topdown", "top-down"}:
            return "top_down"
        if lower in {"isometric", "iso"}:
            return "isometric"
        return t if t in _VALID_GAME_TYPES else "3D"

    @field_validator("platform", mode="before")
    @classmethod
    def normalise_platform(cls, v: str) -> str:
        p = str(v).lower().strip().replace("-", "_")
        return p if p in _VALID_PLATFORMS else "pc"

    @field_validator("locomotion", mode="before")
    @classmethod
    def normalise_locomotion(cls, v: str) -> str:
        """How the player moves — routes compose to the matching controller
        (drive → real WheelCollider car, fly/swim → those variants)."""
        s = str(v).lower().strip()
        synonyms = {
            "driving": "drive", "car": "drive", "vehicle": "drive", "racing": "drive",
            "flying": "fly", "flight": "fly",
            "swimming": "swim", "underwater": "swim", "diving": "swim",
            "walking": "walk", "running": "walk", "on_foot": "walk",
        }
        s = synonyms.get(s, s)
        return s if s in ("walk", "drive", "fly", "swim") else "walk"

    @field_validator("render_pipeline", mode="before")
    @classmethod
    def normalise_pipeline(cls, v: str) -> str:
        p = str(v).upper().strip()
        if p in {"UNIVERSAL", "URP", "LIGHTWEIGHT", "LWRP"}:
            return "URP"
        if p in {"HIGH DEFINITION", "HDRP", "HD"}:
            return "HDRP"
        if p in {"BUILTIN", "BUILT-IN", "BUILT_IN", "STANDARD", "LEGACY"}:
            return "builtin"
        return p if p in _VALID_PIPELINES else "URP"

    @field_validator("unity_version", mode="before")
    @classmethod
    def normalise_version(cls, v: str) -> str:
        s = str(v).strip()
        # Accept "6", "6.0", "6000", "6000.0", "Unity 6", "6000.4", etc.
        if "6000.4" in s:
            return "6000.4"
        if "6000.1" in s:
            return "6000.1"
        if s.startswith("6") and ("6000" in s or s in {"6", "6.0"}):
            return "6000.0"
        if "2022" in s:
            return "2022.3"
        if "2021" in s:
            return "2021.3"
        return s if s in _VALID_VERSIONS else "6000.0"

    @field_validator("features", mode="before")
    @classmethod
    def normalise_features(cls, v: list) -> list:
        """Clamp any unknown feature flag to nothing rather than raising."""
        return [f for f in (str(x).lower().strip() for x in v) if f in _VALID_FEATURES]

    @field_validator("visual_style", mode="before")
    @classmethod
    def normalise_visual_style(cls, v: str) -> str:
        s = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        return s if s in _VALID_VISUAL_STYLES else "stylized"

    @field_validator("color_space", mode="before")
    @classmethod
    def normalise_color_space(cls, v: str) -> str:
        s = str(v).strip().title()
        return s if s in ("Linear", "Gamma") else "Linear"

    @model_validator(mode="after")
    def derive_color_space(self) -> "UnitySchema":
        """HDRP/URP prefer Linear; builtin can use Gamma."""
        if self.render_pipeline in ("URP", "HDRP"):
            self.color_space = "Linear"
        return self

    @model_validator(mode="after")
    def fill_default_palette(self) -> "UnitySchema":
        """Populate color_palette from visual_style defaults if omitted."""
        if self.color_palette is None:
            pal_data = _DEFAULT_PALETTES.get(self.visual_style,
                                             _DEFAULT_PALETTES["stylized"])
            self.color_palette = ColorPalette(**pal_data)
        return self

    def unresolved_packages(self) -> list[str]:
        return [p.description for p in self.packages if not p.package_id]
