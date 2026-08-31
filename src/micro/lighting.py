"""Lighting micro-generator — scene light rigs + the environment-lighting guard.

This micro is ALWAYS composed (see subsystems_for's core list in unity_compose.py).
It owns two things no other micro owns:

  1. The light RIG — key/fill/rim/practical/flood lights beyond the single
     hard-coded Directional Light in templates/unity/scene.unity.j2.
  2. The environment GUARD — RenderSettings ambient mode/colors, the skybox,
     and the reflection source. This is what actually stops "every material is
     black on Play": the scene YAML ships m_AmbientMode: 0 (Skybox) pointing at
     the built-in Default-Skybox, and the only existing repair for that lives
     behind three editor-only gates in SetupRenderPipeline.cs.j2 (EditorPrefs
     one-shot + `defaultRenderPipeline == null` + `not has_atmosphere`), so on
     any project where URP was already assigned it never runs at all.

Runtime-built materials (EntitySpawner, MaterialPalette) have no GI
contribution whatsoever — they render from direct light + ambient only — which
is why the guard has to execute at Play time, not just at import time.

Backed by FX atoms: LightFlash.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeField, CodeMethod, lint_code_fragment, lint_usings, lint_fields


LightingVariant = Literal[
    "three_point", "outdoor_sun", "interior_practical", "flat_2d", "stylized_flood",
]
VARIANTS = list(LightingVariant.__args__)  # type: ignore[attr-defined]

_LIGHT_ROLES = ("key", "fill", "rim", "practical", "flood", "area")
_LIGHT_TYPES = ("directional", "point", "spot", "area")
_SHADOWS     = ("none", "hard", "soft")
_AMBIENT_MODES   = ("skybox", "trilight", "flat")
_REFLECTION_MODES = ("skybox", "custom", "off")

# The "never black" invariant. Relative luminance (Rec.709) below this and a
# scene with no direct light renders as an unreadable silhouette. Enforced by
# LightingPipeline.default_fix — this is a lighting *floor*, not a look: the
# fixer scales toward it while preserving the palette's hue, it never
# substitutes a generic grey.
AMBIENT_LUMINANCE_FLOOR = 0.12

# Minimum summed directional intensity a lit (non-flat_2d) rig must reach.
KEY_INTENSITY_FLOOR = 0.35

# Minimum luminance for a palette-derived light TINT. Below this the light is
# present but contributes nothing visible, which is indistinguishable from the
# bug this micro exists to close.
LIGHT_TINT_FLOOR = 0.25


def _luminance(r: float, g: float, b: float) -> float:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _lift_to_luminance(r: float, g: float, b: float, target: float):
    """Brighten an RGB triple to `target` luminance, keeping its hue.

    A plain scale is not enough: a dark, saturated color (0.02, 0.0, 0.04) clips
    at 1.0 per channel long before it reaches the target, so scaling alone
    silently lands under the floor. Whatever energy the clip lost is recovered by
    blending toward white, which always reaches the target because white's
    luminance is 1.0. Returns None for pure black — there is no hue to preserve.
    """
    lum = _luminance(r, g, b)
    if lum <= 0.0:
        return None
    if lum >= target:
        return round(r, 4), round(g, 4), round(b, 4)

    scale = target / lum
    r, g, b = min(1.0, r * scale), min(1.0, g * scale), min(1.0, b * scale)

    lum = _luminance(r, g, b)
    if lum < target:
        # lerp(c, white, t) has luminance lum*(1-t) + t; solve for the t that hits target.
        t = min(1.0, max(0.0, (target - lum) / max(1e-6, 1.0 - lum)))
        r, g, b = r + (1.0 - r) * t, g + (1.0 - g) * t, b + (1.0 - b) * t
    return round(r, 4), round(g, 4), round(b, 4)


def _set_luminance(r: float, g: float, b: float, target: float):
    """Force an RGB triple to exactly `target` luminance, keeping its hue.

    Unlike _lift_to_luminance this also darkens. Used to re-seat the ambient
    equator/ground bands after the sky has been lifted: they are seeded from a
    different palette slot, so scaling them by the SKY's ratio can overshoot and
    invert the gradient (a bright `secondary` ground ending up above the sky).
    """
    lum = _luminance(r, g, b)
    if lum <= 0.0:
        return None
    if lum > target:                      # scaling down never clips
        scale = target / lum
        return round(r * scale, 4), round(g * scale, 4), round(b * scale, 4)
    return _lift_to_luminance(r, g, b, target)


class LightSpec(BaseModel):
    """One light in the rig. Position/rotation are local to the rig root."""
    role:       str   = "key"
    type:       str   = "directional"
    x:          float = 0.0
    y:          float = 3.0
    z:          float = 0.0
    pitch:      float = 50.0
    yaw:        float = -30.0
    color_r:    float = 1.0
    color_g:    float = 1.0
    color_b:    float = 1.0
    intensity:  float = 1.0
    range:      float = 15.0
    spot_angle: float = 45.0
    shadows:    str   = "soft"

    @field_validator("role", mode="before")
    @classmethod
    def _clamp_role(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _LIGHT_ROLES else "fill"

    @field_validator("type", mode="before")
    @classmethod
    def _clamp_type(cls, v) -> str:
        s = str(v).strip().lower()
        aliases = {"sun": "directional", "dir": "directional", "omni": "point",
                   "flood": "spot", "rect": "area", "reflection": "point"}
        s = aliases.get(s, s)
        return s if s in _LIGHT_TYPES else "point"

    @field_validator("shadows", mode="before")
    @classmethod
    def _clamp_shadows(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _SHADOWS else "soft"

    @field_validator("color_r", "color_g", "color_b", mode="before")
    @classmethod
    def _clamp_channel(cls, v) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 1.0

    @field_validator("intensity", mode="before")
    @classmethod
    def _clamp_intensity(cls, v) -> float:
        try:
            return max(0.0, min(50.0, float(v)))
        except (TypeError, ValueError):
            return 1.0

    @field_validator("range", mode="before")
    @classmethod
    def _clamp_range(cls, v) -> float:
        try:
            return max(0.1, min(2000.0, float(v)))
        except (TypeError, ValueError):
            return 15.0

    @field_validator("spot_angle", mode="before")
    @classmethod
    def _clamp_spot(cls, v) -> float:
        try:
            return max(1.0, min(179.0, float(v)))
        except (TypeError, ValueError):
            return 45.0


class LightingSchema(BaseModel):
    name:        str = "LightingDirector"
    variant:     str = "three_point"
    description: str = "Scene light rig and environment-lighting guard"

    lights: list[LightSpec] = Field(default_factory=list)

    # Environment lighting (RenderSettings)
    ambient_mode:        str   = "trilight"
    ambient_sky_r:       float = 0.42
    ambient_sky_g:       float = 0.47
    ambient_sky_b:       float = 0.56
    ambient_equator_r:   float = 0.30
    ambient_equator_g:   float = 0.33
    ambient_equator_b:   float = 0.38
    ambient_ground_r:    float = 0.16
    ambient_ground_g:    float = 0.16
    ambient_ground_b:    float = 0.18
    ambient_intensity:   float = 1.0
    reflection_mode:     str   = "skybox"
    reflection_intensity: float = 1.0

    # Guards
    repair_black_materials: bool = True
    respect_atmosphere:     bool = True
    adopt_scene_sun:        bool = True

    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_methods: list[CodeMethod] = Field(default_factory=list)
    meta:          dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "three_point"

    @field_validator("ambient_mode", mode="before")
    @classmethod
    def _clamp_ambient_mode(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _AMBIENT_MODES else "trilight"

    @field_validator("reflection_mode", mode="before")
    @classmethod
    def _clamp_reflection_mode(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _REFLECTION_MODES else "skybox"

    @field_validator(
        "ambient_sky_r", "ambient_sky_g", "ambient_sky_b",
        "ambient_equator_r", "ambient_equator_g", "ambient_equator_b",
        "ambient_ground_r", "ambient_ground_g", "ambient_ground_b",
        mode="before",
    )
    @classmethod
    def _clamp_ambient_channel(cls, v) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @field_validator("ambient_intensity", "reflection_intensity", mode="before")
    @classmethod
    def _clamp_ambient_intensity(cls, v) -> float:
        try:
            return max(0.0, min(8.0, float(v)))
        except (TypeError, ValueError):
            return 1.0


CONCEPT_PROMPT = """\
{theme_block}
Describe the lighting setup for this idea:

