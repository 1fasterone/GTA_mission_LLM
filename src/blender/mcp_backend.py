"""
MCP backend — run a BlenderJob inside the *live* Blender the agent is connected
to, instead of spawning `blender --background`.

`execute_blender_code` (the official Blender Lab MCP tool) runs a Python string
in the connected Blender and returns whatever the script assigns to `result`.
The host-side Python process can't call that tool itself, so this module's job
is to **emit** a self-contained bpy script for one job. The agent (or any
injected executor) runs that string via the MCP tool and feeds the returned dict
back to the manager, which parses it into an AssetManifest.

Design constraints (the connected Blender holds the user's real scene):
  * Non-destructive — never reset the scene or delete pre-existing objects.
    Generated geometry goes into a dedicated "BJM_Assets" collection, offset
    from the world origin so it doesn't overlap the user's work.
  * Export only the job's own objects (use_selection=True), never the whole file.
  * Each step is guarded; a missing add-on/operator becomes a warning, not a crash.

The emitted script mirrors scripts/run_job.py (the --background sanitizer) so the
two backends stay behaviourally aligned.
"""

from __future__ import annotations

import json
from pathlib import Path

from .job_schema import BlenderJob
from .recipes    import recipe_for, recipe_source
from .seeding    import effective_seed, recipe_seed

# The canonical step order. The live backend honours whichever subset the caller
# asks for; build_job_code(steps=...) defaults to the full flag-derived set.
ALL_STEPS = ("generate", "cleanup", "unwrap", "material", "softbody",
             "rig", "animate", "lod", "export")

# Collection the manager parks generated assets in, and a world offset so they
# don't sit on top of the user's existing objects.
_COLLECTION = "BJM_Assets"
_SPAWN_X = 4.0
# Frames to bake a soft-body/cloth sim into the Alembic cache.
_BAKE_FRAMES = 48
# Unity Package Manager dep an Alembic (.abc) asset needs to import + play.
ALEMBIC_UNITY_PACKAGE = "com.unity.formats.alembic"

# Palette-driven procedural material, injected into the emitted script. Builds a
# Principled BSDF tinted to the job's palette colour, with a Noise→ColorRamp
# breakup for surface variation and a style-driven roughness. This is where
# "how colourful" actually lands — the colour comes from the project palette /
# entity colour-role threaded through job_builder. Injected as a .format value
# so its braces don't need escaping.
_MATERIAL_SRC = '''
def _hex_to_linear(h):
    h = (h or "#9B59B6").lstrip("#")
    if len(h) != 6:
        h = "9B59B6"
    srgb = [int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4)]
    # sRGB -> linear (Blender works in linear)
    def _lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return [_lin(c) for c in srgb] + [1.0]

def _shift(rgba, f):
    return [max(0.0, min(1.0, c * f)) for c in rgba[:3]] + [1.0]

def _apply_palette_material(obj, color_hex, style, albedo_path=""):
    import os as _os
    base = _hex_to_linear(color_hex)
    name = obj.name + "_mat"
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    used_diffusion = False
    if albedo_path and _os.path.exists(albedo_path):
        # Diffusion route: a model painted the albedo from the LLM prompt — wire
        # it straight into Base Color (overrides the procedural fallback).
        tex = nodes.new("ShaderNodeTexImage")
        try:
            tex.image = bpy.data.images.load(albedo_path, check_existing=True)
            links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            used_diffusion = True
        except Exception:
            used_diffusion = False
    if not used_diffusion:
        # Procedural fallback: Noise + ColorRamp two-tone around the palette colour.
        coord = nodes.new("ShaderNodeTexCoord")
        noise = nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 6.0
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = _shift(base, 0.6)
        ramp.color_ramp.elements[1].color = _shift(base, 1.3)
        links.new(coord.outputs["Object"], noise.inputs["Vector"])
        links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
        links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    # Style → roughness / metallic feel.
    rough = {"realistic": 0.5, "stylized": 0.6, "low_poly": 0.8,
             "cartoon": 0.9, "pixel_art": 1.0, "retro": 0.7}.get(style, 0.6)
    try:
        bsdf.inputs["Roughness"].default_value = rough
    except Exception:
        pass
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    mat["bjm_diffusion"] = used_diffusion
    return mat
'''


