"""Tests for src/blender/sketchfab_convert.py.

No real Blender install is required or exercised here — matches this
codebase's existing convention (see test_sketchfab_freeze.py) of never
invoking a live Blender subprocess from the unit-test suite. This only pins
the generated script's SOURCE TEXT so the fix can't silently regress; the
fix itself was verified live against a real downloaded Sketchfab archive
(see the fix's commit message).
"""

from src.blender.sketchfab_convert import _TEMPLATE


def test_texture_extraction_forces_decode_before_has_data_check():
    """Root-caused 2026-07-09: Blender's gltf importer PACKS every texture but
    does not eagerly decode pixel data, so `img.has_data` reports False for
    every image immediately after import (confirmed live: has_data=False
    despite packed=True and a valid 2048x2048 size) -- the old unconditional
    `if not img.has_data: continue` silently produced 0 extracted textures
    for every Sketchfab model ever converted. The template must force a
    decode attempt (accessing .pixels) before honoring has_data."""
    assert "img.pixels[0]" in _TEMPLATE
    # The bare, unconditional skip must be gone -- has_data may still gate
    # the SECOND check (after the forced-decode attempt), but not the first.
    assert "or not img.has_data" not in _TEMPLATE


def test_texture_extraction_still_skips_render_result():
    assert 'img.name == "Render Result"' in _TEMPLATE


def test_scale_correction_scans_all_scene_objects_not_just_meshes():
    """Root-caused 2026-07-09 (live, against a real downloaded Sketchfab
    archive): every mesh a Sketchfab glTF import produces is nested several
    levels deep under empties (Sketchfab_model -> <hash>.fbx -> RootNode ->
    <node> -> mesh) -- NO mesh object itself has parent=None. Scanning only
    `objs` (the mesh-type list) for parent=None is a silent no-op: the
    computed scale_factor gets reported in the result JSON but is never
    actually applied, and the exported FBX keeps its original (wrong) size.
    The fix must scan bpy.context.scene.objects (every type), not `objs`."""
    assert "for o in bpy.context.scene.objects:" in _TEMPLATE
    assert "if o.parent is None:" in _TEMPLATE


def test_size_table_has_vehicle_category_matching_live_correction():
    """Pins the exact target/bounds verified live: a car measuring 632.88m
    (largest dimension) corrected to 0.00711x -> 4.5m, matching target=4.5
    for "vehicle" (4.5 / 632.88 == 0.007110...)."""
    assert '"vehicle":     (4.5, 1.0,  15.0),' in _TEMPLATE
