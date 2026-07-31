"""
Pydantic schema models for Unity project generation.
Two stages:
  UnitySchema (raw) — plain English descriptions, no package IDs
  UnitySchema (resolved) — package IDs + versions filled by RAG/lookup

The schema is the IR (intermediate representation) for the full multimodal
compiler pipeline. Steps 4-6 are deterministic; only Steps 2-3 are LLM-driven.
"""

from __future__ import annotations
import re
from typing import Any, Literal, Optional, get_args
from pydantic import BaseModel, Field, field_validator, model_validator

from .micro.code_fields import CodeField, CodeMethod
from .atoms.base import AtomInvocation


# ── Core engine literals ───────────────────────────────────────────────────────

GameType       = Literal["2D", "2.5D", "3D", "VR", "top_down", "2d_topdown", "isometric"]
Platform       = Literal["pc", "mobile", "console", "webgl", "all"]
RenderPipeline = Literal["URP", "HDRP", "builtin"]
UnityVersion   = Literal["6000.0", "6000.1", "6000.4", "2022.3", "2021.3"]
ScriptType     = Literal[
    "player", "camera", "game_manager", "enemy",
    "collectible", "ui_manager", "audio_manager", "health",
    "dialogue_manager", "mesh_builder", "procedural_mesh",
    "level_generator", "material_palette", "custom"
]
ScriptAttach   = Literal["player", "camera", "game_manager", "systems"]
VisualStyle    = Literal["cartoon", "realistic", "low_poly", "stylized", "pixel_art", "retro"]
FeatureFlag    = Literal[
    "physics", "ui_toolkit", "input_system", "cinemachine",
    "tilemaps", "navmesh", "audio", "networking",
    "addressables", "localization", "timeline", "animation_rigging"
]
TriggerType    = Literal["on_start", "on_enter_zone", "on_enemy_death", "on_pickup", "on_timer", "manual"]
AudioCategory  = Literal["music", "sfx", "ambient", "voice"]
MeshType       = Literal["terrain", "room", "road", "water", "custom"]

# ── Multimodal IR literals ────────────────────────────────────────────────────

AudioGenMethod    = Literal["sfxr", "stable_audio", "elevenlabs", "procedural", "recorded"]
MaterialGenMethod = Literal["procedural", "substance", "sdxl", "imported"]
ModelCategory     = Literal["vehicle", "character", "prop", "environment"]
ModelGenMethod    = Literal["blender_microgen", "parametric_base", "imported"]
VfxType           = Literal["particle", "volumetric", "shader_fx"]
ShaderLanguage    = Literal["shadergraph", "hlsl", "glsl"]
BTNodeType        = Literal["sequence", "selector", "action", "condition", "decorator"]
WiringType        = Literal["uses", "triggers", "references", "contains"]
CharacterRole     = Literal["player", "npc", "enemy", "boss", "companion"]
MissionObjType    = Literal["reach", "collect", "defeat", "survive", "escort", "timer"]
WeatherType       = Literal["clear", "rain", "storm", "snow", "fog", "wind", "overcast"]
WorldScale        = Literal["tiny", "small", "medium", "large", "massive"]
AIBehaviorKind    = Literal["idle", "patrol", "guard", "chase", "flee", "attack", "custom"]

# ── Validation sets ───────────────────────────────────────────────────────────

_VALID_GAME_TYPES:    frozenset[str] = frozenset(get_args(GameType))
_VALID_PLATFORMS:     frozenset[str] = frozenset(get_args(Platform))
_VALID_PIPELINES:     frozenset[str] = frozenset(get_args(RenderPipeline))
_VALID_VERSIONS:      frozenset[str] = frozenset(get_args(UnityVersion))
_VALID_SCRIPT_TYPES:  frozenset[str] = frozenset(get_args(ScriptType))
_VALID_FEATURES:      frozenset[str] = frozenset(get_args(FeatureFlag))
_VALID_VISUAL_STYLES: frozenset[str] = frozenset(get_args(VisualStyle))
_VALID_TRIGGERS:      frozenset[str] = frozenset(get_args(TriggerType))
_VALID_CATEGORIES:    frozenset[str] = frozenset(get_args(AudioCategory))
_VALID_MESH_TYPES:    frozenset[str] = frozenset(get_args(MeshType))

_VALID_AUDIO_GEN:     frozenset[str] = frozenset(get_args(AudioGenMethod))
_VALID_MAT_GEN:       frozenset[str] = frozenset(get_args(MaterialGenMethod))
_VALID_MODEL_CAT:     frozenset[str] = frozenset(get_args(ModelCategory))
_VALID_MODEL_GEN:     frozenset[str] = frozenset(get_args(ModelGenMethod))
_VALID_VFX_TYPE:      frozenset[str] = frozenset(get_args(VfxType))
_VALID_SHADER_LANG:   frozenset[str] = frozenset(get_args(ShaderLanguage))
_VALID_BT_NODE:       frozenset[str] = frozenset(get_args(BTNodeType))
_VALID_WIRING:        frozenset[str] = frozenset(get_args(WiringType))
_VALID_CHAR_ROLE:     frozenset[str] = frozenset(get_args(CharacterRole))
_VALID_MISSION_OBJ:   frozenset[str] = frozenset(get_args(MissionObjType))
_VALID_WEATHER:       frozenset[str] = frozenset(get_args(WeatherType))
_VALID_WORLD_SCALE:   frozenset[str] = frozenset(get_args(WorldScale))
_VALID_AI_BEH:        frozenset[str] = frozenset(get_args(AIBehaviorKind))

