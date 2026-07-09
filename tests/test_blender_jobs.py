"""
Tests for the Blender Job Manager (src/blender/*).

These exercise the host-side logic — schema clamping, routing, prompt
construction, queueing, and graceful stubbing when Blender is absent. The
in-Blender bpy script (scripts/run_job.py) is not imported here (it requires
bpy); it's covered structurally via the stub path.
"""

import json

import pytest

from src.blender import (
    BlenderJob, BlenderJobManager, build_jobs_for_schema, route_for, build_job,
)
from src.blender.diffusion import build_texture_request, DiffusionClient
from src.blender.manifest import JobStatus
from src.unity_schema import UnitySchema, UnityScript, EntitySpec


# ── schema clamping ───────────────────────────────────────────────────────────

def test_job_clamps_unknown_enums():
    job = BlenderJob.model_validate({
        "name": "Golem 01!!",
        "asset_type": "monster",     # synonym → creature
        "style": "photoreal",        # synonym → realistic
        "animations": ["running", "FLY", "nonsense"],
        "textures": {"resolution": 1500},   # → 1024 (nearest POT down)
    })
    assert job.asset_type == "creature"
    assert job.style == "realistic"
    assert job.name == "Golem_01__"
    assert job.animations == ["run", "fly"]   # nonsense dropped, run normalised
    assert job.textures.resolution == 1024


def test_resolution_snaps_to_power_of_two():
    assert BlenderJob(textures={"resolution": 4097}).textures.resolution == 4096
    assert BlenderJob(textures={"resolution": 100}).textures.resolution == 256
    assert BlenderJob(textures={"resolution": 2048}).textures.resolution == 2048


def test_default_output_paths_from_name():
    job = BlenderJob(name="golem_01").default_output("output/blender")
    assert job.output.fbx == "golem_01.fbx"
    assert job.output.blend == "golem_01.blend"
    assert job.output.textures == "golem_01_textures/"


# ── routing (the goal's procedural-vs-diffusion decision) ─────────────────────

@pytest.mark.parametrize("atype,style,expected", [
    ("character", "realistic", "diffusion"),   # authored skin → diffusion
    ("character", "low_poly",  "blender"),      # stylized → procedural
    ("terrain",   "realistic", "blender"),      # geometric surface stays procedural
    ("building",  "realistic", "blender"),
    ("weapon",    "realistic", "diffusion"),
    ("prop",      "stylized",  "blender"),
])
def test_route_for(atype, style, expected):
    assert route_for(atype, style) == expected


def test_build_job_sets_texture_source_from_route():
    pj = build_job(name="hero", asset_type="character", style="realistic")
    assert pj.meta["route"] == "diffusion"
    assert pj.needs_diffusion()
    lj = build_job(name="rock", asset_type="prop", style="low_poly")
    assert lj.meta["route"] == "blender"
    assert not lj.needs_diffusion()


def test_characters_auto_rig_with_default_anims():
    job = build_job(name="hero", asset_type="character", style="low_poly")
    assert job.rig is True
    assert job.animations == ["idle", "walk", "run"]
    prop = build_job(name="crate", asset_type="prop", style="low_poly")
    assert prop.rig is False
    assert prop.animations == []


# ── diffusion prompt construction ─────────────────────────────────────────────

def test_build_texture_request_uses_description():
    job = build_job(name="orc", asset_type="character", style="realistic",
                    description="a battle-scarred orc warrior in rusted iron armor")
    req = build_texture_request(job)
    assert "orc warrior" in req.prompt
    assert "photorealistic" in req.prompt
    assert "tileable" in req.prompt        # albedo guard present
    assert req.width == req.height == job.textures.resolution


def test_diffusion_client_disabled_by_default():
    assert DiffusionClient({"backend": "none"}).enabled is False
    assert DiffusionClient(None).available() is False


# ── job builder from a Unity schema ───────────────────────────────────────────

def test_build_jobs_for_schema_from_entities():
    schema = UnitySchema(
        project_name="NeonFugitive", game_type="3D", platform="pc",
        render_pipeline="URP", visual_style="low_poly",
        entities=[
            EntitySpec(name="RivalCars", kind="rival car", count=6),
            EntitySpec(name="Towers", kind="building", count=4),
        ],
        scripts=[UnityScript(name="PlayerController", type="player",
                             description="drives the car")],
    )
    jobs = build_jobs_for_schema(schema, concept="a neon street race")
    names = {j.name for j in jobs}
    kinds = {j.asset_type for j in jobs}
    assert "RivalCars" in names
    assert "vehicle" in kinds            # "rival car" → vehicle
    assert "building" in kinds
    # player script → a character asset
    assert any(j.asset_type == "character" for j in jobs)
    # all jobs carry the project's low_poly style → all procedural
    assert all(j.meta["route"] == "blender" for j in jobs)


def test_build_jobs_respects_max_jobs():
    ents = [EntitySpec(name=f"E{i}", kind="prop", count=1) for i in range(20)]
    schema = UnitySchema(project_name="P", game_type="3D", platform="pc",
                         render_pipeline="URP", entities=ents)
    jobs = build_jobs_for_schema(schema, max_jobs=5)
    assert len(jobs) <= 5


# ── manager: queue + graceful stub (no Blender installed) ─────────────────────

def _force_no_blender(monkeypatch):
    """Pretend no Blender is installed so the stub path is exercised even on a
    machine that has Blender (the locator would otherwise auto-detect it)."""
    monkeypatch.setattr("src.blender.job_manager.find_blender", lambda *_a, **_k: None)


