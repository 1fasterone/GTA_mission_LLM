"""
Job builder — turn the LLM story (Unity concept + schema entities) into a queue
of BlenderJobs, and decide each one's fulfilment route.

This is where the goal's branching logic lives:

  "If Blender can procedurally produce textures and maps, use the LLM story and
   construct an image request to Blender; if not, a second pipeline goes to a
   diffusion model with a prompt constructed from the LLM description."

route_for() is that decision. The heuristic is intentionally conservative and
deterministic (no LLM call): a style/asset combination is "procedural" when
Blender's node materials can plausibly cover it (low-poly/stylized/cartoon, or
any geometric surface like terrain/buildings), and "diffusion" when it needs a
painted, photoreal, or highly authored surface (realistic characters, detailed
props, sci-fi panels) that procedural nodes won't sell.
"""

from __future__ import annotations

from typing import Optional

from .job_schema import BlenderJob, TextureSpec, OutputSpec, TextureSource


# Geometry-nodes generators we ship recipes for (see scripts/run_job.py). An
# asset_type maps to a default generator so "character" → human_base etc.
_GEOMETRY_NODES: dict[str, str] = {
    "character":   "human_base",
    "creature":    "creature_base",
    "prop":        "prop_kit",
    "weapon":      "prop_kit",
    "vehicle":     "vehicle_base",
    "building":    "building_kit",
    "environment": "scatter_kit",
    "terrain":     "terrain_noise",
    "foliage":     "foliage_scatter",
    "material":    "",                 # material-only job, no geometry
}

# Styles whose surfaces Blender's procedural node materials handle well.
_PROCEDURAL_STYLES = frozenset({"low_poly", "stylized", "cartoon", "pixel_art", "retro"})

# Asset types that are fundamentally geometric — procedural even when realistic
# (a realistic terrain/rock/building is still noise + triplanar, not a painting).
_GEOMETRIC_TYPES = frozenset({"terrain", "environment", "building", "foliage", "prop"})

# Asset types that read as "authored surface" — a realistic one needs diffusion.
_AUTHORED_SURFACE_TYPES = frozenset({"character", "creature", "weapon", "vehicle"})

# Default animation sets per asset type (only applied when rig is on and the
# LLM didn't specify any).
_DEFAULT_ANIMS: dict[str, list[str]] = {
    "character": ["idle", "walk", "run"],
    "creature":  ["idle", "walk", "attack"],
}


def route_for(asset_type: str, style: str) -> str:
    """Return "blender" (procedural textures) or "diffusion" (painted textures).

    Deterministic, no LLM. See module docstring for the rationale.
    """
    if style in _PROCEDURAL_STYLES:
        return "blender"
    # Realistic / sci-fi from here on.
    if asset_type in _GEOMETRIC_TYPES:
        return "blender"          # geometric surfaces stay procedural
    if asset_type in _AUTHORED_SURFACE_TYPES:
        return "diffusion"        # realistic skin/metal/paint → diffusion maps
    return "blender"


def _texture_source_for(route: str) -> TextureSource:
    return "diffusion" if route == "diffusion" else "procedural"


def build_job(
    *,
    name: str,
    asset_type: str = "prop",
    style: str = "stylized",
    description: str = "",
    rig: Optional[bool] = None,
    animations: Optional[list[str]] = None,
    lods: bool = True,
    resolution: int = 1024,
    detail: int = 3,
    color: str = "",
    soft_body: bool = False,
    seed: Optional[int] = None,
    output_base: str = "output/blender",
) -> BlenderJob:
    """Construct one validated BlenderJob, filling sensible defaults and routing.

    `rig`/`animations` default from the asset type: characters & creatures get
    rigged with a basic locomotion set unless the caller says otherwise.
    """
    # Validate type/style first so routing sees the clamped values. Passing a
    # plain str here is intentional — the model's mode="before" validators
    # clamp any unrecognized value down to a valid Literal at runtime.
    probe = BlenderJob(name=name, asset_type=asset_type, style=style)  # type: ignore[arg-type]
    atype, astyle = probe.asset_type, probe.style

    route = route_for(atype, astyle)

    auto_rig = atype in ("character", "creature") if rig is None else rig
    anims = animations
    if auto_rig and not anims:
        anims = _DEFAULT_ANIMS.get(atype, ["idle"])
    if not auto_rig:
        anims = []

    job = BlenderJob(
        name           = name,
        asset_type     = atype,
        style          = astyle,
        description    = description,
        rig            = auto_rig,
        animations     = anims or [],
        lods           = lods,
        geometry_nodes = _GEOMETRY_NODES.get(atype, "prop_kit"),
        detail         = detail,
        color          = color,
        soft_body      = soft_body,
        seed           = seed,
        textures = TextureSpec(
            resolution     = resolution,
            bake           = True,
            source         = _texture_source_for(route),
            material_brief = description,
        ),
        output = OutputSpec(),
        meta   = {"route": route},
    )
    return job.default_output(output_base)