# ── Hex color helpers ──────────────────────────────────────────────────────────

_HEX_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


def _norm_hex(v: str, default: str = "#FFFFFF") -> str:
    """Normalise a CSS hex color to #RRGGBB uppercase, or return default."""
    s = str(v).strip()
    if not s.startswith('#'):
        s = '#' + s
    s = s.upper()
    return s if _HEX_RE.match(s) else default


def _role_key(name: str) -> str:
    """Single-token comparable form of a character/entity name.

    Schema entity names are single-token by parser requirement
    ("GrandmaEdna"), while CharacterSpec names may carry the display form
    ("Grandma Edna") -- both collapse to "grandmaedna" so the role-graph
    cross-check (UnitySchema.enforce_role_graph) can match them.
    """
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


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


# ── Existing schema primitives (preserved for backward compatibility) ──────────

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


def _clamp_points(v, cap: int, allow_bank: bool = False) -> list:
    """Shared coercion for point-list fields: ≤ `cap` [x, y, z] float triples,
    dropping anything malformed. With `allow_bank`, an optional 4th component
    is kept as a bank angle (degrees of roll about the direction of travel)
    clamped to ±45 — a zero bank is dropped back to a plain triple so
    unbanked schemas stay byte-identical downstream."""
    if not isinstance(v, list):
        return []
    width = 4 if allow_bank else 3
    out: list[list[float]] = []
    for p in v[:cap]:
        try:
            nums = [float(x) for x in list(p)[:width]]
        except (TypeError, ValueError):
            continue
        while len(nums) < 3:
            nums.append(0.0)
        point = [max(-2000.0, min(2000.0, n)) for n in nums[:3]]
        if allow_bank and len(nums) > 3:
            bank = max(-45.0, min(45.0, nums[3]))
            if bank != 0.0:
                point.append(bank)
        out.append(point)
    return out


# Kinds that can never BE the player, so may never hold the "Player" tag.
# Deliberately a denylist of scenery/hostile kinds rather than an allowlist:
# the player is not always kind="player" — a racing scaffold's PlayerKart is
# kind="vehicle" and must keep the tag (see src/unity_racetrack.py).
_NEVER_PLAYER_KINDS: frozenset[str] = frozenset({
    "prop", "obstacle", "pickup", "ground", "road", "rail", "tree", "building",
    "crowd", "npc", "enemy", "fx", "hazard", "item", "ramp", "platform",
    "wall", "decor", "weapon",
})