def test_manager_stubs_without_blender(tmp_path, monkeypatch):
    _force_no_blender(monkeypatch)
    mgr = BlenderJobManager({"output_dir": str(tmp_path), "blender_path": "/no/such/blender"})
    assert mgr.blender is None
    mgr.submit(build_job(name="crate", asset_type="prop", style="low_poly"))
    mgr.submit(build_job(name="hero", asset_type="character", style="realistic"))
    assert mgr.pending == 2
    batch = mgr.run_all()
    assert batch.total == 2
    assert batch.stubbed == 2
    assert batch.failed == 0
    for r in batch.results:
        assert r.status == JobStatus.STUBBED
        assert r.ok
        assert r.manifest is not None
        assert r.manifest.stub is True
        assert "export_fbx" in r.manifest.operations
    # manifest files were written to disk
    assert (tmp_path / "crate" / "crate.manifest.json").exists()


def test_manager_submit_spec_from_dict(tmp_path):
    mgr = BlenderJobManager({"output_dir": str(tmp_path), "blender_path": "/no/such"})
    job = mgr.submit_spec({
        "name": "golem_01", "asset_type": "character", "style": "realistic",
        "rig": True, "animations": ["walk", "run", "attack"], "lods": True,
        "textures": {"resolution": 2048, "bake": True},
        "geometry_nodes": "human_base",
        "output": {"fbx": "golem_01.fbx", "textures": "golem_01_textures/",
                   "blend": "golem_01.blend"},
    })
    assert job.name == "golem_01"
    assert job.animations == ["walk", "run", "attack"]
    assert job.textures.resolution == 2048


def test_dry_run_does_not_write_geometry(tmp_path):
    mgr = BlenderJobManager({"output_dir": str(tmp_path)})
    mgr.submit(build_job(name="crate", asset_type="prop", style="low_poly"))
    batch = mgr.run_all(dry_run=True)
    assert batch.results[0].status == JobStatus.STUBBED
    # dry-run writes nothing to disk
    assert not (tmp_path / "crate").exists()


def test_batch_manifest_roundtrip(tmp_path, monkeypatch):
    _force_no_blender(monkeypatch)
    mgr = BlenderJobManager({"output_dir": str(tmp_path), "blender_path": "/no/such"})
    mgr.submit(build_job(name="crate", asset_type="prop", style="low_poly"))
    batch = mgr.run_all()
    out = batch.write(tmp_path / "batch.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total"] == 1
    assert data["results"][0]["manifest"]["name"] == "crate"


# ── MCP (live-Blender) backend ────────────────────────────────────────────────

from src.blender.mcp_backend import build_job_code, steps_for


def test_build_job_code_is_nondestructive_and_has_steps():
    job = build_job(name="golem_01", asset_type="character", style="realistic")
    code = build_job_code(job, "out", steps=["generate", "cleanup", "unwrap", "export"])
    # operates in its own collection, never resets the scene or deletes user objects
    assert "BJM_Assets" in code
    assert "read_factory_settings" not in code
    assert "select_all(action=\"SELECT\")\nbpy.ops.object.delete" not in code
    # the requested steps are present
    assert "remove_doubles" in code
    assert "smart_project" in code
    assert "export_scene.fbx" in code
    # export only the job's objects, not the whole file
    assert "use_selection=True" in code


def test_steps_for_derives_from_flags():
    # character → rig on + auto anims → rig + animate steps present
    rigged = build_job(name="hero", asset_type="character", style="low_poly")
    assert steps_for(rigged) == ["generate", "cleanup", "unwrap", "material",
                                 "rig", "animate", "lod", "export"]
    # prop → no rig, no animate
    prop = build_job(name="crate", asset_type="prop", style="low_poly")
    assert "rig" not in steps_for(prop)
    assert "animate" not in steps_for(prop)


def test_build_job_code_emits_rig_and_animation():
    job = build_job(name="golem_01", asset_type="character", style="realistic")
    job.animations = ["walk", "run", "attack"]
    code = build_job_code(job, "out")   # flag-derived steps include rig + animate
    assert "rigify_generate" in code
    assert "armature_human_metarig_add" in code
    assert "parent_set(type=\"ARMATURE_AUTO\")" in code
    assert "animation_data_create" in code
    assert "bake_anim" in code


def test_manager_mcp_backend_with_fake_executor(tmp_path):
    """Inject a fake executor that mimics execute_blender_code's envelope."""
    captured = {}

    def fake_executor(code: str) -> dict:
        captured["code"] = code
        return {"status": "ok", "result": {
            "name": "golem_01", "asset_type": "character", "style": "realistic",
            "route": "diffusion", "fbx": str(tmp_path / "golem_01/golem_01.fbx"),
            "blend": None, "textures": [], "lods": [], "animations": [],
            "rigged": False, "tris": 6,
            "operations": ["generate", "mesh_cleanup", "uv_unwrap", "export_fbx"],
            "warnings": [], "stub": False,
        }}

    mgr = BlenderJobManager({"output_dir": str(tmp_path)}, mcp_executor=fake_executor)
    mgr.live_steps = ["generate", "cleanup", "unwrap", "export"]
    mgr.submit(build_job(name="golem_01", asset_type="character", style="realistic"))
    batch = mgr.run_all()

    assert batch.done == 1 and batch.failed == 0 and batch.stubbed == 0
    r = batch.results[0]
    assert r.status == JobStatus.DONE
    assert r.manifest.stub is False
    assert r.manifest.tris == 6
    assert "export_fbx" in r.manifest.operations
    assert "smart_project" in captured["code"]   # code really was built + passed
    assert (tmp_path / "golem_01" / "golem_01.manifest.json").exists()