# ── Drive from a Unity schema ─────────────────────────────────────────────────

def build_jobs_for_schema(
    schema,
    concept: str = "",
    *,
    output_base: str = "output/blender",
    max_jobs: int = 12,
    master_seed: Optional[int] = None,
) -> list[BlenderJob]:
    """Derive a job queue from a resolved UnitySchema.

    Sources of assets, in priority order:
      1. schema.entities — the prompt-derived scene content ("6 rival cars",
         "a ring of buildings"); each distinct `kind` becomes one job.
      2. character/enemy scripts — imply a rigged character asset.
    Deduplicates by job name and caps at `max_jobs`.

    The Unity schema's own `visual_style` carries the project art direction; we
    map it onto each job's style so the whole package stays visually coherent.
    """
    from .seeding import derive_seed
    style = _map_unity_style(getattr(schema, "visual_style", "stylized"))
    palette = getattr(schema, "color_palette", None)
    # Reuse the Unity theme seed (themes.resolve_theme records it in meta) as the
    # master so the whole prompt→schema→assets chain is reproducible together.
    if master_seed is None and isinstance(getattr(schema, "meta", None), dict):
        master_seed = schema.meta.get("seed")
    jobs: list[BlenderJob] = []
    seen: set[str] = set()

    def _seed_for(name: str) -> Optional[int]:
        return derive_seed(master_seed, name) if master_seed is not None else None

    def _color_for(role: str) -> str:
        if palette is None:
            return ""
        return getattr(palette, role if role in
                       ("primary", "secondary", "accent", "trim", "background")
                       else "accent", "") or ""

    def _add(job: BlenderJob) -> None:
        if job.name in seen:
            return
        seen.add(job.name)
        jobs.append(job)

    # 1) entities → props/creatures/vehicles/buildings
    for ent in getattr(schema, "entities", []) or []:
        if len(jobs) >= max_jobs:
            break
        kind = (ent.kind or ent.name or "prop").strip()
        atype = _kind_to_asset_type(kind, getattr(schema, "locomotion", "walk"))
        desc = f"{kind} for {getattr(schema, 'description', '') or concept[:160]}".strip()
        jn = _job_name(ent.name or kind)
        _add(build_job(
            name        = jn,
            asset_type  = atype,
            style       = style,
            description = desc,
            lods        = True,
            color       = _color_for(getattr(ent, "color_role", "accent")),
            seed        = _seed_for(jn),
            output_base = output_base,
        ))

    # 2) explicit character/enemy scripts → a hero/enemy character asset
    script_types = {s.type for s in getattr(schema, "scripts", [])}
    if ("player" in script_types or "enemy" in script_types) and len(jobs) < max_jobs:
        desc = (concept[:200] or getattr(schema, "description", "")
                or "playable character")
        cn = _job_name(f"{getattr(schema, 'project_name', 'Hero')}_Character")
        _add(build_job(
            name        = cn,
            asset_type  = "character",
            style       = style,
            description = desc,
            rig         = True,
            color       = _color_for("primary"),
            seed        = _seed_for(cn),
            output_base = output_base,
        ))

    return jobs[:max_jobs]


# ── helpers ───────────────────────────────────────────────────────────────────

_UNITY_STYLE_MAP = {
    "cartoon": "cartoon", "realistic": "realistic", "low_poly": "low_poly",
    "stylized": "stylized", "pixel_art": "pixel_art", "retro": "retro",
}


def _map_unity_style(s: str) -> str:
    return _UNITY_STYLE_MAP.get(str(s).lower().strip(), "stylized")


def _kind_to_asset_type(kind: str, locomotion: str) -> str:
    k = kind.lower()
    table = [
        (("car", "vehicle", "truck", "bike", "racer"), "vehicle"),
        (("enemy", "soldier", "guard", "npc", "person", "rival", "hero"), "character"),
        (("monster", "beast", "creature", "animal", "zombie"), "creature"),
        (("building", "house", "tower", "wall", "structure"), "building"),
        (("tree", "bush", "plant", "grass", "foliage"), "foliage"),
        (("gun", "sword", "weapon", "rifle"), "weapon"),
        (("terrain", "ground", "ground_plane", "landscape"), "terrain"),
    ]
    for needles, atype in table:
        if any(n in k for n in needles):
            return atype
    if locomotion == "drive":
        return "vehicle"
    return "prop"


def _job_name(raw: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(raw)).strip("_")
    return s or "Asset"