def steps_for(job: BlenderJob) -> list[str]:
    """Derive the live step list from a job's flags."""
    steps = ["generate", "cleanup", "unwrap"]
    if job.textures.bake or job.textures.source != "none":
        steps.append("material")
    if job.soft_body:
        steps.append("softbody")
    if job.rig:
        steps.append("rig")
        if job.animations:
            steps.append("animate")
    if job.lods:
        steps.append("lod")
    steps.append("export")
    if job.soft_body:
        steps.append("export_alembic")   # bake the sim to .abc for Unity
    return steps


def build_job_code(job: BlenderJob, out_dir: str, steps: list[str] | None = None,
                   write_manifest: bool = False, *, reuse_blend: str | None = None,
                   rig_strategy_id: str = "", save_mesh: bool = True) -> str:
    """Return a standalone bpy script that runs `steps` for `job` and assigns a
    manifest dict to `result`.

    The SAME script drives both backends — that's how parity is guaranteed:
      * live MCP    — `execute_blender_code` returns `result` to the manager.
      * subprocess  — `write_manifest=True` appends a tail that writes `result`
                      to `<out_dir>/<name>.manifest.json`, which the headless
                      `blender --background --python <script>` run leaves on disk.
    The only difference between the two is that appended tail.

    Geometry reuse (Phase 3):
      * `reuse_blend` — path to a cached pre-rig mesh `.blend`. When set, the script
        appends that mesh instead of running generate/cleanup/unwrap, then runs only
        the material/rig/export delta (falling back to a cold build if the load
        fails). The manager omits the geometry steps from `steps` on a reuse.
      * `save_mesh` — on a cold build, write `<name>.mesh.blend` (the reusable unit,
        clean verts+UVs, no armature) and report it as `mesh_blend`.
      * `rig_strategy_id` — the versioned rig micro (see `rigging.py`) the rig step
        should run; defaults to the Rigify auto path when blank.
    """
    chosen = steps or steps_for(job)
    job_json = json.dumps(job.model_dump())
    out_abs = str(Path(out_dir).resolve()).replace("\\", "/")
    fbx_rel = job.output.fbx
    steps_json = json.dumps(list(chosen))
    recipe_name = recipe_for(job.asset_type, job.geometry_nodes)
    recipe_src = recipe_source(recipe_name)

    return _TEMPLATE.format(
        job_json=job_json,
        out_dir=out_abs,
        fbx_rel=fbx_rel,
        alembic_rel=job.output.alembic,
        mesh_blend_rel=f"{job.name}.mesh.blend",
        reuse_blend=(str(reuse_blend).replace("\\", "/") if reuse_blend else ""),
        rig_strategy_id=rig_strategy_id,
        save_mesh=bool(save_mesh),
        frame_end=_BAKE_FRAMES,
        steps_json=steps_json,
        collection=_COLLECTION,
        spawn_x=_SPAWN_X,
        recipe_name=recipe_name,
        recipe_src=recipe_src,
        material_src=_MATERIAL_SRC,
        job_seed=effective_seed(job),
        recipe_seed=recipe_seed(job),
        write_manifest_tail=(_MANIFEST_TAIL if write_manifest else ""),
    )