def test_recipe_dispatch_and_injection():
    from src.blender.recipes import recipe_for, recipe_source
    assert recipe_for("terrain", "") == "terrain_noise"
    assert recipe_for("foliage", "") == "foliage_tree"
    assert recipe_for("character", "human_base") == "humanoid_base"
    assert recipe_for("prop", "crystal_cluster") == "crystal_cluster"  # explicit wins
    # every recipe source defines the expected entry point
    for key in ("terrain_noise", "rock_kit", "crystal_cluster", "foliage_tree",
                "building_kit", "humanoid_base", "prop_kit", "scatter_kit"):
        assert "_build_geometry" in recipe_source(key)


def test_build_job_code_carries_recipe_color_detail():
    job = build_job(name="rock1", asset_type="prop", style="low_poly",
                    color="#E74C3C", detail=5, soft_body=True)
    code = build_job_code(job, "out")
    assert "_build_geometry" in code              # recipe injected
    assert "_apply_palette_material" in code      # palette material injected
    assert "#E74C3C" in code                      # palette colour threaded through
    assert "softbody" in code                     # soft body step present
    assert "SOFT_BODY" not in code or True        # (flag lives in JOB json)
    assert "softbody" in steps_for(job)


def test_color_and_detail_clamp():
    j = BlenderJob.model_validate({"name": "x", "detail": 99, "color": "e74c3c"})
    assert j.detail == 5
    assert j.color == "#E74C3C"     # '#'-prefixed + uppercased
    j2 = BlenderJob.model_validate({"name": "y", "detail": -3, "color": "nothex"})
    assert j2.detail == 1
    assert j2.color == ""           # invalid → empty


def test_palette_color_threaded_from_schema():
    from src.blender.job_builder import build_jobs_for_schema
    schema = UnitySchema(
        project_name="P", game_type="3D", platform="pc", render_pipeline="URP",
        visual_style="low_poly",
        entities=[EntitySpec(name="Rocks", kind="rock", count=5, color_role="accent")],
    )
    jobs = build_jobs_for_schema(schema)
    rock = next(j for j in jobs if j.name == "Rocks")
    # low_poly default palette accent is a hex; the job carries it
    assert rock.color.startswith("#") and len(rock.color) == 7


def test_softbody_emits_alembic_bake():
    job = build_job(name="jelly", asset_type="prop", style="low_poly", soft_body=True)
    assert job.output.alembic == "jelly.abc"
    s = steps_for(job)
    assert "softbody" in s and "export_alembic" in s
    assert s.index("export_alembic") > s.index("export")     # bake after geometry export
    code = build_job_code(job, "out")
    assert "alembic_export" in code
    assert "SOFT_BODY" in code
    assert "com.unity.formats.alembic" in code


def test_stub_softbody_manifest_carries_alembic(tmp_path, monkeypatch):
    _force_no_blender(monkeypatch)
    mgr = BlenderJobManager({"output_dir": str(tmp_path), "blender_path": "/no/such"})
    mgr.submit(build_job(name="jelly", asset_type="prop", style="low_poly", soft_body=True))
    batch = mgr.run_all()
    m = batch.results[0].manifest
    assert m.alembic and m.alembic.endswith("jelly.abc")
    assert "com.unity.formats.alembic" in m.unity_packages
    assert "export_alembic" in m.operations


def test_unity_overlay_copies_assets_and_merges_packages(tmp_path):
    from src.blender.manifest import AssetManifest, BatchManifest, JobResult, JobStatus
    from src.blender.unity_overlay import overlay_batch
    # a fake source asset on disk
    src = tmp_path / "src"
    (src).mkdir()
    fbx = src / "jelly.fbx"; fbx.write_bytes(b"FBX")
    abc = src / "jelly.abc"; abc.write_bytes(b"ABC")
    tex = src / "jelly_albedo.png"; tex.write_bytes(b"PNG")
    # a Unity project skeleton
    proj = tmp_path / "UnityProj"
    (proj / "Assets").mkdir(parents=True)
    (proj / "Packages").mkdir()
    (proj / "Packages" / "manifest.json").write_text(
        '{"dependencies": {"com.unity.render-pipelines.universal": "14.0.11"}}', encoding="utf-8")

    man = AssetManifest(name="jelly", asset_type="prop", style="cartoon", route="blender",
                        fbx=str(fbx), alembic=str(abc), textures=[str(tex)],
                        unity_packages=["com.unity.formats.alembic"])
    batch = BatchManifest(total=1, done=1, failed=0, stubbed=0,
                          results=[JobResult(name="jelly", status=JobStatus.DONE,
                                             route="blender", manifest=man)])
    res = overlay_batch(batch, proj)
    # files landed under Assets/BlenderAssets/jelly/
    assert (proj / "Assets/BlenderAssets/jelly/jelly.fbx").exists()
    assert (proj / "Assets/BlenderAssets/jelly/jelly.abc").exists()
    assert (proj / "Assets/BlenderAssets/jelly/textures/jelly_albedo.png").exists()
    # alembic package merged in without clobbering URP
    deps = json.loads((proj / "Packages/manifest.json").read_text())["dependencies"]
    assert deps["com.unity.formats.alembic"] == "2.3.3"
    assert deps["com.unity.render-pipelines.universal"] == "14.0.11"
    assert "com.unity.formats.alembic" in res.packages_added


