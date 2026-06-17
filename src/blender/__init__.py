"""
Blender Job Manager — asset-fulfilment stage for the Unity pipeline.

The Unity pipeline (src/unity_pipeline.py) compiles an LLM concept into a Unity
project, but its meshes are runtime primitives + Kenney low-poly imports. This
package is the *external* asset factory that raises package quality: it turns the
LLM story into Blender jobs, runs Blender headless as both a **generator** and a
**sanitizer** (mesh cleanup → UV unwrap → LOD → Rigify auto-rig → animation →
Unity-friendly FBX export), and returns an asset manifest the Unity overlay can
consume.

Two fulfilment routes (decided per asset by job_builder.route_for):
  * "blender"   — Blender can produce the geometry AND its textures procedurally
                  (geometry nodes + procedural material → baked maps).
  * "diffusion" — Blender can build the geometry but NOT a convincing texture
                  (e.g. a realistic character skin); a second pipeline sends an
                  LLM-constructed prompt to a diffusion model for the maps, which
                  Blender then bakes/assigns before export.

Everything degrades gracefully when Blender / the diffusion backend isn't
installed (same philosophy as the headless Unity `verify` gate): jobs resolve to
a stub manifest instead of blocking the pipeline.
"""

from .job_schema  import BlenderJob, TextureSpec, OutputSpec, JOB_FULFILMENT_ROUTES
from .manifest    import AssetManifest, JobResult, JobStatus
from .locator     import find_blender, blender_available, BlenderInstall
from .job_manager import BlenderJobManager
from .job_builder import build_jobs_for_schema, route_for, build_job
from .unity_overlay import overlay_batch, overlay_asset, load_batch, OverlayResult
from .normalize import normalize_export, normalize_fbx, normalize_alembic
from .cache import BlenderCache, GeoStore, CacheEntry, CACHE_VERSION
from .rigging import (
    rig_strategy, rig_class_for, template_available,
    RigStrategy, RigKind, TEMPLATE_CLASSES, RIG_VERSION,
)

__all__ = [
    "BlenderJob", "TextureSpec", "OutputSpec", "JOB_FULFILMENT_ROUTES",
    "AssetManifest", "JobResult", "JobStatus",
    "find_blender", "blender_available", "BlenderInstall",
    "BlenderJobManager",
    "build_jobs_for_schema", "route_for", "build_job",
    "overlay_batch", "overlay_asset", "load_batch", "OverlayResult",
    "normalize_export", "normalize_fbx", "normalize_alembic",
    "BlenderCache", "GeoStore", "CacheEntry", "CACHE_VERSION",
    "rig_strategy", "rig_class_for", "template_available",
    "RigStrategy", "RigKind", "TEMPLATE_CLASSES", "RIG_VERSION",
]