class EntitySpec(BaseModel):
    """A prompt-derived group of scene objects: N primitives placed by a rule.

    This is the Tier-2 vocabulary that lets a concept's *content* reach the
    scene — "6 rival cars", "a ring of buildings", "a crowd of spheres" — instead
    of the one hardcoded player+enemy the base scene emits. A deterministic
    bootstrap turns each EntitySpec into `count` GameObjects at the given layout.
    """
    name:       str   = "Entities"
    kind:       str   = "prop"
    count:      int   = 1
    shape:      str   = "cube"
    placement:  str   = "single"
    color_role: str   = "accent"
    spacing:    float = 2.0
    radius:     float = 8.0
    scale:      float = 1.0
    tag:        str   = "Untagged"
    behavior:   str   = ""
    # 2-5 plain words describing the physical object ("pine tree", "wooden
    # ramp", "hanging banner") — steers the per-entity asset-catalog match so
    # the group spawns a fitting real model instead of the shared default prop.
    # "default prop" is the escape hatch that keeps Models/Defaults' Prop.fbx.
    model_hint: str   = ""
    # Live Sketchfab search query (Tier S, src/unity_entity_asset_resolver.py) —
    # deliberately SEPARATE from model_hint, which drives literal keyword-overlap
    # matching against Kenney's short curated tags (src/unity_asset_catalog.py);
    # an evocative multi-word phrase reused there would dilute that scoring.
    # Normally filled by src/unity_sketchfab_translator.py (a shared project
    # style brief + this entity's noun phrase) so independently-sourced
    # Sketchfab picks read as one art direction instead of "constructed chaos".
    # Empty -> the resolver falls back to model_hint / tokenized entity name.
    sketchfab_query: str = ""
    atoms:      list[AtomInvocation] = Field(default_factory=list)
    # placement == "path": world-space [x, y, z] points (y = elevation!). The
    # spawner lays the instances along this polyline AND emits a "Waypoints"
    # container of empty GameObjects at the points — winding roads, patrol
    # routes, and racing lines that waypoint-following AI can consume.
    # An optional 4th component per point is a BANK angle: degrees of roll
    # about the direction of travel (clamped ±45; negative leans the surface
    # left, positive right), linearly interpolated between points exactly
    # like elevation — consumed by tiled groups (path_width > 0) to build
    # banked corners and off-camber sections.
    path_points: list[list[float]] = Field(default_factory=list)
    # placement == "path" only: cross-track width in meters. 0.0 (default) =
    # legacy behavior — spread `count` instances evenly along the polyline.
    # > 0.0 = TILE mode: the spawner width-normalizes the resolved model to
    # this many meters across, derives the instance count from the path's arc
    # length / segment length (authored `count` is only a fallback when the
    # model can't be measured), and lays segments end-to-end facing the path
    # tangent — continuous road ribbons and guardrails instead of scattered
    # pieces.
    path_width: float = 0.0
    # World-space Y this group's instances sit at — the altitude/depth axis a
    # ground-plane layout has no way to express. 0.0 (default) = let the
    # spatial primitive decide (src/unity_spatial.py): on a surface game that
    # means the floor, and in a volume game (fly/swim locomotion) it means the
    # altitude tier of this group's kind. A non-zero value is an explicit
    # authored altitude and always wins over the derived tier.
    elevation:  float = 0.0
    # World-space [x, z] center this group is packed into by src/unity_layout.py
    # (Step 5 of the pipeline) — every group used to spawn centered on the
    # scene origin regardless of `radius`, so distinct groups (e.g. a ring of
    # buildings and a ring of vehicles, both radius=8) landed on the exact
    # same circle and stacked. [0, 0] until the layout pass runs.
    origin_offset: list[float] = Field(default_factory=lambda: [0.0, 0.0])
    # Bounding radius (world units) of this group's footprint AFTER layout —
    # used both for non-overlapping group packing and for sizing the scene's
    # ground plane to the content instead of a fixed constant. 0.0 = not yet
    # computed (placement == "path", or the layout pass hasn't run).
    group_radius: float = 0.0
    # Baked-deterministic per-instance [x, y, z] world positions computed by
    # the layout pass for placement in {single, line, ring, grid, scatter} —
    # supersedes EntitySpawner's runtime PlaceAt() formulas (which only knew
    # about ONE group at a time and, for "scatter", ignored `spacing`/scale
    # entirely) with positions that already account for group_radius packing,
    # scale-derived footprint, and guaranteed minimum inter-instance spacing.
    # Empty -> spawner falls back to its legacy runtime formulas (2D archetypes
    # never populate this).
    instance_positions: list[list[float]] = Field(default_factory=list)
    # Baked per-instance facing (yaw, degrees about world Y) parallel to
    # `instance_positions` — for groups whose orientation matters, e.g. a
    # starting grid aligned to the race direction (src/unity_racetrack.py).
    # Authored by deterministic scaffolds, never by the LLM. Empty -> the
    # spawner leaves each instance at the model's default rotation.
    instance_yaws: list[float] = Field(default_factory=list)

    @field_validator("path_points", mode="before")
    @classmethod
    def _clamp_path(cls, v) -> list:
        """Coerce to ≤ 64 [x, y, z] (or [x, y, z, bank°]) points; the bank
        angle is clamped to ±45 and dropped when 0. Drop anything malformed."""
        return _clamp_points(v, 64, allow_bank=True)

    @field_validator("instance_positions", mode="before")
    @classmethod
    def _clamp_instance_positions(cls, v) -> list:
        """Coerce to ≤ 200 [x, y, z] float triples (matches `count`'s cap)."""
        return _clamp_points(v, 200)

    @field_validator("instance_yaws", mode="before")
    @classmethod
    def _clamp_instance_yaws(cls, v) -> list:
        """Coerce to ≤ 200 floats wrapped into [-360, 360]; drop malformed."""
        try:
            return [max(-360.0, min(360.0, float(x))) for x in list(v)[:200]]
        except (TypeError, ValueError):
            return []

    @field_validator("origin_offset", mode="before")
    @classmethod
    def _clamp_offset(cls, v) -> list:
        """Coerce to exactly [x, z] floats, clamped to a sane world range."""
        try:
            nums = [float(x) for x in list(v)[:2]]
        except (TypeError, ValueError):
            return [0.0, 0.0]
        while len(nums) < 2:
            nums.append(0.0)
        return [max(-2000.0, min(2000.0, n)) for n in nums]

    @field_validator("elevation", mode="before")
    @classmethod
    def _clamp_elevation(cls, v) -> float:
        """Clamped to the same world range origin_offset uses, so an authored
        altitude can't push a group outside anything that has to contain it."""
        try:
            return max(-2000.0, min(2000.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("group_radius", mode="before")
    @classmethod
    def _clamp_group_radius(cls, v) -> float:
        try:
            return max(0.0, min(2000.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("path_width", mode="before")
    @classmethod
    def _clamp_path_width(cls, v) -> float:
        try:
            return max(0.0, min(100.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("model_hint", mode="before")
    @classmethod
    def _clamp_model_hint(cls, v) -> str:
        """Lowercase, ≤ 6 plain words — a search phrase, not a sentence."""
        words = re.sub(r"[^a-z0-9\s-]", " ", str(v or "").lower()).split()
        return " ".join(words[:6])

    @model_validator(mode="after")
    def _reserve_player_tag(self) -> "EntitySpec":
        """"Player" is a SINGLETON tag — only a kind="player" entity may hold it.

        Every player-facing lookup in the generated game is
        `GameObject.FindWithTag("Player")`: the camera rig mounts to it, the
        weapon mounts to its eye, and enemy AI chases it. Unity returns an
        arbitrary match when several objects share a tag, so one mis-tagged prop
        silently captures all of them. Hit live on AstralArena, where the LLM
        tagged a "WeaponModels" prop group "Player": the camera locked onto the
        prop (no first-person view, no follow), the gun never mounted because
        the prop has no eye transform, and the real player still turned under
        mouse look — so the swarm chasing it appeared to swing around with the
        mouse. Every one of those symptoms was this single tag.
        """
        if self.tag == "Player" and (self.kind or "").lower() in _NEVER_PLAYER_KINDS:
            self.tag = "Untagged"
        return self

    @model_validator(mode="after")
    def _no_enemy_firing_squad(self) -> "EntitySpec":
        """A group of enemies is a crowd, not a parade.

        "line" places instances evenly along a single axis, which is right for
        fence posts and start grids but makes a combat encounter read as a row
        of models standing shoulder to shoulder facing the player — the exact
        "still clumped up" look, because a line concentrates every instance in
        one band. Enemies get scattered instead. Groups that carry authored
        positions (a racetrack start grid) never reach the layout pass at all,
        so this cannot disturb them.
        """
        if (self.kind or "").lower() == "enemy" and self.placement == "line" \
                and self.count > 2 and not self.instance_positions:
            self.placement = "scatter"
        return self

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        try:
            return max(0, min(200, int(v)))
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
    time_of_day:   str            = "day"
    fog:           Optional[bool]  = None
    fog_color:     Optional[str]   = None
    sun_intensity: Optional[float] = None

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


class SceneSpec(BaseModel):
    """Per-scene content: mood overrides + which entity groups live there.

    This is what makes "StormShelf — thunderheads, boss encounter" render
    differently from "FinalApproach — low-altitude dawn descent" instead of
    every gameplay scene being a byte-identical template. `name` must match an
    entry in schema.scenes; unset fields inherit the project environment.
    """
    name:        str = ""
    description: str = ""                       # one line; also drives mood inference
    time_of_day: str = ""                       # "" = inherit project environment
    fog:         Optional[bool] = None
    fog_color:   Optional[str]  = None
    # None = all entity groups spawn here (default); [] = an empty scene;
    # otherwise the entity NAMES active in this scene.
    entities:    Optional[list[str]] = None

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "", str(v or ""))

    @field_validator("description", mode="before")
    @classmethod
    def _clamp_desc(cls, v: str) -> str:
        return str(v or "").strip()[:200]

    @field_validator("time_of_day", mode="before")
    @classmethod
    def _clamp_tod(cls, v: str) -> str:
        s = str(v or "").lower().strip()
        if not s:
            return ""                            # inherit
        synonyms = {"morning": "dawn", "sunrise": "dawn", "noon": "day",
                    "midday": "day", "dusk": "sunset", "evening": "sunset",
                    "midnight": "night", "dark": "night"}
        s = synonyms.get(s, s)
        return s if s in ("dawn", "day", "sunset", "night") else ""

    @field_validator("fog_color", mode="before")
    @classmethod
    def _norm_fog_color(cls, v):
        if v is None or str(v).strip() == "":
            return None
        return _norm_hex(str(v))

    @field_validator("entities", mode="before")
    @classmethod
    def _clean_entities(cls, v):
        if v is None:
            return None
        if not isinstance(v, list):
            return None
        return [str(x).strip() for x in v[:40] if str(x).strip()]


class ColorPalette(BaseModel):
    """Six named hex colors that drive material generation and scene lighting."""
    name:       str = "Stylized"
    primary:    str = "#9B59B6"
    secondary:  str = "#1ABC9C"
    accent:     str = "#E67E22"
    background: str = "#2C3E50"
    trim:       str = "#F0F0F0"

    @field_validator("primary", "secondary", "accent", "background", "trim", mode="before")
    @classmethod
    def normalise_hex(cls, v: str) -> str:
        return _norm_hex(v)


# ── World Spec ────────────────────────────────────────────────────────────────

class WorldRegion(BaseModel):
    name:     str         = "Region"
    biome:    str         = ""
    entities: list[str]   = Field(default_factory=list)
    bounds:   list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])  # xmin, zmin, xmax, zmax

    @field_validator("bounds", mode="before")
    @classmethod
    def _clamp_bounds(cls, v) -> list:
        if not isinstance(v, list):
            return [0.0, 0.0, 0.0, 0.0]
        result = []
        for x in (list(v) + [0.0, 0.0, 0.0, 0.0])[:4]:
            try:
                result.append(max(-10000.0, min(10000.0, float(x))))
            except (TypeError, ValueError):
                result.append(0.0)
        return result


class ProceduralRules(BaseModel):
    blender: dict[str, Any] = Field(default_factory=dict)
    unity:   dict[str, Any] = Field(default_factory=dict)
    navmesh: dict[str, Any] = Field(default_factory=dict)


class WorldSpec(BaseModel):
    biome:            str                    = "urban"
    time_of_day:      str                    = "day"
    weather:          str                    = "clear"
    scale:            str                    = "medium"
    regions:          list[WorldRegion]      = Field(default_factory=list)
    procedural_rules: Optional[ProceduralRules] = None

    @field_validator("time_of_day", mode="before")
    @classmethod
    def _norm_tod(cls, v: str) -> str:
        s = str(v).lower().strip()
        synonyms = {"morning": "dawn", "sunrise": "dawn", "noon": "day",
                    "midday": "day", "dusk": "sunset", "evening": "sunset",
                    "midnight": "night", "dark": "night"}
        s = synonyms.get(s, s)
        return s if s in ("dawn", "day", "sunset", "night") else "day"

    @field_validator("weather", mode="before")
    @classmethod
    def _norm_weather(cls, v: str) -> str:
        s = str(v).lower().strip()
        synonyms = {"sunny": "clear", "cloudy": "overcast", "windy": "wind",
                    "rainy": "rain", "stormy": "storm", "snowy": "snow", "foggy": "fog"}
        s = synonyms.get(s, s)
        return s if s in _VALID_WEATHER else "clear"

    @field_validator("scale", mode="before")
    @classmethod
    def _norm_scale(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_WORLD_SCALE else "medium"


# ── Dialog Graph ──────────────────────────────────────────────────────────────

class DialogChoice(BaseModel):
    text:      str
    next_node: str
    condition: str = ""  # optional guard — e.g. "player.has_item(KeyCard)"


class DialogNode(BaseModel):
    id:        str
    speaker:   str
    text:      str
    emotion:   str = "neutral"
    audio_ref: str = ""  # AudioSpec id — empty = no voiced line
    choices:   list[DialogChoice] = Field(default_factory=list)


class DialogGraph(BaseModel):
    start_node: str              = ""
    nodes:      list[DialogNode] = Field(default_factory=list)


# ── Behavior Tree ─────────────────────────────────────────────────────────────

class BTNode(BaseModel):
    id:       str
    type:     str               = "action"
    children: list[str]         = Field(default_factory=list)  # child BTNode ids
    action:   str               = ""
    params:   dict[str, Any]    = Field(default_factory=dict)

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_BT_NODE else "action"


class BehaviorTree(BaseModel):
    root:  str           = ""
    nodes: list[BTNode]  = Field(default_factory=list)


# ── NPC Brain ─────────────────────────────────────────────────────────────────

class NPCBrain(BaseModel):
    traits:        list[str]              = Field(default_factory=list)
    goals:         list[str]              = Field(default_factory=list)
    memory:        list[str]              = Field(default_factory=list)
    behavior_tree: Optional[BehaviorTree] = None


# ── Character Spec ────────────────────────────────────────────────────────────

class CharacterSpec(BaseModel):
    id:            str
    name:          str
    role:          str              = "npc"
    model_ref:     str              = ""  # ModelSpec id or file path
    animation_set: list[str]        = Field(default_factory=list)
    dialog_graph:  Optional[DialogGraph] = None
    npc_brain:     Optional[NPCBrain]    = None

    @field_validator("role", mode="before")
    @classmethod
    def _norm_role(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_CHAR_ROLE else "npc"


# ── Audio Spec (extended — keeps existing AudioTrack for backward compat) ─────

class AudioSpec(BaseModel):
    id:                str
    type:              str              = "sfx"
    description:       str              = ""
    generation_method: str              = "sfxr"
    parameters:        dict[str, Any]   = Field(default_factory=dict)
    file_ref:          str              = ""
    loop:              bool             = False

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_CATEGORIES else "sfx"

    @field_validator("generation_method", mode="before")
    @classmethod
    def _norm_gen(cls, v: str) -> str:
        s = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        return s if s in _VALID_AUDIO_GEN else "sfxr"


# ── PBR Maps + Material Spec ──────────────────────────────────────────────────

class PBRMaps(BaseModel):
    albedo:    str = ""
    normal:    str = ""
    roughness: str = ""
    metallic:  str = ""
    ao:        str = ""


class MaterialSpec(BaseModel):
    id:                str
    name:              str
    shader_model:      str            = "Standard"
    pbr_maps:          Optional[PBRMaps] = None
    generation_method: str            = "procedural"
    parameters:        dict[str, Any] = Field(default_factory=dict)

    @field_validator("generation_method", mode="before")
    @classmethod
    def _norm_gen(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_MAT_GEN else "procedural"


# ── Model Spec ────────────────────────────────────────────────────────────────

class ModelSpec(BaseModel):
    id:                str
    name:              str
    category:          str            = "prop"
    generation_method: str            = "blender_microgen"
    parameters:        dict[str, Any] = Field(default_factory=dict)
    file_ref:          str            = ""
    materials:         list[str]      = Field(default_factory=list)  # MaterialSpec ids

    @field_validator("category", mode="before")
    @classmethod
    def _norm_cat(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_MODEL_CAT else "prop"

    @field_validator("generation_method", mode="before")
    @classmethod
    def _norm_gen(cls, v: str) -> str:
        s = str(v).lower().strip().replace("-", "_").replace(" ", "_")
        return s if s in _VALID_MODEL_GEN else "blender_microgen"


# ── Animation Spec ────────────────────────────────────────────────────────────

class AnimationSpec(BaseModel):
    id:     str
    name:   str
    clip:   str       = ""
    loop:   bool      = True
    events: list[str] = Field(default_factory=list)  # Unity Animation Event names


# ── VFX Spec ─────────────────────────────────────────────────────────────────

class VfxSpec(BaseModel):
    id:         str
    name:       str
    type:       str            = "particle"
    parameters: dict[str, Any] = Field(default_factory=dict)
    file_ref:   str            = ""

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, v: str) -> str:
        s = str(v).lower().strip().replace(" ", "_")
        return s if s in _VALID_VFX_TYPE else "particle"


# ── Shader Spec ───────────────────────────────────────────────────────────────

class ShaderSpec(BaseModel):
    id:         str
    name:       str
    language:   str            = "shadergraph"
    parameters: dict[str, Any] = Field(default_factory=dict)
    file_ref:   str            = ""

    @field_validator("language", mode="before")
    @classmethod
    def _norm_lang(cls, v: str) -> str:
        s = str(v).lower().strip().replace(" ", "").replace("_", "").replace("-", "")
        if s in ("shadergraph", "graph", "sg"):
            return "shadergraph"
        if s in ("hlsl", "cg"):
            return "hlsl"
        if s in ("glsl",):
            return "glsl"
        return "shadergraph"


# ── Mission Spec ──────────────────────────────────────────────────────────────

class MissionObjective(BaseModel):
    id:          str
    description: str
    type:        str  = "reach"
    target_ref:  str  = ""
    count:       int  = 1
    optional:    bool = False

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_MISSION_OBJ else "reach"

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        try:
            return max(1, min(9999, int(v)))
        except (TypeError, ValueError):
            return 1


class MissionSpec(BaseModel):
    id:             str
    title:          str
    description:    str                    = ""
    objectives:     list[MissionObjective] = Field(default_factory=list)
    triggers:       list[str]              = Field(default_factory=list)
    rewards:        dict[str, Any]         = Field(default_factory=dict)
    story_beat_ref: str                    = ""  # StoryBeat.id


# ── AI Behavior Tree (standalone registry entry, referenced by CharacterSpec) ─

class AIBehaviorTree(BaseModel):
    id:   str
    name: str
    tree: Optional[BehaviorTree] = None


# ── Microgen Spec ─────────────────────────────────────────────────────────────

class MicrogensSpec(BaseModel):
    kind:         str
    input:        dict[str, Any]          = Field(default_factory=dict)
    output_files: list[str]               = Field(default_factory=list)
    atoms:        list[AtomInvocation]    = Field(default_factory=list)


# ── Wiring Map ────────────────────────────────────────────────────────────────

class WiringDependency(BaseModel):
    source: str   # originating asset/system id
    target: str   # destination asset/system id
    type:   str = "references"

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_WIRING else "references"


class WiringMap(BaseModel):
    dependencies: list[WiringDependency] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_list(cls, v):
        """Absorb the two list shapes the LLM emits for `wiring`.

        The schema-fill step reliably hands back a LIST here instead of the
        mapping — either the bare dependency list (`[{source, target, type}]`)
        or the map wrapped in a list (`[{"dependencies": [...]}]`). Both carry
        the right data in the wrong container, so flatten them rather than
        hard-failing an otherwise valid schema.
        """
        if isinstance(v, list):
            deps: list = []
            for item in v:
                if isinstance(item, dict) and "dependencies" in item:
                    inner = item.get("dependencies") or []
                    if isinstance(inner, list):
                        deps.extend(inner)
                elif isinstance(item, dict):
                    deps.append(item)
            v = {"dependencies": deps}

        # Drop half-written edges. A dependency is a (source -> target) pair and
        # the LLM sometimes emits just one end ({"source": "ai"}); a single such
        # entry failed validation for the ENTIRE schema, throwing away a
        # 25-minute build over one unusable edge in an advisory wiring hint.
        if isinstance(v, dict) and isinstance(v.get("dependencies"), list):
            v = dict(v)
            v["dependencies"] = [
                d for d in v["dependencies"]
                if not isinstance(d, dict)
                or (str(d.get("source", "")).strip() and str(d.get("target", "")).strip())
            ]
        return v


# ── Capability models (behavior-first schema layer) ───────────────────────────
# The LLM fills these; the capability resolver (Step 5) converts them to atoms.
# Each field is a typed parameter — "jump: true" + "jump_force" both flow
# downstream to the Unity C# snippet that implements the Jump atom.

class InputBinding(BaseModel):
    action: str
    key:    str
    event:  str = ""  # SendMessage target; if empty → "On{Action}" at resolve time


class ControllerCapability(BaseModel):
    movement:          bool  = True
    jump:              bool  = True
    sprint:            bool  = False
    crouch:            bool  = False
    look_sensitivity:  float = 2.0
    speed:             float = 6.0
    sprint_multiplier: float = 1.7
    jump_force:        float = 8.0

    @field_validator("look_sensitivity", "speed", "sprint_multiplier", "jump_force", mode="before")
    @classmethod
    def _clamp_positive(cls, v) -> float:
        try:
            return max(0.01, float(v))
        except (TypeError, ValueError):
            return 1.0


class PhysicsCapability(BaseModel):
    gravity:   bool  = True
    collision: str   = "dynamic"   # none | static | kinematic | dynamic
    mass:      float = 80.0
    drag:      float = 0.0

    @field_validator("collision", mode="before")
    @classmethod
    def _norm_collision(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in ("none", "static", "kinematic", "dynamic") else "dynamic"

    @field_validator("mass", mode="before")
    @classmethod
    def _clamp_mass(cls, v) -> float:
        try:
            return max(0.001, min(100_000.0, float(v)))
        except (TypeError, ValueError):
            return 80.0

    @field_validator("drag", mode="before")
    @classmethod
    def _clamp_drag(cls, v) -> float:
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            return 0.0


class HudCapability(BaseModel):
    show_health:    bool = True
    show_ammo:      bool = False
    show_objective: bool = True
    show_minimap:   bool = False


class ModelCapability(BaseModel):
    lod:        str             = "auto"
    animations: dict[str, str] = Field(default_factory=dict)

    @field_validator("lod", mode="before")
    @classmethod
    def _norm_lod(cls, v) -> str:
        s = str(v).lower().strip()
        return s if s in ("auto", "0", "1", "2", "disabled") else "auto"


class InputCapability(BaseModel):
    bindings: list[InputBinding] = Field(default_factory=list)


class AnimationCapability(BaseModel):
    rig_type: str             = "generic"   # generic | humanoid
    clips:    dict[str, str]  = Field(default_factory=dict)  # state → clip path

    @field_validator("rig_type", mode="before")
    @classmethod
    def _norm_rig(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in ("generic", "humanoid") else "generic"


class DialogCapability(BaseModel):
    system:        str  = "simple"   # simple | branching | voiced
    bark_on_sight: bool = False

    @field_validator("system", mode="before")
    @classmethod
    def _norm_system(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in ("simple", "branching", "voiced") else "simple"


class AICapability(BaseModel):
    pathfinding:     bool  = True
    detection_range: float = 15.0
    behavior:        str   = "patrol"

    @field_validator("behavior", mode="before")
    @classmethod
    def _norm_behavior(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in _VALID_AI_BEH else "patrol"

    @field_validator("detection_range", mode="before")
    @classmethod
    def _clamp_range(cls, v) -> float:
        try:
            return max(0.5, min(200.0, float(v)))
        except (TypeError, ValueError):
            return 15.0


class VfxCapability(BaseModel):
    particles:       bool = True
    post_processing: bool = False


class ProjectCapabilities(BaseModel):
    """
    Capability graph — the LLM's vocabulary for activating microgens.
    Each present sub-model activates the matching microgen and seeds its
    atom sequence with typed parameters instead of hardcoded defaults.
    """
    controller: Optional[ControllerCapability] = None
    physics:    Optional[PhysicsCapability]    = None
    hud:        Optional[HudCapability]        = None
    input:      Optional[InputCapability]      = None
    model:      Optional[ModelCapability]      = None
    animation:  Optional[AnimationCapability]  = None
    dialog:     Optional[DialogCapability]     = None
    ai:         Optional[AICapability]         = None
    vfx:        Optional[VfxCapability]        = None


# ── Package + Script models ───────────────────────────────────────────────────

class UnityPackage(BaseModel):
    description: str
    package_id:  Optional[str] = None
    version:     Optional[str] = None


class UnityScript(BaseModel):
    name:        str
    type:        ScriptType
    description: str

    attach_to:          str              = "systems"
    extra_usings:       list[str]        = Field(default_factory=list)
    extra_fields:       list[CodeField]  = Field(default_factory=list)
    awake_extra:        str              = ""
    update_extra:       str              = ""
    fixed_update_extra: str              = ""
    extra_methods:      list[CodeMethod] = Field(default_factory=list)
    atoms:              list[AtomInvocation] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def normalise_type(cls, v: str) -> str:
        # Unknown types become "custom" so LLM-authored fragments carry the behavior
        t = str(v).lower().strip()
        return t if t in _VALID_SCRIPT_TYPES else "custom"

    @field_validator("attach_to", mode="before")
    @classmethod
    def normalise_attach(cls, v: str) -> str:
        s = str(v).lower().strip()
        return s if s in frozenset(get_args(ScriptAttach)) else "systems"


# ── Root schema ───────────────────────────────────────────────────────────────

class UnitySchema(BaseModel):
    # ── Core project identity ─────────────────────────────────────────────────
    project_name:    str
    company_name:    str           = "MyCompany"
    description:     str           = ""
    game_type:       GameType
    platform:        Platform
    locomotion:      Literal["walk", "drive", "fly", "swim"] = "walk"
    render_pipeline: RenderPipeline
    unity_version:   UnityVersion  = "6000.0"
    color_space:     Literal["Linear", "Gamma"] = "Linear"
    visual_style:    str           = "stylized"

    # ── Legacy/existing collections (kept for backward compat) ────────────────
    scenes:            list[str]             = Field(default_factory=lambda: ["Main", "MainMenu"])
    scene_specs:       list[SceneSpec]       = Field(default_factory=list, max_length=8)
    features:          list[str]             = Field(default_factory=list)
    packages:          list[UnityPackage]    = Field(default_factory=list, max_length=20)
    scripts:           list[UnityScript]     = Field(default_factory=list, max_length=24)
    story_beats:       list[StoryBeat]       = Field(default_factory=list)
    audio_tracks:      list[AudioTrack]      = Field(default_factory=list)
    procedural_meshes: list[ProceduralMesh]  = Field(default_factory=list)
    entities:          list[EntitySpec]      = Field(default_factory=list, max_length=40)
    atoms:             list[AtomInvocation]  = Field(default_factory=list)
    capabilities:      Optional[ProjectCapabilities] = None
    environment:       Optional[EnvironmentSpec]     = None
    color_palette:     Optional[ColorPalette]        = None
    meta:              dict                          = Field(default_factory=dict)

    # ── Multimodal IR — the full design space ─────────────────────────────────
    world_spec:     Optional[WorldSpec]   = None
    characters:     list[CharacterSpec]   = Field(default_factory=list)
    missions:       list[MissionSpec]     = Field(default_factory=list)
    audio_specs:    list[AudioSpec]       = Field(default_factory=list)
    materials:      list[MaterialSpec]    = Field(default_factory=list)
    models:         list[ModelSpec]       = Field(default_factory=list)
    animations:     list[AnimationSpec]   = Field(default_factory=list)
    vfx:            list[VfxSpec]         = Field(default_factory=list)
    shaders:        list[ShaderSpec]      = Field(default_factory=list)
    behavior_trees: list[AIBehaviorTree]  = Field(default_factory=list)
    microgens:      list[MicrogensSpec]   = Field(default_factory=list)
    wiring:         Optional[WiringMap]   = None

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("game_type", mode="before")
    @classmethod
    def normalise_game_type(cls, v: str) -> str:
        t = str(v).strip()
        upper = t.upper()
        if upper in {"2D", "2.5D", "3D", "VR"}:
            return upper
        lower = t.lower()
        if lower in {"2d_topdown", "2d topdown", "2d-topdown", "flat top_down", "flat topdown"}:
            return "2d_topdown"
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
        m = re.search(r"6000\.(\d+)", s) or re.fullmatch(r"6(?:\.(\d+))?", s)
        if m:
            minor = int(m.group(1) or 0)
            # Editors newer than the pinned minors (6.2, 6.3, 6.5, …) take the
            # newest pin set — the 6000.0 pins (e.g. Cinemachine) are too old
            # for them and fail compilation from the PackageCache.
            if minor >= 2:
                return "6000.4"
            return "6000.1" if minor == 1 else "6000.0"
        if "2022" in s:
            return "2022.3"
        if "2021" in s:
            return "2021.3"
        return s if s in _VALID_VERSIONS else "6000.0"

    @field_validator("features", mode="before")
    @classmethod
    def normalise_features(cls, v: list) -> list:
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
        if self.render_pipeline in ("URP", "HDRP"):
            self.color_space = "Linear"
        return self

    @model_validator(mode="after")
    def fill_default_palette(self) -> "UnitySchema":
        if self.color_palette is None:
            pal_data = _DEFAULT_PALETTES.get(self.visual_style,
                                             _DEFAULT_PALETTES["stylized"])
            self.color_palette = ColorPalette(**pal_data)
        return self

    # -- Role-graph invariants (cross-field; runs after per-entity guards) -----
    # A role inversion -- the boss filled in as role="player" and the real
    # player demoted -- is schema-valid in BOTH orientations, so field-level
    # coercion can never repair it; only a cross-field check can. Two ground
    # truths make the repair deterministic and LLM-free: entity tags are
    # assigned by the pre-flight tagger from the brief's ENTITIES list, and
    # EntitySpec._reserve_player_tag has already stripped "Player" from
    # never-player kinds before this root validator runs. Repairs are applied
    # in place and logged to meta["role_lint"]["repairs"]; anything that
    # cannot be derived without guessing is recorded under
    # meta["role_lint"]["violations"] for the Step-7 playability gate to
    # score -- never a hard failure, because one bad role must not throw away
    # an otherwise valid schema (same lesson as WiringMap._accept_list).

    @model_validator(mode="after")
    def enforce_role_graph(self) -> "UnitySchema":
        repairs:    list[str] = []
        violations: list[str] = []

        # R1 -- "Player" is a schema-wide SINGLETON tag. The per-entity guard
        # (_reserve_player_tag) sees one entity at a time, so duplicates across
        # groups survive it. Unity's FindWithTag returns an arbitrary match,
        # so a duplicate breaks camera/weapon/AI wiring nondeterministically
        # (the AstralArena failure). Keep the strongest holder -- kind="player"
        # first, else declaration order -- and untag the rest.
        holders = [e for e in self.entities if e.tag == "Player"]
        if len(holders) > 1:
            keep = next((e for e in holders
                         if (e.kind or "").lower() == "player"), holders[0])
            for e in holders:
                if e is not keep:
                    e.tag = "Untagged"
                    repairs.append(
                        f"R1: stripped duplicate Player tag from entity "
                        f"{e.name!r} (kept {keep.name!r})")

        # R2 -- characters[] must agree with the deterministic entity tags.
        # The character matching the Player-tagged entity IS the player; a
        # character whose entity is hostile-tagged can never be role="player".
        # This is the inversion catcher, and the repair source is the entity
        # layer, not the LLM.
        if self.characters:
            tag_by_key  = {_role_key(e.name): e.tag for e in self.entities}
            kind_by_key = {_role_key(e.name): (e.kind or "").lower()
                           for e in self.entities}
            for c in self.characters:
                key  = _role_key(c.name) or _role_key(c.id)
                tag  = tag_by_key.get(key)
                kind = kind_by_key.get(key, "")
                if tag == "Player" and c.role != "player":
                    repairs.append(
                        f"R2: character {c.name!r} matches the Player-tagged "
                        f"entity but had role={c.role!r} -> 'player'")
                    c.role = "player"
                elif c.role == "player" and (tag == "Enemy"
                                             or kind in ("enemy", "boss")):
                    demoted = "boss" if kind == "boss" else "enemy"
                    repairs.append(
                        f"R2: character {c.name!r} had role='player' but its "
                        f"entity is hostile-tagged -> {demoted!r}")
                    c.role = demoted

            # R3 -- exactly one player once any characters exist. After R2 the
            # remaining failure modes have no derivable repair (zero players
            # with no Player-tagged entity to promote from; two players both
            # matching neutral entities), so record for the playability gate
            # instead of guessing.
            players = [c.name for c in self.characters if c.role == "player"]
            if len(players) != 1:
                violations.append(
                    f"R3: role graph needs exactly 1 player character, "
                    f"found {len(players)}: {players or 'none'}")

        if repairs or violations:
            lint = dict(self.meta.get("role_lint") or {})
            lint["repairs"]    = list(lint.get("repairs")    or []) + repairs
            lint["violations"] = list(lint.get("violations") or []) + violations
            self.meta["role_lint"] = lint
        return self

    def unresolved_packages(self) -> list[str]:
        return [p.description for p in self.packages if not p.package_id]