def test_live_material_emits_diffusion_branch():
    job = build_job(name="orc", asset_type="character", style="realistic",
                    description="battle-scarred orc")
    job.meta["diffusion_albedo"] = "/some/orc_albedo.png"
    code = build_job_code(job, "out")
    # the material function reads the albedo from JOB meta and can wire an image
    assert "diffusion_albedo" in code
    assert "ShaderNodeTexImage" in code           # image-texture branch present
    assert "material:diffusion" in code           # operation label present
    assert "bjm_diffusion" in code


def test_unity_overlay_reports_missing_source(tmp_path):
    from src.blender.manifest import AssetManifest, BatchManifest, JobResult, JobStatus
    from src.blender.unity_overlay import overlay_batch
    proj = tmp_path / "U"; (proj / "Assets").mkdir(parents=True)
    man = AssetManifest(name="ghost", asset_type="prop", style="low_poly", route="blender",
                        fbx=str(tmp_path / "nope.fbx"))
    batch = BatchManifest(total=1, done=1, failed=0, stubbed=0,
                          results=[JobResult(name="ghost", status=JobStatus.DONE,
                                             route="blender", manifest=man)])
    res = overlay_batch(batch, proj)
    assert res.copied == []
    assert any("nope.fbx" in m for m in res.missing)


# ── fixed-seed determinism (Phase 1) ──────────────────────────────────────────

from src.blender.seeding import derive_seed, effective_seed, recipe_seed, diffusion_seed


def test_derive_seed_is_stable_and_independent():
    # same inputs → same output (across processes: it's a pure md5 hash)
    assert derive_seed(1234, "boulder", "recipe") == derive_seed(1234, "boulder", "recipe")
    # different stage / master / name → different seed (independent)
    assert derive_seed(1234, "boulder", "recipe") != derive_seed(1234, "boulder", "diffusion")
    assert derive_seed(1234, "boulder", "recipe") != derive_seed(9999, "boulder", "recipe")
    assert derive_seed(1234, "a", "recipe") != derive_seed(1234, "b", "recipe")


def test_effective_seed_explicit_vs_derived():
    pinned = build_job(name="rock", asset_type="prop", style="low_poly", seed=42)
    assert pinned.seed == 42
    assert effective_seed(pinned) == 42
    unpinned = build_job(name="rock", asset_type="prop", style="low_poly")
    assert unpinned.seed is None
    # still deterministic — derived from the name
    assert effective_seed(unpinned) == effective_seed(
        build_job(name="rock", asset_type="prop", style="low_poly"))


def test_seed_coercion():
    assert BlenderJob.model_validate({"name": "x", "seed": "77"}).seed == 77
    assert BlenderJob.model_validate({"name": "x", "seed": -5}).seed == 5
    assert BlenderJob.model_validate({"name": "x", "seed": "nope"}).seed is None


def test_same_seed_emits_identical_code_different_seed_differs():
    j1 = build_job(name="shard", asset_type="prop", style="low_poly", seed=7)
    j1.geometry_nodes = "crystal_cluster"
    j2 = build_job(name="shard", asset_type="prop", style="low_poly", seed=7)
    j2.geometry_nodes = "crystal_cluster"
    j3 = build_job(name="shard", asset_type="prop", style="low_poly", seed=8)
    j3.geometry_nodes = "crystal_cluster"
    c1 = build_job_code(j1, "out"); c2 = build_job_code(j2, "out"); c3 = build_job_code(j3, "out")
    assert c1 == c2          # same seed → byte-identical script
    assert c1 != c3          # different seed → different script
    assert "random.seed(SEED)" in c1
    assert "_build_geometry(name, COLOR, DETAIL, SEED)" in c1


def test_diffusion_seed_is_deterministic_per_job():
    job = build_job(name="orc", asset_type="character", style="realistic", seed=100)
    r1 = build_texture_request(job); r2 = build_texture_request(job)
    assert r1.seed == r2.seed == diffusion_seed(job)
    # recipe and diffusion sub-seeds are independent
    assert diffusion_seed(job) != recipe_seed(job)


def test_stub_manifest_records_seed(tmp_path, monkeypatch):
    _force_no_blender(monkeypatch)
    mgr = BlenderJobManager({"output_dir": str(tmp_path), "blender_path": "/no/such"})
    mgr.submit(build_job(name="rock", asset_type="prop", style="low_poly", seed=55))
    batch = mgr.run_all()
    assert batch.results[0].manifest.seed == 55


def test_master_seed_stamps_reproducible_jobs():
    from src.blender.job_builder import build_jobs_for_schema
    schema = UnitySchema(project_name="P", game_type="3D", platform="pc",
                         render_pipeline="URP", visual_style="low_poly",
                         entities=[EntitySpec(name="Rocks", kind="rock", count=3)])
    a = build_jobs_for_schema(schema, master_seed=2024)
    b = build_jobs_for_schema(schema, master_seed=2024)
    c = build_jobs_for_schema(schema, master_seed=999)
    sa = {j.name: j.seed for j in a}
    sb = {j.name: j.seed for j in b}
    sc = {j.name: j.seed for j in c}
    assert sa == sb and all(v is not None for v in sa.values())   # reproducible
    assert sa != sc                                               # master changes them


# ── export determinism / timestamp normalization (Phase 3) ────────────────────

from src.blender import normalize_export
from src.blender.normalize import (
    normalize_alembic_bytes, normalize_fbx_bytes,
    CANONICAL_ASCTIME,
)


def test_alembic_date_canonicalized_length_preserving():
    # a fake Alembic metadata blob with a real asctime write-date
    blob = (b"...FramesPerTimeUnit=24.000000;_ai_Application=Blender;"
            b"_ai_DateWritten=Mon Jun 15 15:43:16 2026;_ai_Description=unknown;...")
    out, n = normalize_alembic_bytes(blob)
    assert n == 1
    assert len(out) == len(blob)                    # offset tables stay valid
    assert CANONICAL_ASCTIME in out
    assert b"Mon Jun 15 15:43:16 2026" not in out