"{user_prompt}"

Variant: {variant}

Cover in 1-2 paragraphs:
- The key light: direction, color temperature, how hard the shadows are
- Fill and rim/back light: how much they lift the shadow side and separate
  subjects from the background
- Any practical lights in the world (lamps, neon, fires, headlights, screens)
- The ambient/sky contribution — what color light comes from the environment
  itself when nothing is directly lit

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into a lighting schema. Output ONLY JSON.

Variant: {variant}

three_point        — key + fill + rim directional rig; the general-purpose 3D look
outdoor_sun        — one strong key sun, bright sky ambient, skybox reflections
interior_practical — dim ambient plus point/spot practicals placed in the room
flat_2d            — no light objects at all; flat full-brightness ambient (2D sprites)
stylized_flood     — wide soft floods, minimal shadowing, saturated ambient

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "description": "One sentence.",
  "lights": [
    {{"role": "key",  "type": "directional", "pitch": 50, "yaw": -30,
      "color_r": 1.0, "color_g": 0.96, "color_b": 0.84, "intensity": 1.1, "shadows": "soft"}},
    {{"role": "fill", "type": "directional", "pitch": 25, "yaw": 150,
      "color_r": 0.6, "color_g": 0.7, "color_b": 0.9, "intensity": 0.35, "shadows": "none"}}
  ],
  "ambient_mode": "trilight",
  "ambient_sky_r": 0.42, "ambient_sky_g": 0.47, "ambient_sky_b": 0.56,
  "ambient_equator_r": 0.30, "ambient_equator_g": 0.33, "ambient_equator_b": 0.38,
  "ambient_ground_r": 0.16, "ambient_ground_g": 0.16, "ambient_ground_b": 0.18,
  "ambient_intensity": 1.0,
  "reflection_mode": "skybox",
  "reflection_intensity": 1.0,
  "repair_black_materials": true,
  "respect_atmosphere": true,
  "adopt_scene_sun": true,
  "extra_usings": [],
  "extra_fields": [],
  "extra_methods": []
}}

