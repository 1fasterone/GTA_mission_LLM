"""FX / VFX atoms — particle effects, lighting, screen effects."""

from .base import AtomDef, AtomParam

FX_ATOMS: list[AtomDef] = [
    AtomDef(
        name="SpawnParticle",
        category="fx",
        description="Instantiate a named particle effect prefab at a world position.",
        duration="instant",
        side_effects=["vfx"],
        aliases=["spawn_particle", "spawn_vfx", "particle_effect", "vfx", "fx"],
        params=[
            AtomParam(name="effect", type="vfx_id",  required=True, description="Particle effect identifier"),
            AtomParam(name="x",      type="float",   default=0.0),
            AtomParam(name="y",      type="float",   default=0.0),
            AtomParam(name="z",      type="float",   default=0.0),
            AtomParam(name="scale",  type="float",   default=1.0,  description="Uniform scale"),
        ],
    ),
    AtomDef(
        name="LightFlash",
        category="fx",
        description="Spike a light to a high intensity then restore it after a duration.",
        duration="duration_param",
        side_effects=["lighting"],
        aliases=["light_flash", "flash_light", "muzzle_flash", "strobe"],
        params=[
            AtomParam(name="intensity", type="float", default=5.0,  description="Peak light intensity"),
            AtomParam(name="duration",  type="float", default=0.05, description="Flash duration seconds"),
            AtomParam(name="color_r",   type="float", default=1.0),
            AtomParam(name="color_g",   type="float", default=1.0),
            AtomParam(name="color_b",   type="float", default=1.0),
        ],
    ),
    AtomDef(
        name="ScreenFade",
        category="fx",
        description="Fade a full-screen overlay to or from a solid color.",
        duration="duration_param",
        side_effects=["screen"],
        aliases=["screen_fade", "fade_out", "fade_in", "fade_to_black", "fade_to_white"],
        params=[
            AtomParam(name="color_r",  type="float", default=0.0, description="Fade color R 0..1"),
            AtomParam(name="color_g",  type="float", default=0.0, description="Fade color G 0..1"),
            AtomParam(name="color_b",  type="float", default=0.0, description="Fade color B 0..1"),
            AtomParam(name="alpha",    type="float", default=1.0, description="Target alpha (1=opaque)"),
            AtomParam(name="duration", type="float", default=1.0, description="Fade duration seconds"),
        ],
    ),
]