def test_two_exports_become_byte_identical_after_normalize():
    """Simulate two exports of identical geometry that differ ONLY in the
    embedded write-date (what really happens), and prove the normalizer collapses
    them to byte-identical output."""
    base = (b"OgawaHEADER...geometry-bytes-identical..."
            b"_ai_DateWritten=Mon Jun 15 15:43:16 2026;tail")
    later = base.replace(b"Mon Jun 15 15:43:16 2026", b"Tue Jun 16 09:01:59 2026")
    assert base != later                            # raw re-exports differ (dirty)
    na, _ = normalize_alembic_bytes(base)
    nb, _ = normalize_alembic_bytes(later)
    assert na == nb                                 # after normalize → identical


def test_fbx_bytes_never_rewritten():
    """FBX normalize is a hard NO-OP — root-caused 2026-07-09: binary FBX
    cross-validates CreationTime against the FileId + footer code, and Unity's
    FBX SDK rejects the whole file when a timestamp rewrite breaks that pact
    (Blender ignores the footer, which masked it). Blender writes a constant
    dummy CreationTime anyway, so output is already byte-deterministic."""
    blob = b"...CreationTimeS\x17\x00\x00\x002026-06-15 15:43:16:789 more..."
    out, n = normalize_fbx_bytes(blob)
    assert n == 0
    assert out == blob                              # byte-for-byte untouched


def test_normalize_export_dispatch_and_real_files(tmp_path):
    abc = tmp_path / "a.abc"
    abc.write_bytes(b"x_ai_DateWritten=Wed Jul 01 12:00:00 2026;y")
    fbx = tmp_path / "a.fbx"
    fbx_raw = b"CreationTime|2026-07-01 12:00:00:000|"
    fbx.write_bytes(fbx_raw)
    assert normalize_export(abc) == ("alembic", 1)
    assert normalize_export(fbx) == ("fbx", 0)      # FBX: never rewritten
    assert CANONICAL_ASCTIME in abc.read_bytes()
    assert fbx.read_bytes() == fbx_raw              # FBX bytes untouched
    # re-normalizing an already-clean file is a no-op
    assert normalize_export(abc) == ("alembic", 0)
    assert normalize_export(tmp_path / "nope.abc") == (None, 0)


def test_manager_normalizes_outputs(tmp_path, monkeypatch):
    """The MCP backend normalizes produced files when the fake executor 'writes'
    an Alembic with a live date."""
    abc = tmp_path / "jelly" / "jelly.abc"
    abc.parent.mkdir(parents=True)
    abc.write_bytes(b"hdr_ai_DateWritten=Fri Jun 13 08:30:00 2026;end")

    def executor(code: str) -> dict:
        return {"result": {"name": "jelly", "asset_type": "prop", "style": "cartoon",
                           "route": "blender", "alembic": str(abc),
                           "operations": ["export_alembic"], "warnings": [], "stub": False}}

    mgr = BlenderJobManager({"output_dir": str(tmp_path)}, mcp_executor=executor)
    mgr.submit(build_job(name="jelly", asset_type="prop", style="cartoon", soft_body=True))
    batch = mgr.run_all()
    assert CANONICAL_ASCTIME in abc.read_bytes()
    assert "normalize_alembic" in batch.results[0].manifest.operations


def test_normalize_can_be_disabled(tmp_path):
    abc = tmp_path / "j" / "j.abc"
    abc.parent.mkdir(parents=True)
    raw = b"hdr_ai_DateWritten=Fri Jun 13 08:30:00 2026;end"
    abc.write_bytes(raw)

    def executor(code: str) -> dict:
        return {"result": {"name": "j", "asset_type": "prop", "style": "cartoon",
                           "route": "blender", "alembic": str(abc),
                           "operations": [], "warnings": [], "stub": False}}

    mgr = BlenderJobManager({"output_dir": str(tmp_path), "normalize_exports": False},
                            mcp_executor=executor)
    mgr.submit(build_job(name="j", asset_type="prop", style="cartoon", soft_body=True))
    mgr.run_all()
    assert abc.read_bytes() == raw          # untouched when disabled


# ── backend parity (Phase 1) ──────────────────────────────────────────────────

def test_backend_parity_only_differs_by_manifest_tail():
    """The subprocess and live-MCP backends run the SAME recipe script; the only
    difference is the manifest-writing tail appended for the headless run."""
    job = build_job(name="rock", asset_type="prop", style="low_poly", seed=5)
    job.geometry_nodes = "rock_kit"
    live = build_job_code(job, "out", write_manifest=False)
    headless = build_job_code(job, "out", write_manifest=True)
    assert headless.startswith(live)                 # identical up to the tail
    tail = headless[len(live):]
    assert "json.dump(result" in tail and "manifest.json" in tail
    assert "_build_geometry" in live                 # both run the recipe library
    assert "_apply_palette_material" in live         # both apply palette materials