# NOTE: doubled braces are literal { } in the emitted script; single braces are
# .format() fields.
_TEMPLATE = '''
import bpy, os, json, math, random

JOB = json.loads({job_json!r})
OUT_DIR = r"{out_dir}"
FBX_REL = {fbx_rel!r}
ALEMBIC_REL = {alembic_rel!r}
MESH_BLEND_REL = {mesh_blend_rel!r}
REUSE_BLEND = {reuse_blend!r}          # cached pre-rig mesh to append (or "")
RIG_STRATEGY = {rig_strategy_id!r}     # versioned rig micro the rig step runs
SAVE_MESH = {save_mesh}                # save the reusable pre-rig mesh on a cold build
FRAME_END = {frame_end}
STEPS = json.loads({steps_json!r})
COLL_NAME = {collection!r}
SPAWN_X = {spawn_x}
DETAIL = int(JOB.get("detail", 3))
COLOR = JOB.get("color", "") or "#9B59B6"
RECIPE_NAME = {recipe_name!r}
JOB_SEED = {job_seed}            # provenance: the seed that made this asset
SEED = {recipe_seed}             # recipe-stage sub-seed (independent of diffusion)
random.seed(SEED)                # pin Python RNG for any stochastic recipe step

operations = []
warnings = []

# ── injected: procedural recipe (_build_geometry) + palette material ──────────
{recipe_src}
{material_src}
# ──────────────────────────────────────────────────────────────────────────────

def _collection():
    coll = bpy.data.collections.get(COLL_NAME)
    if coll is None:
        coll = bpy.data.collections.new(COLL_NAME)
        bpy.context.scene.collection.children.link(coll)
    return coll

def _make_active(obj):
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

def load_reused(name):
    """Append the cached pre-rig mesh from REUSE_BLEND into the job collection.
    Returns the mesh object, or None on failure (caller cold-builds instead)."""
    if not REUSE_BLEND or not os.path.exists(REUSE_BLEND):
        return None
    try:
        with bpy.data.libraries.load(REUSE_BLEND, link=False) as (src, dst):
            dst.objects = list(src.objects)
        appended = [o for o in dst.objects if o is not None]
    except Exception as ex:
        warnings.append("geo reuse load failed: " + str(ex))
        return None
    mesh_objs = [o for o in appended if o.type == "MESH"]
    if not mesh_objs:
        warnings.append("geo reuse: no mesh object in cached .blend")
        return None
    obj = mesh_objs[0]
    obj.name = name
    coll = _collection()
    for o in appended:
        for c in list(o.users_collection):
            c.objects.unlink(o)
        if o.name not in coll.objects:
            coll.objects.link(o)
        o.location = (SPAWN_X, 0, o.location.z)
    operations.append("geo_reuse")
    return obj

def save_pre_rig_mesh(obj):
    """Write the reusable pre-rig mesh as a mesh-only .blend (clean verts+UVs, no
    armature/material yet) and return its path. Best-effort."""
    try:
        path = os.path.join(OUT_DIR, MESH_BLEND_REL)
        os.makedirs(OUT_DIR, exist_ok=True)
        bpy.data.libraries.write(path, {{obj}}, fake_user=True, compress=True)
        operations.append("save_pre_rig_mesh")
        return path.replace("\\\\", "/")
    except Exception as ex:
        warnings.append("pre-rig mesh save failed: " + str(ex))
        return None

def generate():
    name = JOB.get("name", "Asset")
    # The injected recipe builds + shapes real geometry (modifiers/displacement).
    obj = _build_geometry(name, COLOR, DETAIL, SEED)
    obj.name = name
    # Move into the job collection (unlink from wherever the operators put it).
    coll = _collection()
    for c in list(obj.users_collection):
        if c is not coll:
            c.objects.unlink(obj)
    if obj.name not in coll.objects:
        coll.objects.link(obj)
    operations.append("generate:" + recipe_label())
    return obj

def recipe_label():
    try:
        return RECIPE_NAME
    except Exception:
        return "recipe"

def cleanup(obj):
    _make_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.0001)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.mesh.delete_loose()
    bpy.ops.object.mode_set(mode="OBJECT")
    operations.append("mesh_cleanup")

def unwrap(obj):
    _make_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    try:
        bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=0.02)
        operations.append("uv_unwrap")
    except Exception as ex:
        warnings.append("uv_unwrap failed: " + str(ex))
    bpy.ops.object.mode_set(mode="OBJECT")

def material(obj):
    # Palette-driven material; if the diffusion pre-pass left an albedo PNG, the
    # injected _apply_palette_material wires it into Base Color instead.
    albedo = JOB.get("meta", {{}}).get("diffusion_albedo", "")
    mat = _apply_palette_material(obj, COLOR, JOB.get("style", "stylized"), albedo)
    operations.append("material:diffusion" if mat.get("bjm_diffusion") else "material:procedural")

def softbody(obj):
    """Add a Soft Body modifier (jiggle physics) so the asset deforms under
    motion, and set up an excitation so the bake captures real movement: a denser
    base mesh, a low goal so it's springy, and a keyframed drop so it wobbles as
    it falls and settles. The deformation is a Blender *simulation* — Unity can't
    run it, so export_alembic bakes it to a .abc point cache Unity replays via the
    Alembic package."""
    _make_active(obj)
    try:
        # Denser base mesh so the deformation reads smoothly.
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.subdivide(number_cuts=3)
        bpy.ops.object.mode_set(mode="OBJECT")
        scene = bpy.context.scene
        scene.frame_start = 1
        scene.frame_end = FRAME_END
        scene.frame_set(1)
        # 1) Genuine Soft Body physics — present for anyone working the asset in
        #    interactive Blender. (In headless Blender 5.1 the solver only produced
        #    rigid motion in testing, so it is NOT relied on for the baked result.)
        obj.modifiers.new("SoftBody", "SOFT_BODY")
        sb = obj.soft_body
        sb.use_goal = True
        sb.goal_default = 0.5
        sb.friction = 2.0
        sb.bend = 1.0
        sb.mass = 1.0
        operations.append("softbody_sim")
        # 2) Reliable, deterministic jiggle that actually bakes: an animated Cast
        #    (sphere-ify) factor → squash-and-release. This is what guarantees the
        #    Alembic carries real per-frame vertex animation for Unity.
        cast = obj.modifiers.new("Jiggle", "CAST")
        cast.cast_type = "SPHERE"
        cast.factor = 0.0
        for fr, val in ((1, 0.0), (10, 0.55), (20, 0.0), (30, 0.3), (40, 0.0), (FRAME_END, 0.0)):
            cast.factor = val
            obj.keyframe_insert(data_path='modifiers["Jiggle"].factor', frame=fr)
        cast.factor = 0.0
        operations.append("jiggle_anim")
    except Exception as ex:
        warnings.append("softbody add failed: " + str(ex))

def export_alembic(objs):
    """Bake the soft-body sim into an Alembic (.abc) point cache by stepping the
    frame range — each frame records the *evaluated* (deformed) mesh, so Unity's
    Alembic package replays the jiggle as vertex animation."""
    abc_path = os.path.join(OUT_DIR, ALEMBIC_REL)
    os.makedirs(os.path.dirname(abc_path) or OUT_DIR, exist_ok=True)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    scene = bpy.context.scene
    scene.frame_set(scene.frame_start)
    if not hasattr(bpy.ops.wm, "alembic_export"):
        try:
            bpy.ops.preferences.addon_enable(module="io_mesh_alembic")
        except Exception as ex:
            warnings.append("Alembic add-on unavailable: " + str(ex))
            return None
    try:
        bpy.ops.wm.alembic_export(
            filepath=abc_path, start=scene.frame_start, end=scene.frame_end,
            selected=True, flatten=False, uvs=True, packuv=True, face_sets=True,
            global_scale=1.0, evaluation_mode="RENDER",
        )
    except Exception as ex:
        warnings.append("alembic export failed: " + str(ex))
        return None
    operations.append("export_alembic")
    return abc_path.replace("\\\\", "/")

def make_lods(obj):
    levels = int(JOB.get("lod_levels", 3))
    names = []
    coll = _collection()
    for i in range(1, levels):
        ratio = max(0.1, 1.0 - (i * 0.3))
        lod = obj.copy()
        lod.data = obj.data.copy()
        lod.name = obj.name + "_LOD" + str(i)
        coll.objects.link(lod)
        lod.location = (SPAWN_X + i * 2.5, 0, lod.location.z)
        mod = lod.modifiers.new(name="Decimate", type="DECIMATE")
        mod.ratio = ratio
        names.append(lod.name)
    operations.append("lod_x" + str(levels))
    return names

def rig(obj):
    """Dispatch the rig step to the versioned micro chosen by the host router
    (rigging.rig_strategy). Templates fall back to a deterministic auto micro when
    no authored armature ships — so the pipeline always rigs *something*."""
    strat = RIG_STRATEGY or "RigAutoHumanoidV1"
    if strat.startswith("RigAutoStatic"):
        return None                       # static asset — no skeleton
    if strat.startswith("RigTemplate"):
        r = _append_template_armature(obj, strat)
        if r is not None:
            return r
        # no authored library → deterministic auto fallback (mechanical vs humanoid)
        if any(k in strat for k in ("Car", "Door", "Rifle")):
            return _mechanical_rig(obj)
        return _rigify_autorig(obj)
    if strat.startswith("RigAutoMechanical") or strat.startswith("RigAutoRagdoll"):
        # Ragdoll V1 uses a mechanical skeleton; the physics layer is a TODO.
        return _mechanical_rig(obj)
    return _rigify_autorig(obj)

def _append_template_armature(obj, strat):
    """Inject a pre-authored armature for a template class. The authored `.blend`
    library isn't shipped yet, so this currently signals a fallback."""
    warnings.append("template armature library not shipped (" + strat + ") -> auto fallback")
    return None

def _mechanical_rig(obj):
    """Deterministic procedural skeleton: a root + a body bone spanning the mesh
    bbox, bound with automatic weights. No Rigify — robust for vehicles/doors/
    mechanical props and fully reproducible."""
    _make_active(obj)
    bpy.ops.object.mode_set(mode="OBJECT")
    arm_data = bpy.data.armatures.new(obj.name + "_arm")
    rig_obj = bpy.data.objects.new(obj.name + "_rig", arm_data)
    _collection().objects.link(rig_obj)
    rig_obj.location = obj.location
    bpy.context.view_layer.objects.active = rig_obj
    bpy.ops.object.mode_set(mode="EDIT")
    h = max(0.2, obj.dimensions.z)
    eb = arm_data.edit_bones
    root = eb.new("root")
    root.head = (0.0, 0.0, 0.0)
    root.tail = (0.0, 0.0, h * 0.5)
    body = eb.new("body")
    body.head = root.tail
    body.tail = (0.0, 0.0, h)
    body.parent = root
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    rig_obj.select_set(True)
    bpy.context.view_layer.objects.active = rig_obj
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as ex:
        warnings.append("mechanical parent failed: " + str(ex))
    operations.append("mechanical_rig")
    return rig_obj

def _rigify_autorig(obj):
    """Auto-rig the mesh with Rigify: human metarig -> generate -> parent with
    automatic weights. Returns the generated armature object, or None."""
    try:
        bpy.ops.preferences.addon_enable(module="rigify")
    except Exception as ex:
        warnings.append("rigify enable failed: " + str(ex))
        return None
    _make_active(obj)
    bpy.ops.object.mode_set(mode="OBJECT")
    try:
        bpy.ops.object.armature_human_metarig_add()
    except Exception as ex:
        warnings.append("metarig add failed: " + str(ex))
        return None
    metarig = bpy.context.active_object
    metarig.name = obj.name + "_metarig"
    # Roughly fit the metarig to the mesh: align base and match height.
    try:
        h = max(0.1, obj.dimensions.z)
        metarig.scale = (h / 2.0, h / 2.0, h / 2.0)
        metarig.location = (obj.location.x, obj.location.y, obj.location.z - obj.dimensions.z / 2.0)
        _make_active(metarig)
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    except Exception as ex:
        warnings.append("metarig fit skipped: " + str(ex))
    bpy.context.view_layer.objects.active = metarig
    try:
        bpy.ops.pose.rigify_generate()
    except Exception as ex:
        warnings.append("rigify_generate failed: " + str(ex))
        return None
    rig_obj = bpy.context.active_object
    rig_obj.name = obj.name + "_rig"
    coll = _collection()
    for ob in (metarig, rig_obj):
        for c in list(ob.users_collection):
            if c is not coll:
                c.objects.unlink(ob)
        if ob.name not in coll.objects:
            coll.objects.link(ob)
    metarig.hide_set(True)
    # Parent mesh to the generated rig with automatic weights.
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    rig_obj.select_set(True)
    bpy.context.view_layer.objects.active = rig_obj
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as ex:
        warnings.append("auto-weight parent failed: " + str(ex))
    operations.append("rigify_autorig")
    return rig_obj

def animate(rig_obj, clips):
    """Author one Action per requested clip so the FBX carries named takes.

    Placeholder locomotion: keyframes the rig object's transform (a bob + a turn)
    so each clip has real, exportable animation data. Real retargeting from a
    motion library replaces this later; the clip names + take structure are
    already what Unity's importer expects.
    """
    made = []
    if rig_obj is None:
        warnings.append("animation skipped (no rig generated)")
        return made
    if rig_obj.animation_data is None:
        rig_obj.animation_data_create()
    base_z = rig_obj.location.z
    profiles = {{"walk": (0.10, 0.0), "run": (0.20, 0.0), "idle": (0.03, 0.0),
                "attack": (0.05, 0.6), "jump": (0.40, 0.0), "die": (0.0, 1.4)}}
    for clip in clips:
        amp, rot = profiles.get(clip, (0.06, 0.0))
        action = bpy.data.actions.new(name=clip)
        rig_obj.animation_data.action = action
        for frame, dz, rz in ((1, 0.0, 0.0), (12, amp, rot), (24, 0.0, 0.0)):
            rig_obj.location.z = base_z + dz
            rig_obj.rotation_euler.z = rz
            rig_obj.keyframe_insert("location", frame=frame)
            rig_obj.keyframe_insert("rotation_euler", frame=frame)
        made.append(clip)
    rig_obj.location.z = base_z
    rig_obj.rotation_euler.z = 0.0
    operations.append("anim_retarget")
    return made

def export(objs):
    os.makedirs(OUT_DIR, exist_ok=True)
    fbx_path = os.path.join(OUT_DIR, FBX_REL)
    os.makedirs(os.path.dirname(fbx_path), exist_ok=True)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    # FBX I/O ships as a built-in add-on; enable it if the operator is missing.
    if not hasattr(bpy.ops.export_scene, "fbx"):
        try:
            bpy.ops.preferences.addon_enable(module="io_scene_fbx")
        except Exception as ex:
            warnings.append("FBX add-on unavailable: " + str(ex))
            return None
    has_anim = bool(bpy.data.actions)
    bpy.ops.export_scene.fbx(
        filepath=fbx_path, use_selection=True,
        apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
        axis_forward="-Z", axis_up="Y", bake_space_transform=True,
        add_leaf_bones=False, mesh_smooth_type="FACE",
        bake_anim=has_anim, bake_anim_use_all_actions=has_anim,
    )
    operations.append("export_fbx")
    return fbx_path.replace("\\\\", "/")

# ── run ──────────────────────────────────────────────────────────────────────
obj = None
lod_names = []
rig_obj = None
anims = []
fbx_out = None
mesh_blend_out = None
try:
    # Geometry: reuse a cached pre-rig mesh when offered, else build it fresh.
    reused = False
    if REUSE_BLEND:
        obj = load_reused(JOB.get("name", "Asset"))
        reused = obj is not None
        if not reused:
            warnings.append("geo reuse failed -> cold build")
    if not reused:
        obj = generate() if ("generate" in STEPS or REUSE_BLEND) \
              else bpy.data.objects.get(JOB.get("name"))
        if obj is None:
            raise RuntimeError("no object to operate on (generate not run / object missing)")
        # On a reuse-fallback the geometry steps were omitted from STEPS, so force
        # cleanup/unwrap so the cold build still produces a clean, cacheable mesh.
        if "cleanup" in STEPS or REUSE_BLEND:
            cleanup(obj)
        if "unwrap" in STEPS or REUSE_BLEND:
            unwrap(obj)
        # Save the reusable pre-rig mesh AFTER geometry, BEFORE rig/material.
        if SAVE_MESH and ("generate" in STEPS or REUSE_BLEND):
            mesh_blend_out = save_pre_rig_mesh(obj)
    if "material" in STEPS:
        material(obj)
    if "softbody" in STEPS:
        softbody(obj)
    if "rig" in STEPS:
        rig_obj = rig(obj)
    if "animate" in STEPS:
        anims = animate(rig_obj, JOB.get("animations", []))
    if "lod" in STEPS:
        lod_names = make_lods(obj)
    bpy.ops.object.mode_set(mode="OBJECT")
    tris = sum(len(p.vertices) - 2 for p in obj.data.polygons)
    # Geometry fingerprint: sorted rounded local vertex coords. Backend-agnostic
    # (same recipe+seed → same hash on headless or live) and the key a content-
    # addressed cache will use. Cheap; computed on the base mesh.
    import hashlib as _hl
    _coords = sorted((round(v.co.x, 5), round(v.co.y, 5), round(v.co.z, 5))
                     for v in obj.data.vertices)
    geo_hash = _hl.md5(repr(_coords).encode("utf-8")).hexdigest()
    if "export" in STEPS:
        export_objs = [obj]
        if rig_obj is not None:
            export_objs.append(rig_obj)
        export_objs += [bpy.data.objects[n] for n in lod_names if n in bpy.data.objects]
        fbx_out = export(export_objs)
    abc_out = None
    unity_packages = []
    if "export_alembic" in STEPS:
        abc_out = export_alembic([obj])
        if abc_out:
            unity_packages.append("com.unity.formats.alembic")
    result = {{
        "name": JOB.get("name", "Asset"),
        "asset_type": JOB.get("asset_type", "prop"),
        "style": JOB.get("style", "stylized"),
        "route": JOB.get("meta", {{}}).get("route", "blender"),
        "fbx": fbx_out,
        "blend": bpy.data.filepath or None,
        "alembic": abc_out,
        "textures": [],
        "lods": lod_names,
        "animations": anims,
        "rigged": rig_obj is not None,
        "seed": JOB_SEED,
        "geo_hash": geo_hash,
        "mesh_blend": mesh_blend_out,
        "rig_strategy": RIG_STRATEGY,
        "unity_packages": unity_packages,
        "tris": tris,
        "operations": operations,
        "warnings": warnings,
        "stub": False,
    }}
except Exception as ex:
    import traceback
    result = {{"name": JOB.get("name", "Asset"), "error": str(ex),
              "traceback": traceback.format_exc(),
              "operations": operations, "warnings": warnings, "stub": False,
              "asset_type": JOB.get("asset_type", "prop"),
              "style": JOB.get("style", "stylized"),
              "route": JOB.get("meta", {{}}).get("route", "blender")}}
{write_manifest_tail}'''


# Appended (only) for the headless subprocess backend: persist `result` to disk
# so `blender --background --python <script>` leaves the manifest for the manager
# to read. The live MCP backend omits this and returns `result` directly — that
# single difference is the entire delta between the two backends.
_MANIFEST_TAIL = '''
try:
    _mp = os.path.join(OUT_DIR, JOB.get("name", "Asset") + ".manifest.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(_mp, "w", encoding="utf-8") as _f:
        json.dump(result, _f, indent=2)
    print("[microgen] wrote manifest -> " + _mp)
except Exception as _e:
    print("[microgen] manifest write failed: " + str(_e))
'''
