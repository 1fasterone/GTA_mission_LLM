"""
Sketchfab glTF/GLB -> FBX conversion — headless Blender subprocess.

Deliberately does the minimum: import the artist-authored mesh, export it
straight back out as FBX, unpack embedded textures to a sibling Textures/
folder. No retopology / decimation / mesh cleanup — Sketchfab meshes are
already artist-authored (unlike the generative-mesh pipeline in job_manager.py,
which genuinely needs cleanup), and staying a pure format-shift keeps the
CC-BY-ND "derivative work" question as narrow as possible.

Invoked the same way src/blender/job_manager.py runs Blender headless: write a
generated bpy script to disk, `blender --background --factory-startup --python
<script>`, read back a JSON result the script's tail writes. Missing Blender /
timeout / nonzero exit / no importable mesh -> None, so callers
(src/sketchfab_freeze.py) fall through to the Kenney catalog cleanly — this is
also how CI (no Blender installed) degrades safely, same as BlenderJobManager's
existing stub path.

The Textures/ folder sits as a SIBLING of the FBX on purpose:
unity_entity_asset_resolver.texture_dirs_for() already walks every parent of a
ResolvedModel's source path looking for a directory literally named
"Textures" — this layout needs zero changes to that function.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from .locator import find_blender

# NOTE: doubled braces are literal Python dict/f-string braces in the emitted
# script; single braces are str.format() placeholders (same convention as
# mcp_backend.py's _TEMPLATE).
_TEMPLATE = '''
import bpy, json, os, mathutils

GLTF_IN = r"{gltf_in}"
FBX_OUT = r"{fbx_out}"
TEX_DIR = r"{tex_dir}"
CATEGORY = "{category}"

# Plausible real-world size (largest bounding-box dimension, meters) per
# catalog category. Many Sketchfab uploads are authored in the wrong unit
# (cm/mm mistaken for meters) with no way for Blender to detect this from the
# file alone -- glTF mandates 1 unit = 1 meter, so Blender faithfully
# reproduces whatever the artist's tool actually wrote. (target, min, max);
# a corrective uniform scale fires ONLY when the imported size is off by more
# than 3x the (already generous) max/min bound -- normal size variation
# within a category (a bus vs a compact car, both "vehicle") is left alone.
_SIZE_TABLE = {{
    "vehicle":     (4.5, 1.0,  15.0),
    "character":   (1.8, 0.3,  3.0),
    "prop":        (0.6, 0.05, 3.0),
    "environment": (4.0, 0.5,  30.0),
    "nature":      (3.0, 0.1,  25.0),
    "building":    (8.0, 1.5,  60.0),
    "weapon":      (0.8, 0.05, 2.5),
    "furniture":   (1.0, 0.2,  3.5),
}}
_DEFAULT_SIZE = (1.5, 0.05, 20.0)

result = {{"ok": False}}
try:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not hasattr(bpy.ops.import_scene, "gltf"):
        bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
    bpy.ops.import_scene.gltf(filepath=GLTF_IN)

    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not objs:
        raise RuntimeError("no mesh objects imported")

    # ── Bounding-box sanity scale (root-caused 2026-07-09: a Sketchfab car
    # imported at 345 x 632 x 259 METERS -- ~150x too large in every axis) ──
    bpy.context.view_layer.update()
    bb_min = mathutils.Vector((1e18, 1e18, 1e18))
    bb_max = mathutils.Vector((-1e18, -1e18, -1e18))
    for o in objs:
        for corner in o.bound_box:
            wc = o.matrix_world @ mathutils.Vector(corner)
            bb_min.x = min(bb_min.x, wc.x); bb_max.x = max(bb_max.x, wc.x)
            bb_min.y = min(bb_min.y, wc.y); bb_max.y = max(bb_max.y, wc.y)
            bb_min.z = min(bb_min.z, wc.z); bb_max.z = max(bb_max.z, wc.z)
    largest_dim = max((bb_max - bb_min).x, (bb_max - bb_min).y, (bb_max - bb_min).z)
    target, plaus_min, plaus_max = _SIZE_TABLE.get(CATEGORY, _DEFAULT_SIZE)
    scale_factor = None
    if largest_dim > 0:
        if largest_dim > plaus_max * 3:
            scale_factor = target / largest_dim
        elif largest_dim < plaus_min / 3:
            scale_factor = target / largest_dim
    if scale_factor:
        # Root-caused: Sketchfab glTF imports nest every mesh several levels
        # deep under empties (Sketchfab_model -> <hash>.fbx -> RootNode ->
        # <node> -> mesh) -- NO mesh object itself has parent=None, so scaling
        # only mesh-level roots is a silent no-op. Scale the true scene
        # root(s) instead (ANY object type, not just MESH); parenting
        # cascades the correction to every descendant automatically.
        for o in bpy.context.scene.objects:
            if o.parent is None:
                o.location = o.location * scale_factor
                o.scale = o.scale * scale_factor
        bpy.context.view_layer.update()

    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]

    os.makedirs(os.path.dirname(FBX_OUT), exist_ok=True)
    if not hasattr(bpy.ops.export_scene, "fbx"):
        bpy.ops.preferences.addon_enable(module="io_scene_fbx")
    bpy.ops.export_scene.fbx(
        filepath=FBX_OUT, use_selection=True,
        apply_unit_scale=True, apply_scale_options="FBX_SCALE_ALL",
        axis_forward="-Z", axis_up="Y", bake_space_transform=True,
        add_leaf_bones=False, mesh_smooth_type="FACE",
        bake_anim=False,
    )

    os.makedirs(TEX_DIR, exist_ok=True)
    n_tex = 0
    for img in bpy.data.images:
        if img.name == "Render Result":
            continue
        if not img.has_data:
            # Blender's gltf importer PACKS every texture but does not eagerly
            # decode pixel data -- has_data reports False until the buffer is
            # actually touched, even though img.size/packed_file already show
            # real data is present. Force the decode, then re-check; a
            # genuinely empty/corrupt image still reports False afterward and
            # is correctly skipped. (Root-caused 2026-07-09: this unconditional
            # `continue` silently produced 0 extracted textures for EVERY
            # Sketchfab model ever converted, regardless of source content.)
            try:
                _ = img.pixels[0]
            except Exception:
                pass
        if not img.has_data:
            continue
        base = bpy.path.basename(img.filepath) or img.name
        # We always write PNG data (file_format below) — force the extension to
        # match, whatever the source file was called. A texture written with a
        # stray .fbx/.bin name makes Unity try (and fail) to import it as a model.
        base = os.path.splitext(base)[0] + ".png"
        dest = os.path.join(TEX_DIR, base)
        try:
            img.filepath_raw = dest
            img.file_format = "PNG"
            img.save()
            n_tex += 1
        except Exception:
            pass

    # Post-correction bounding-box dims (meters, Blender axis order x/y/z with
    # z = up) — diagnostics persisted into the Sketchfab catalog entry so
    # mis-scaled models can be spotted by grep/tests without re-opening Blender.
    _f = scale_factor or 1.0
    size_after = [round((bb_max - bb_min).x * _f, 4),
                  round((bb_max - bb_min).y * _f, 4),
                  round((bb_max - bb_min).z * _f, 4)]
    result = {{"ok": True, "fbx": FBX_OUT, "textures": n_tex,
              "scale_corrected": scale_factor, "largest_dim_before": largest_dim,
              "size_after": size_after}}
except Exception as exc:
    result = {{"ok": False, "error": str(exc)}}

with open(r"{result_path}", "w", encoding="utf-8") as f:
    json.dump(result, f)
'''


def convert_gltf_to_fbx(gltf_path: Path, fbx_out: Path, textures_dir: Path, *,
                        blender_cfg: Optional[dict] = None,
                        timeout: float = 300,
                        category: str = "prop") -> Optional[Path]:
    """Convert a Sketchfab-downloaded glTF/GLB to FBX + a sibling Textures/
    folder, via headless Blender. Returns fbx_out on success, None on any
    failure (Blender missing, timeout, nonzero exit, no importable mesh) —
    callers must treat None as a clean miss, not an error.

    `category` (the same catalog category the pick is filed under) drives a
    bounding-box sanity check: many Sketchfab uploads are authored in the
    wrong unit (cm/mm mistaken for meters), and a uniform corrective scale is
    applied to the mesh BEFORE export when the imported size is grossly
    implausible for its category (see _SIZE_TABLE in _TEMPLATE) — the
    exported FBX is then already correctly sized, no changes needed anywhere
    else in the resolve/copy/import pipeline.
    """
    cfg = blender_cfg or {}
    inst = find_blender(cfg.get("blender_path"))
    if inst is None:
        return None

    fbx_out = Path(fbx_out)
    textures_dir = Path(textures_dir)
    fbx_out.parent.mkdir(parents=True, exist_ok=True)
    result_path = fbx_out.with_suffix(".convert.json")
    script_path = fbx_out.with_suffix(".convert.py")
    if result_path.exists():
        result_path.unlink()

    script_path.write_text(_TEMPLATE.format(
        gltf_in=str(Path(gltf_path).resolve()).replace("\\", "/"),
        fbx_out=str(fbx_out.resolve()).replace("\\", "/"),
        tex_dir=str(textures_dir.resolve()).replace("\\", "/"),
        result_path=str(result_path.resolve()).replace("\\", "/"),
        category=(category or "prop").replace('"', ""),
    ), encoding="utf-8")

    cmd = [str(inst.path), "--background", "--factory-startup",
           "--python", str(script_path)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None

    if not result_path.exists():
        return None
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("ok") or not fbx_out.is_file():
        return None

    # NOTE: no normalize_fbx() here — rewriting the CreationTime invalidates
    # the FBX footer/FileId cross-check and Unity's FBX SDK rejects the file
    # (root-caused 2026-07-09). Blender's output is already deterministic.
    return fbx_out