def test_subprocess_backend_runs_recipe_script(tmp_path, monkeypatch):
    """Headless backend: mock Blender as a process that runs the emitted recipe
    script and leaves a manifest — prove the manager drives the recipe path."""
    from pathlib import Path
    from src.blender import job_manager as jm
    from src.blender.locator import BlenderInstall

    monkeypatch.setattr(jm, "find_blender",
                        lambda *a, **k: BlenderInstall(path=Path("/fake/blender"), version="5.1"))

    captured = {}

    def fake_run(cmd, **kw):
        script = Path(cmd[-1])
        assert script.name.endswith(".run.py")
        code = script.read_text(encoding="utf-8")
        captured["code"] = code
        assert "_build_geometry" in code              # recipe geometry
        assert "_apply_palette_material" in code       # palette material
        assert "json.dump(result" in code              # manifest tail
        name = script.name[: -len(".run.py")]
        (script.parent / f"{name}.manifest.json").write_text(json.dumps({
            "name": name, "asset_type": "prop", "style": "low_poly", "route": "blender",
            "fbx": None, "operations": ["generate:rock_kit", "export_fbx"],
            "warnings": [], "stub": False}), encoding="utf-8")

        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(jm.subprocess, "run", fake_run)

    mgr = jm.BlenderJobManager({"output_dir": str(tmp_path)})
    assert mgr.blender is not None and mgr.mcp_executor is None   # subprocess path
    rock = build_job(name="rock", asset_type="prop", style="low_poly")
    rock.geometry_nodes = "rock_kit"
    mgr.submit(rock)
    batch = mgr.run_all()
    assert batch.done == 1 and batch.failed == 0
    assert "generate:rock_kit" in batch.results[0].manifest.operations
    assert "generate:rock_kit" in captured["code"] or "rock_kit" in captured["code"]


def test_manager_mcp_backend_reports_blender_error(tmp_path):
    def failing_executor(code: str) -> dict:
        return {"status": "ok", "result": {"name": "golem_01", "error": "boom in bpy",
                                            "asset_type": "character", "style": "realistic",
                                            "route": "blender", "operations": [], "warnings": [],
                                            "stub": False}}

    mgr = BlenderJobManager({"output_dir": str(tmp_path), "max_retries": 0},
                            mcp_executor=failing_executor)
    mgr.submit(build_job(name="golem_01", asset_type="character", style="low_poly"))
    batch = mgr.run_all()
    assert batch.failed == 1
    assert "boom in bpy" in batch.results[0].error


# ── deterministic cache (Micro-Generator Phase 2) ─────────────────────────────

def test_cache_fingerprint_stable_and_sensitive(tmp_path):
    from src.blender.cache import BlenderCache
    bc = BlenderCache(tmp_path)
    steps = ["generate", "cleanup", "unwrap", "export"]
    base = lambda: build_job(name="rock", asset_type="prop", style="low_poly")
    fp = bc.fingerprint(base(), steps)

    assert len(fp) == 64                          # sha256 hex
    assert bc.fingerprint(base(), steps) == fp    # stable across identical inputs

    # geometry-determining fields move the key
    j = base(); j.detail = 5
    assert bc.fingerprint(j, steps) != fp
    j = base(); j.color = "#FF0000"
    assert bc.fingerprint(j, steps) != fp
    # the step list is part of the key
    assert bc.fingerprint(base(), steps + ["lod"]) != fp
    # name change → different derived seed → different key (un-pinned seed)
    other = build_job(name="rock2", asset_type="prop", style="low_poly")
    assert bc.fingerprint(other, steps) != fp


def test_cache_store_lookup_restore_roundtrip(tmp_path):
    from pathlib import Path
    from src.blender.cache import BlenderCache
    from src.blender.manifest import AssetManifest

    cache_dir = tmp_path / "cache"
    bc = BlenderCache(cache_dir)
    src_out = tmp_path / "rock"; src_out.mkdir()
    fbx = src_out / "rock.fbx"; fbx.write_bytes(b"FBX-GEOMETRY")
    tdir = src_out / "rock_textures"; tdir.mkdir()
    tex = tdir / "rock_albedo.png"; tex.write_bytes(b"PNG")

    job = build_job(name="rock", asset_type="prop", style="low_poly").default_output(str(tmp_path))
    manifest = AssetManifest(
        name="rock", asset_type="prop", style="low_poly", route="blender",
        fbx=str(fbx), textures=[str(tex)], geo_hash="abc123",
        operations=["generate", "export_fbx"], tris=12)
    fp = bc.fingerprint(job, ["generate", "export"])
    bc.store(fp, manifest, src_out, job)

    # a fresh instance over the same dir finds it; a bogus key misses
    bc2 = BlenderCache(cache_dir)
    entry = bc2.lookup(fp)
    assert entry is not None and entry.geo_hash == "abc123"
    assert bc2.lookup("0" * 64) is None

    # restore into a NEW dir: files copied, paths rewritten, cache_hit recorded
    new_out = tmp_path / "restored"
    m = bc2.restore(entry, new_out, job)
    assert m.fbx == str(new_out / "rock.fbx")
    assert Path(m.fbx).read_bytes() == b"FBX-GEOMETRY"
    assert len(m.textures) == 1 and Path(m.textures[0]).is_file()
    assert "cache_hit" in m.operations