Light roles: key | fill | rim | practical | flood | area
Light types: directional | point | spot | area
Shadows:     none | hard | soft

Colors are 0..1 floats. Point/spot lights also use "x"/"y"/"z", "range" and
"spot_angle"; directional lights use "pitch"/"yaw" only.

Output ONLY valid JSON.
"""


# ── Per-variant rig blueprints ────────────────────────────────────────────────
# Used only when the schema supplies no lights of its own. Angles/roles are
# structural (the shape of a three-point rig is not a stylistic choice); COLOR
# and INTENSITY are overwritten from the project's palette + visual style by
# _apply_project_light() below, so no generic look leaks through.

_RIG_BLUEPRINTS: dict[str, list[dict]] = {
    "three_point": [
        {"role": "key",  "type": "directional", "pitch": 50.0, "yaw": -30.0,
         "intensity": 1.1, "shadows": "soft"},
        {"role": "fill", "type": "directional", "pitch": 25.0, "yaw": 150.0,
         "intensity": 0.35, "shadows": "none"},
        {"role": "rim",  "type": "directional", "pitch": 12.0, "yaw": 75.0,
         "intensity": 0.45, "shadows": "none"},
    ],
    "outdoor_sun": [
        {"role": "key",  "type": "directional", "pitch": 55.0, "yaw": -35.0,
         "intensity": 1.3, "shadows": "soft"},
        {"role": "fill", "type": "directional", "pitch": 20.0, "yaw": 160.0,
         "intensity": 0.25, "shadows": "none"},
    ],
    "interior_practical": [
        {"role": "key",       "type": "directional", "pitch": 60.0, "yaw": -20.0,
         "intensity": 0.45, "shadows": "soft"},
        {"role": "practical", "type": "point", "x": -4.0, "y": 3.0, "z": 2.0,
         "intensity": 2.5, "range": 12.0, "shadows": "none"},
        {"role": "practical", "type": "point", "x": 4.0, "y": 3.0, "z": -3.0,
         "intensity": 2.5, "range": 12.0, "shadows": "none"},
        {"role": "practical", "type": "spot", "x": 0.0, "y": 5.0, "z": 0.0,
         "pitch": 90.0, "yaw": 0.0, "intensity": 3.0, "range": 16.0,
         "spot_angle": 70.0, "shadows": "hard"},
    ],
    "flat_2d": [],
    "stylized_flood": [
        {"role": "key",   "type": "directional", "pitch": 45.0, "yaw": -30.0,
         "intensity": 1.0, "shadows": "none"},
        {"role": "flood", "type": "spot", "x": -6.0, "y": 8.0, "z": -4.0,
         "pitch": 55.0, "yaw": 35.0, "intensity": 4.0, "range": 30.0,
         "spot_angle": 110.0, "shadows": "none"},
        {"role": "flood", "type": "spot", "x": 6.0, "y": 8.0, "z": -4.0,
         "pitch": 55.0, "yaw": -35.0, "intensity": 4.0, "range": 30.0,
         "spot_angle": 110.0, "shadows": "none"},
    ],
}

# Ambient mode per variant. flat_2d uses Flat because a 2D project disables the
# scene's Directional Light entirely (scene.unity.j2 m_IsActive) — sprites are
# lit by ambient alone, so a directional gradient would just darken them.
_AMBIENT_MODE_BY_VARIANT = {
    "three_point":        "trilight",
    "outdoor_sun":        "skybox",
    "interior_practical": "trilight",
    "flat_2d":            "flat",
    "stylized_flood":     "trilight",
}

# UnityEngine.Rendering.AmbientMode as serialized in scene YAML (m_AmbientMode).
_UNITY_AMBIENT_MODE = {"skybox": 0, "trilight": 1, "flat": 3}


# ── Variant selection ─────────────────────────────────────────────────────────
# Lives here, not in unity_compose, so the generator can pick the same variant
# for a NON-composed project — otherwise the scene YAML and the runtime guard
# would disagree the moment --compose is off.

_INTERIOR_WORDS = frozenset({
    "interior", "indoor", "indoors", "room", "corridor", "hallway", "bunker",
    "vault", "dungeon", "cave", "cavern", "warehouse", "lab", "laboratory",
    "basement", "cellar", "tunnel", "mine", "facility", "office", "apartment",
    "crypt", "tomb", "catacomb", "sewer", "hangar", "cockpit", "submarine",
    "spaceship", "starship", "elevator", "station", "chamber", "shaft", "silo",
    "airlock", "vent", "ductwork", "stairwell", "lobby",
})
_EXTERIOR_WORDS = frozenset({
    "outdoor", "outdoors", "desert", "forest", "wood", "field", "street",
    "city", "town", "island", "beach", "coast", "mountain", "valley", "road",
    "highway", "track", "circuit", "sky", "countryside", "jungle", "canyon",
    "tundra", "wasteland", "plain", "meadow", "ocean", "sea", "harbour",
    "harbor", "rooftop", "hillside", "savanna", "swamp", "glacier",
})


def select_variant(game_type: str, visual_style: str = "stylized",
                   time_of_day: str = "", explicit_env: bool = False,
                   user_prompt: str = "", scenes: list[str] | None = None) -> str:
    """Deterministic lighting variant from the project's own content.

    Interior/exterior is the dominant signal — it decides whether practicals or
    a sun carry the scene. Time-of-day only breaks ties.
    """
    gt = (game_type or "3D").lower()
    if gt in ("2d", "2d_topdown"):
        return "flat_2d"

    text  = " ".join([(user_prompt or "").lower(), " ".join(scenes or [])]).lower()
    words = set(re.split(r"[^a-z]+", text))
    # Depluralise so the word tables stay singular: "corridors"/"tunnels" are
    # exactly the forms these prompts actually use, and enumerating both
    # spellings of every entry is how one silently goes missing.
    words |= {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}
    interior = bool(words & _INTERIOR_WORDS)
    exterior = bool(words & _EXTERIOR_WORDS)

    # Exterior wins outright — a night race is still lit by a sky, not by lamps.
    if exterior and not interior:
        return "outdoor_sun"
    if interior:
        return "interior_practical"
    if time_of_day == "night":
        return "interior_practical"
    if explicit_env and time_of_day in ("dawn", "day", "sunset"):
        return "outdoor_sun"
    # Flat-shaded styles read better under wide floods than a hard key.
    if visual_style in ("cartoon", "pixel_art"):
        return "stylized_flood"
    return "three_point"


# ── Ambient resolution (single source of truth) ───────────────────────────────

class AmbientResolution(BaseModel):
    """The resolved environment lighting for one project.

    Consumed by BOTH LightingPipeline.default_fix (which bakes it into the
    generated C#) and unity_generator (which bakes it into scene.unity.j2's
    RenderSettings), so the scene at rest and the scene at Play agree.
    """
    mode:       str    # skybox | trilight | flat
    unity_mode: int    # m_AmbientMode as serialized in scene YAML
    sky:        tuple[float, float, float]
    equator:    tuple[float, float, float]
    ground:     tuple[float, float, float]
    intensity:  float
    lifted:     bool = False   # True when the floor guard had to brighten it


def _palette_slot(palette: dict, slot: str, fallback: tuple[float, float, float]):
    entry = (palette or {}).get(slot)
    if not isinstance(entry, dict):
        return fallback
    try:
        return (float(entry["r"]), float(entry["g"]), float(entry["b"]))
    except (KeyError, TypeError, ValueError):
        return fallback


def resolve_ambient(
    palette_rgb: dict | None,
    variant:     str,
    *,
    sky:       tuple[float, float, float] | None = None,
    equator:   tuple[float, float, float] | None = None,
    ground:    tuple[float, float, float] | None = None,
    intensity: float = 1.0,
    authored:  bool  = False,
) -> AmbientResolution:
    """Resolve the environment lighting for a project. Pure and deterministic.

    Seeds the gradient from the palette (background, background*0.6,
    secondary*0.3 — the shape scene.unity.j2 has always used), then applies the
    AMBIENT_LUMINANCE_FLOOR guard. `authored=True` keeps caller-supplied colors
    and only runs the guard.
    """
    mode = _AMBIENT_MODE_BY_VARIANT.get(variant, "trilight")

    sky     = sky     if sky     is not None else (0.42, 0.47, 0.56)
    equator = equator if equator is not None else (0.30, 0.33, 0.38)
    ground  = ground  if ground  is not None else (0.16, 0.16, 0.18)

    if palette_rgb and not authored:
        br, bg, bb = _palette_slot(palette_rgb, "background", (0.0, 0.0, 0.0))
        sr, sg, sb = _palette_slot(palette_rgb, "secondary",  (0.0, 0.0, 0.0))
        if _luminance(br, bg, bb) > 0.0:
            sky     = (br, bg, bb)
            equator = (round(br * 0.6, 4), round(bg * 0.6, 4), round(bb * 0.6, 4))
            ground  = (round(sr * 0.3, 4), round(sg * 0.3, 4), round(sb * 0.3, 4))

    intensity = max(0.0, min(8.0, float(intensity)))
    lifted    = False

    if _luminance(*sky) > 0.0 and _luminance(*sky) * intensity < AMBIENT_LUMINANCE_FLOOR:
        # White ambient at intensity i tops out at luminance i, so anything below
        # the floor needs the intensity raised before the color can help.
        if intensity < AMBIENT_LUMINANCE_FLOOR:
            intensity = AMBIENT_LUMINANCE_FLOOR
        # Small margin so the 4-dp rounding inside the lift can't land a hair
        # under the floor and re-open the exact hole this guard closes.
        target = min(1.0, (AMBIENT_LUMINANCE_FLOOR * 1.01) / intensity)
        new_sky = _lift_to_luminance(*sky, target)
        if new_sky is not None:
            sky     = new_sky
            lifted  = True
            # Re-seat the lower bands relative to the lifted sky, preserving the
            # gradient shape. Each band keeps its own hue — only its luminance
            # moves, so a bright `secondary` ground can't overshoot the sky.
            sky_lum = _luminance(*sky)
            equator = _set_luminance(*equator, sky_lum * 0.6) or \
                tuple(round(c * 0.6, 4) for c in sky)          # type: ignore[assignment]
            ground  = _set_luminance(*ground, sky_lum * 0.3) or \
                tuple(round(c * 0.3, 4) for c in sky)          # type: ignore[assignment]

    return AmbientResolution(
        mode=mode, unity_mode=_UNITY_AMBIENT_MODE.get(mode, 1),
        sky=sky, equator=equator, ground=ground,
        intensity=round(intensity, 4), lifted=lifted,
    )


# ── Default rig construction (pure, LLM-free) ─────────────────────────────────
# Factored out of LightingPipeline so the deterministic generator path (Steps
# 4-7) can bake an identical baseline rig for a non-`--compose` build without
# constructing a LightingPipeline — its __init__ (via MicroPipeline) builds an
# LLM chat client that would never be called here, exactly the kind of Steps
# 4-7 LLM blur the Pipeline Contract forbids. LightingPipeline's own methods
# below delegate to these for the `--compose` path, so both stay byte-for-byte
# in agreement.

def _tint_light(light: LightSpec, palette: dict) -> None:
    """Tint a blueprint light from the project palette.

    key   — warm sun, left at near-white so albedo reads true
    fill  — the background/sky color (what actually bounces onto shadow sides)
    rim   — the accent color, so silhouettes pick up the project's identity
    others— trim, the neutral practical color
    """
    if not palette:
        return
    slot = {"key": "trim", "fill": "background", "rim": "accent"}.get(
        light.role, "trim")
    r, g, b = _palette_slot(palette, slot, (light.color_r, light.color_g, light.color_b))
    # A near-black palette slot would defeat the point of the light; lift it
    # to a usable tint while keeping its hue.
    lifted = _lift_to_luminance(r, g, b, LIGHT_TINT_FLOOR)
    if lifted is None:
        return
    light.color_r, light.color_g, light.color_b = lifted


def _enforce_key_light(schema, variant: str, *, on_fix=None) -> None:
    """Lift the summed directional intensity to KEY_INTENSITY_FLOOR."""
    if variant == "flat_2d":
        return
    directional = sum(l.intensity for l in schema.lights if l.type == "directional")
    if directional < KEY_INTENSITY_FLOOR:
        key = next((l for l in schema.lights if l.role == "key"), None)
        if key is None:
            key = next((l for l in schema.lights if l.type == "directional"), None)
        if key is not None:
            key.intensity = round(key.intensity + (KEY_INTENSITY_FLOOR - directional), 4)
            if on_fix:
                on_fix(f"key light lifted to the {KEY_INTENSITY_FLOOR} directional floor "
                       f"(intensity -> {key.intensity})")


def _enforce_lit(schema, variant: str, palette: dict, *, on_fix=None) -> None:
    """Guarantee the rig can never resolve to an unlit scene.

    Delegates the ambient half to resolve_ambient() — the same pure function
    unity_generator uses to bake scene.unity.j2's RenderSettings, so the
    scene at rest and the scene at Play cannot drift apart. Raises when
    there is genuinely nothing to lift: an all-zero ambient with no lights
    is not a look, it's a broken scene, and shipping it silently is what
    produced the black-materials bug this micro exists to close.
    """
    res = resolve_ambient(
        palette, variant,
        sky=(schema.ambient_sky_r, schema.ambient_sky_g, schema.ambient_sky_b),
        equator=(schema.ambient_equator_r, schema.ambient_equator_g,
                 schema.ambient_equator_b),
        ground=(schema.ambient_ground_r, schema.ambient_ground_g,
                schema.ambient_ground_b),
        intensity=schema.ambient_intensity,
        authored=bool(schema.meta.get("ambient_authored")),
    )

    if _luminance(*res.sky) <= 0.0 and not schema.lights:
        raise ValueError(
            f"lighting: variant {variant!r} resolves to a fully unlit scene "
            "(ambient luminance 0, no lights). Refusing to emit — supply an "
            "ambient color or at least one light."
        )

    schema.ambient_mode = res.mode
    schema.ambient_intensity = res.intensity
    (schema.ambient_sky_r, schema.ambient_sky_g, schema.ambient_sky_b) = res.sky
    (schema.ambient_equator_r, schema.ambient_equator_g,
     schema.ambient_equator_b) = res.equator
    (schema.ambient_ground_r, schema.ambient_ground_g,
     schema.ambient_ground_b) = res.ground
    if res.lifted and on_fix:
        on_fix(f"ambient lifted to the {AMBIENT_LUMINANCE_FLOOR} luminance "
               f"floor (sky luminance -> {_luminance(*res.sky):.4f})")

    _enforce_key_light(schema, variant, on_fix=on_fix)


def build_default_lighting_schema(
        variant: str, palette: dict | None = None, *,
        name: str = "LightingDirector", description: str = "",
        on_fix=None) -> "LightingSchema":
    """The same default light rig + ambient guard `LightingPipeline.default_fix`
    bakes for an unauthored (no LLM content) schema — pure and LLM-free, for
    callers that need a baseline rig without going through the micro pipeline.
    """
    schema = LightingSchema(
        name=name or "LightingDirector",
        variant=variant,
        description=description or "Scene light rig and environment-lighting guard",
    )
    palette = palette or {}
    if variant == "flat_2d":
        schema.lights = []
        schema.ambient_mode = "flat"
    else:
        schema.lights = [LightSpec(**bp) for bp in _RIG_BLUEPRINTS.get(variant, [])]
        for light in schema.lights:
            _tint_light(light, palette)
    _enforce_lit(schema, variant, palette, on_fix=on_fix)
    return schema


class LightingPipeline(UnityMicroPipeline):
    kind            = "lighting"
    display_name    = "Unity Lighting"
    schema_cls      = LightingSchema
    variants        = VARIANTS
    default_variant = "three_point"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "lighting"

    _RESERVED_FIELD_NAMES = {
        "_rigroot", "_key", "_built", "_lights", "_probe",
    }
    _TEMPLATE_USINGS = {
        "UnityEngine", "System.Collections", "System.Collections.Generic",
        "UnityEngine.Rendering",
    }

    # ── fixer ────────────────────────────────────────────────────────────────

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "LightingDirector")

        palette = self._palette(schema)

        # 2D projects have no lit geometry to rig — force the flat path so a
        # stray LLM-authored directional light can't darken sprites.
        if variant == "flat_2d":
            schema.lights = []
            schema.ambient_mode = "flat"
        else:
            if not schema.lights:
                schema.lights = [
                    LightSpec(**bp) for bp in _RIG_BLUEPRINTS.get(variant, [])
                ]
                for light in schema.lights:
                    _tint_light(light, palette)

        # Ambient mode/colors/intensity are resolved (and floor-guarded) in one
        # place — see resolve_ambient. flat_2d's "flat" mode falls out of the
        # variant table, so the branch above no longer needs to set it.
        self._enforce_lit(schema, variant, palette)

        from .code_fields import (
            _KNOWN_NAMESPACES, template_field_types, lint_field_type_misuse,
            _repair_collection_compared_as_scalar, _repair_animator_play_layer_arg,
            _repair_self_class_static_call, _repair_struct_return_type_mismatch,
            _repair_field_assignment_type_mismatch, template_method_return_types,
            _body_is_live,
        )
        schema.extra_usings = lint_usings(
            schema.extra_usings, already_in_template=self._TEMPLATE_USINGS,
            allowed=_KNOWN_NAMESPACES,
        )
        schema.extra_fields = lint_fields(
            schema.extra_fields, reserved=self._RESERVED_FIELD_NAMES,
        )
        # template_field_types: this micro predates lint_extensible (same gap
        # animator.py/spawner.py had — see their own comments) so the
        # TEMPLATE's own fields (ambientSky: Color, etc.) were invisible to
        # every misuse check below until now. Confirmed live (2026-08-28),
        # LightingDirector.cs: GetSkyboxColor() returned a Vector3-typed
        # extra_field where Color was declared (CS0029) — see
        # _repair_struct_return_type_mismatch's own docstring.
        field_types = {**template_field_types(str(self.template_root), variant),
                       **{f.name: f.type for f in schema.extra_fields if f.name}}
        method_return_types = template_method_return_types(str(self.template_root), variant)
        kept_methods = []
        for m in schema.extra_methods:
            body = lint_code_fragment(m.body, what="method body")
            if field_types:
                body, _ = lint_field_type_misuse(body, field_types=field_types,
                                                 signature=m.signature)
                body = _repair_collection_compared_as_scalar(body, field_types)
                body = _repair_animator_play_layer_arg(body, field_types)
                body = _repair_struct_return_type_mismatch(
                    body, field_types, signature=m.signature)
                body = _repair_field_assignment_type_mismatch(
                    body, field_types=field_types,
                    method_return_types=method_return_types)
            body = _repair_self_class_static_call(body, schema.name)
            # Not lint_method (this micro predates it, same reason it calls
            # lint_code_fragment directly above) — but its OWN dead-body drop
            # is still needed: a neutralized return leaves a non-void method
            # with no return on any path (CS0161), a NEW error the fixes
            # above alone would trade for the ones they just removed.
            if _body_is_live(body):
                kept_methods.append(m.model_copy(update={"body": body}))
        schema.extra_methods = kept_methods
        return schema

    # ── project-derived light character ──────────────────────────────────────

    def _palette(self, schema) -> dict:
        """Project palette (0..1 RGB per slot) if compose supplied one.

        Compose passes `meta["palette_rgb"]` straight from the generator's own
        scene context (unity_generator._unity_color output), so the rig is lit
        in the project's real colors rather than a generic white.
        """
        meta = getattr(schema, "meta", None) or {}
        pal  = meta.get("palette_rgb")
        return pal if isinstance(pal, dict) else {}

    @staticmethod
    def _slot(palette: dict, slot: str, fallback: tuple[float, float, float]):
        entry = palette.get(slot)
        if not isinstance(entry, dict):
            return fallback
        try:
            return (float(entry["r"]), float(entry["g"]), float(entry["b"]))
        except (KeyError, TypeError, ValueError):
            return fallback

    def _apply_project_light(self, light: LightSpec, palette: dict) -> None:
        _tint_light(light, palette)

    # ── the "never black" invariant ──────────────────────────────────────────

    def _enforce_lit(self, schema, variant: str, palette: dict) -> None:
        """Guarantee the rig can never resolve to an unlit scene.

        Delegates to the module-level `_enforce_lit`/`_enforce_key_light` (see
        their docstrings) — this wrapper only supplies `self._fix` as the
        logging sink, so `--compose` output stays byte-identical to before
        this was factored out for reuse by the non-compose generator path.
        """
        _enforce_lit(schema, variant, palette, on_fix=self._fix)

    def _enforce_key_light(self, schema, variant: str) -> None:
        _enforce_key_light(schema, variant, on_fix=self._fix)

    # ── writer ───────────────────────────────────────────────────────────────

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("LightingDirector.cs.j2").render(
                s=schema, variant=variant, ambient_floor=AMBIENT_LUMINANCE_FLOOR),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