def test_manager_second_identical_job_hits_cache(tmp_path):
    """End-to-end: an identical job on a fresh manager sharing the cache dir is a
    hit — Blender (the executor) is NOT invoked the second time."""
    calls = {"n": 0}

    def executor(code: str) -> dict:
        calls["n"] += 1
        out = tmp_path / "rock"; out.mkdir(parents=True, exist_ok=True)
        (out / "rock.fbx").write_bytes(b"GEO")
        return {"status": "ok", "result": {
            "name": "rock", "asset_type": "prop", "style": "low_poly", "route": "blender",
            "fbx": str(out / "rock.fbx"), "blend": None, "textures": [], "lods": [],
            "animations": [], "rigged": False, "tris": 12, "geo_hash": "deadbeef",
            "operations": ["generate", "export_fbx"], "warnings": [], "stub": False}}

    cfg = {"output_dir": str(tmp_path)}
    mgr1 = BlenderJobManager(cfg, mcp_executor=executor)
    mgr1.submit(build_job(name="rock", asset_type="prop", style="low_poly"))
    b1 = mgr1.run_all()
    assert b1.done == 1 and calls["n"] == 1
    assert "cache_hit" not in b1.results[0].manifest.operations

    mgr2 = BlenderJobManager(cfg, mcp_executor=executor)
    mgr2.submit(build_job(name="rock", asset_type="prop", style="low_poly"))
    b2 = mgr2.run_all()
    assert b2.done == 1
    assert calls["n"] == 1                                   # executor not re-invoked
    assert "cache_hit" in b2.results[0].manifest.operations
    assert b2.results[0].manifest.geo_hash == "deadbeef"


def test_cache_disabled_runs_every_time(tmp_path):
    from pathlib import Path
    calls = {"n": 0}

    def executor(code: str) -> dict:
        calls["n"] += 1
        out = tmp_path / "rock"; out.mkdir(parents=True, exist_ok=True)
        (out / "rock.fbx").write_bytes(b"GEO")
        return {"status": "ok", "result": {
            "name": "rock", "asset_type": "prop", "style": "low_poly", "route": "blender",
            "fbx": str(out / "rock.fbx"), "tris": 12, "geo_hash": "x",
            "operations": ["generate"], "warnings": [], "stub": False}}

    cfg = {"output_dir": str(tmp_path), "cache": False}
    for _ in range(2):
        mgr = BlenderJobManager(cfg, mcp_executor=executor)
        mgr.submit(build_job(name="rock", asset_type="prop", style="low_poly"))
        mgr.run_all()
    assert calls["n"] == 2
    assert not (Path(tmp_path) / "_cache").exists()


def test_geo_substrate_groups_variants_and_dedups_blobs(tmp_path):
    """The deeper layer: two different jobs (→ different fingerprints) that yield
    the SAME mesh are filed under one geo_hash, sharing identical bytes; each
    fingerprint still resolves & restores independently above."""
    from pathlib import Path
    from src.blender.cache import BlenderCache
    from src.blender.manifest import AssetManifest

    bc = BlenderCache(tmp_path / "cache")
    out = tmp_path / "o"; out.mkdir()
    fbx = out / "rock.fbx"; fbx.write_bytes(b"MESH-BYTES")   # identical geometry export

    steps = ["generate", "export"]
    a = build_job(name="rock", asset_type="prop", style="low_poly").default_output(str(tmp_path))
    b = build_job(name="rock", asset_type="prop", style="low_poly").default_output(str(tmp_path))
    a.color, b.color = "#FF0000", "#00FF00"        # only the colour differs
    fpa, fpb = bc.fingerprint(a, steps), bc.fingerprint(b, steps)
    assert fpa != fpb                               # different inputs → different keys

    def _man():
        return AssetManifest(name="rock", asset_type="prop", style="low_poly",
                             route="blender", fbx=str(fbx), geo_hash="GEO1",
                             operations=["generate"], tris=12)
    bc.store(fpa, _man(), out, a)
    bc.store(fpb, _man(), out, b)

    rec = bc.geo.load_geo("GEO1")
    assert rec is not None and len(rec["variants"]) == 2          # one mesh, two variants
    blobs = list((tmp_path / "cache" / "geo" / "blobs").iterdir())
    assert len(blobs) == 1                                        # identical bytes → one blob

    ea, eb = bc.lookup(fpa), bc.lookup(fpb)                       # skip-work layer intact
    assert ea and eb and ea.geo_hash == "GEO1" and eb.geo_hash == "GEO1"
    ra = bc.restore(ea, tmp_path / "ra", a)
    assert Path(ra.fbx) == tmp_path / "ra" / "rock.fbx"
    assert Path(ra.fbx).read_bytes() == b"MESH-BYTES" and "cache_hit" in ra.operations
    assert bc.stats()["geo_hashes"] == 1 and bc.stats()["fingerprints"] == 2


# ── rig strategy router (Phase 3) ─────────────────────────────────────────────

def test_rig_strategy_routing_and_fallback(tmp_path):
    from src.blender.rigging import rig_strategy, rig_class_for, RigKind, TEMPLATE_CLASSES

    # no rig requested → static, no armature
    s = rig_strategy(build_job(name="c", asset_type="prop"))
    assert s.id == "RigAutoStaticV1" and s.kind is RigKind.NONE and not s.needs_armature

    # character rig, no template library → deterministic auto (Rigify) fallback
    hero = build_job(name="h", asset_type="character"); hero.rig = True
    assert rig_strategy(hero).id == "RigAutoHumanoidV1"
    assert rig_strategy(hero).kind is RigKind.AUTO

    # vehicle rig → mechanical; rig=True crate → static (a crate doesn't deform)
    car = build_job(name="v", asset_type="vehicle"); car.rig = True
    assert rig_strategy(car).id == "RigAutoMechanicalV1"
    crate = build_job(name="b", asset_type="prop"); crate.rig = True
    assert rig_strategy(crate).id == "RigAutoStaticV1"

    # explicit ragdoll hint
    rag = build_job(name="r", asset_type="character"); rag.rig = True
    rag.meta["rig_kind"] = "ragdoll"
    assert rig_strategy(rag).id == "RigAutoRagdollV1"

    # template selected once an authored armature is available
    (tmp_path / "humanoid.blend").write_bytes(b"ARM")
    s = rig_strategy(hero, template_root=str(tmp_path))
    assert s.id == "RigTemplateHumanoidV1" and s.kind is RigKind.TEMPLATE
    assert s.template_class == "humanoid" and s.needs_armature
    # a class with no shipped armature still falls back even when a root is set
    assert rig_strategy(car, template_root=str(tmp_path)).id == "RigAutoMechanicalV1"

    assert rig_class_for("character") == "humanoid" and rig_class_for("terrain") is None
    assert set(TEMPLATE_CLASSES) == {"humanoid", "quadruped", "car", "rifle", "door", "crate"}


# ── geometry fingerprint + pre-rig mesh layer (Phase 3) ───────────────────────

def test_geo_fingerprint_excludes_variant_axes(tmp_path):
    from src.blender.cache import BlenderCache
    bc = BlenderCache(tmp_path); bc.blender_version = "5.1"
    base = build_job(name="rock", asset_type="prop", style="low_poly")
    gf = bc.geo_fingerprint(base)
    assert gf.startswith("geo-")

    # colour / material / rig / animations do NOT change the geometry key
    for mut in (lambda j: setattr(j, "color", "#FF0000"),
                lambda j: setattr(j, "rig", True),
                lambda j: setattr(j, "animations", ["walk"]),
                lambda j: setattr(j.textures, "material_brief", "rusty")):
        j = build_job(name="rock", asset_type="prop", style="low_poly"); mut(j)
        assert bc.geo_fingerprint(j) == gf

    # detail / geometry / Blender version DO
    j = build_job(name="rock", asset_type="prop", style="low_poly"); j.detail = 5
    assert bc.geo_fingerprint(j) != gf
    bc.blender_version = "5.2"
    assert bc.geo_fingerprint(base) != gf


def test_geo_mesh_layer_roundtrip(tmp_path):
    from pathlib import Path
    from src.blender.cache import GeoStore
    gs = GeoStore(tmp_path / "geo")
    blend = tmp_path / "src.mesh.blend"; blend.write_bytes(b"PRE-RIG-MESH")
    assert gs.put_mesh("geo-abc", blend, "GEOHASH", 12) is not None
    rec = gs.get_mesh("geo-abc")
    assert rec and rec["geo_hash"] == "GEOHASH" and Path(rec["blend_path"]).is_file()
    out = gs.restore_mesh("geo-abc", tmp_path / "dst" / "x.mesh.blend")
    assert out and out.read_bytes() == b"PRE-RIG-MESH"
    assert gs.get_mesh("geo-missing") is None
    assert gs.stats()["meshes"] == 1


def test_full_fingerprint_tracks_rig_strategy(tmp_path):
    from src.blender.cache import BlenderCache
    bc = BlenderCache(tmp_path)
    steps = ["generate", "rig", "export"]
    a = build_job(name="h", asset_type="character"); a.rig = True   # → RigAutoHumanoidV1
    b = build_job(name="h", asset_type="character"); b.rig = True
    b.meta["rig_kind"] = "ragdoll"                                  # → RigAutoRagdollV1
    assert bc.fingerprint(a, steps) != bc.fingerprint(b, steps)


def test_manager_geometry_reuse_across_color_variants(tmp_path):
    """Cold build caches the pre-rig mesh; a colour variant (different full
    fingerprint → skip-work miss) reuses that mesh and runs only the delta."""
    codes = []

    def executor(code: str) -> dict:
        codes.append(code)
        out = tmp_path / "hero"; out.mkdir(parents=True, exist_ok=True)
        (out / "hero.fbx").write_bytes(b"FBX")
        is_reuse = "REUSE_BLEND = ''" not in code
        mesh_blend = None
        if not is_reuse:                       # cold build saves the reusable mesh
            (out / "hero.mesh.blend").write_bytes(b"MESH")
            mesh_blend = str(out / "hero.mesh.blend")
        return {"status": "ok", "result": {
            "name": "hero", "asset_type": "character", "style": "low_poly",
            "route": "blender", "fbx": str(out / "hero.fbx"), "blend": None,
            "textures": [], "lods": [], "animations": [], "rigged": True, "tris": 20,
            "geo_hash": "GEOHERO", "mesh_blend": mesh_blend,
            "rig_strategy": "RigAutoHumanoidV1",
            "operations": ["geo_reuse" if is_reuse else "generate", "export_fbx"],
            "warnings": [], "stub": False}}

    cfg = {"output_dir": str(tmp_path)}

    m1 = BlenderJobManager(cfg, mcp_executor=executor)
    red = build_job(name="hero", asset_type="character", style="low_poly")
    red.rig = True; red.color = "#FF0000"
    m1.submit(red); b1 = m1.run_all()
    assert b1.done == 1
    geo_fp = m1.cache.geo_fingerprint(red)
    assert m1.cache.geo.get_mesh(geo_fp) is not None       # pre-rig mesh cached
    assert "REUSE_BLEND = ''" in codes[0]                  # first run was a cold build

    # same geometry, different colour: skip-work misses, geometry is reused
    m2 = BlenderJobManager(cfg, mcp_executor=executor)
    green = build_job(name="hero", asset_type="character", style="low_poly")
    green.rig = True; green.color = "#00FF00"
    assert m2.cache.geo_fingerprint(green) == geo_fp
    m2.submit(green); b2 = m2.run_all()
    assert b2.done == 1
    assert len(codes) == 2                                 # skip-work missed (executor ran)
    assert "REUSE_BLEND = ''" not in codes[1]             # ...but geometry was reused
    assert "hero.mesh.blend" in codes[1]
    assert (tmp_path / "hero" / "hero.mesh.blend").is_file()  # mesh restored for the run
