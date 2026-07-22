"""Lighting micro-generator — rig blueprints, the ambient guard, and compose wiring.

The load-bearing property here is the "never black" invariant: whatever the LLM
authors, whatever the variant, the emitted rig must resolve to a lit scene. Every
test below either checks that invariant or checks that enforcing it did not
trample the project's authored look.
"""

from pathlib import Path

import pytest

from src.micro import REGISTRY
from src.micro.lighting import (
    AMBIENT_LUMINANCE_FLOOR,
    KEY_INTENSITY_FLOOR,
    VARIANTS,
    LightSpec,
    LightingPipeline,
    LightingSchema,
    _luminance,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":   str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


@pytest.fixture
def pipeline(cfg):
    return LightingPipeline(cfg)


def _fixed(pipeline, variant, **kw):
    return pipeline.default_fix(LightingSchema(variant=variant, **kw), variant)


# ── Registry / wiring ────────────────────────────────────────────────────────

def test_lighting_is_registered():
    assert REGISTRY["lighting"] is LightingPipeline


def test_lighting_is_in_every_golden_core_signature():
    """Always-on means always-on — for 2D and 3D archetypes alike."""
    from src.unity_compose import (
        GOLDEN_2D_CORE_SIGNATURE, GOLDEN_2D_TOPDOWN_CORE_SIGNATURE,
        GOLDEN_2_5D_CORE_SIGNATURE, GOLDEN_3D_CORE_SIGNATURE,
    )
    for sig in (GOLDEN_3D_CORE_SIGNATURE, GOLDEN_2D_CORE_SIGNATURE,
                GOLDEN_2_5D_CORE_SIGNATURE, GOLDEN_2D_TOPDOWN_CORE_SIGNATURE):
        assert "lighting" in {kind for kind, _ in sig}


def test_compose_always_emits_lighting():
    from src.unity_compose import subsystems_for
    for gt in ("3D", "2D", "2.5D", "2d_topdown", "top_down", "VR", "isometric"):
        kinds = {s.kind for s in subsystems_for(gt, [])}
        assert "lighting" in kinds, gt


# ── Variant selection ────────────────────────────────────────────────────────

@pytest.mark.parametrize("game_type", ["2D", "2d_topdown"])
def test_flat_2d_variant_for_2d_game_types(game_type):
    from src.unity_compose import subsystems_for
    sub = next(s for s in subsystems_for(game_type, []) if s.kind == "lighting")
    assert sub.variant == "flat_2d"


def test_interior_prompt_selects_practicals():
    from src.unity_compose import subsystems_for
    sub = next(s for s in subsystems_for(
        "3D", [], user_prompt="escape the flooded submarine corridor")
        if s.kind == "lighting")
    assert sub.variant == "interior_practical"


def test_exterior_prompt_selects_sun_even_at_night():
    """A night race is still lit by a sky — exterior must win over time-of-day."""
    from types import SimpleNamespace
    from src.unity_compose import subsystems_for
    sub = next(s for s in subsystems_for(
        "3D", [], user_prompt="midnight street race through the city",
        environment=SimpleNamespace(time_of_day="night"))
        if s.kind == "lighting")
    assert sub.variant == "outdoor_sun"


@pytest.mark.parametrize("prompt", [
    "explore the mine tunnels",              # plural of a listed word
    "abandoned research station corridors",  # plural + a word that was missing
    "crawl through the sewers",
])
def test_plural_interior_words_still_match(prompt):
    """The word tables are singular; prompts are not. Depluralisation is the bridge."""
    from src.unity_compose import subsystems_for
    sub = next(s for s in subsystems_for("3D", [], user_prompt=prompt)
               if s.kind == "lighting")
    assert sub.variant == "interior_practical", prompt


def test_unsignalled_project_falls_back_to_three_point():
    from src.unity_compose import subsystems_for
    sub = next(s for s in subsystems_for("3D", [], user_prompt="a game about a thing")
               if s.kind == "lighting")
    assert sub.variant == "three_point"


def test_variant_selection_is_deterministic():
    from src.unity_compose import subsystems_for
    kw = dict(user_prompt="abandoned laboratory", visual_style="realistic")
    a = next(s for s in subsystems_for("3D", [], **kw) if s.kind == "lighting")
    b = next(s for s in subsystems_for("3D", [], **kw) if s.kind == "lighting")
    assert (a.variant, a.options) == (b.variant, b.options)


# ── The "never black" invariant ──────────────────────────────────────────────

@pytest.mark.parametrize("variant", VARIANTS)
def test_every_variant_resolves_to_a_lit_scene(pipeline, variant):
    s = _fixed(pipeline, variant)
    ambient = _luminance(s.ambient_sky_r, s.ambient_sky_g, s.ambient_sky_b)
    assert ambient * s.ambient_intensity >= AMBIENT_LUMINANCE_FLOOR - 1e-6
    if variant != "flat_2d":
        directional = sum(l.intensity for l in s.lights if l.type == "directional")
        assert directional >= KEY_INTENSITY_FLOOR - 1e-6


def test_black_ambient_is_lifted_not_replaced(pipeline):
    """A dark-but-tinted ambient is brightened along its own hue, not greyed out."""
    s = _fixed(pipeline, "three_point",
               ambient_sky_r=0.04, ambient_sky_g=0.01, ambient_sky_b=0.02)
    lit = _luminance(s.ambient_sky_r, s.ambient_sky_g, s.ambient_sky_b) * s.ambient_intensity
    assert lit >= AMBIENT_LUMINANCE_FLOOR - 1e-6
    # 4:1:2 in, 4:1:2 out — the channel ratios are the look, and they survive.
    assert s.ambient_sky_r / s.ambient_sky_g == pytest.approx(4.0, rel=1e-3)
    assert s.ambient_sky_b / s.ambient_sky_g == pytest.approx(2.0, rel=1e-3)


def test_dark_palette_cannot_exhaust_the_intensity_clamp(pipeline):
    """ambient_intensity clamps at 8.0 — the lift must not rely on it alone.

    Regression: scaling intensity for a near-black palette saturated at 8.0 and
    still landed under the floor, silently shipping the black scene this micro
    exists to prevent.
    """
    palette = {**_PALETTE, "background": {"r": 0.02, "g": 0.0, "b": 0.04}}
    s = _fixed(pipeline, "three_point", meta={"palette_rgb": palette})
    lit = _luminance(s.ambient_sky_r, s.ambient_sky_g, s.ambient_sky_b) * s.ambient_intensity
    assert lit >= AMBIENT_LUMINANCE_FLOOR - 1e-6
    assert s.ambient_intensity <= 8.0


def test_ambient_gradient_keeps_its_shape_after_a_lift(pipeline):
    """Sky stays brighter than equator, equator brighter than ground."""
    palette = {**_PALETTE, "background": {"r": 0.03, "g": 0.02, "b": 0.05}}
    s = _fixed(pipeline, "three_point", meta={"palette_rgb": palette})
    sky = _luminance(s.ambient_sky_r, s.ambient_sky_g, s.ambient_sky_b)
    eq  = _luminance(s.ambient_equator_r, s.ambient_equator_g, s.ambient_equator_b)
    gnd = _luminance(s.ambient_ground_r, s.ambient_ground_g, s.ambient_ground_b)
    assert sky > eq > gnd


def test_zero_ambient_with_no_lights_hard_fails(pipeline):
    """FAIL-FAST: an all-zero unlit scene is broken output, not a look."""
    schema = LightingSchema(
        variant="flat_2d",
        ambient_sky_r=0.0, ambient_sky_g=0.0, ambient_sky_b=0.0,
    )
    with pytest.raises(ValueError, match="unlit scene"):
        pipeline.default_fix(schema, "flat_2d")


def test_dim_key_light_is_lifted_to_the_floor(pipeline):
    s = _fixed(pipeline, "three_point",
               lights=[LightSpec(role="key", type="directional", intensity=0.01)])
    key = next(l for l in s.lights if l.role == "key")
    assert key.intensity >= KEY_INTENSITY_FLOOR - 1e-6


def test_flat_2d_drops_authored_lights(pipeline):
    """2D disables the scene's Directional Light — a stray one would only darken."""
    s = _fixed(pipeline, "flat_2d",
               lights=[LightSpec(role="key", type="directional", intensity=2.0)])
    assert s.lights == []
    assert s.ambient_mode == "flat"


# ── Palette-derived look (no generic defaults) ───────────────────────────────

_PALETTE = {
    "primary":    {"r": 0.61, "g": 0.35, "b": 0.71},
    "secondary":  {"r": 0.10, "g": 0.74, "b": 0.61},
    "accent":     {"r": 0.90, "g": 0.49, "b": 0.13},
    "background": {"r": 0.17, "g": 0.24, "b": 0.31},
    "trim":       {"r": 0.94, "g": 0.94, "b": 0.94},
}


def test_ambient_gradient_comes_from_the_palette(pipeline):
    s = _fixed(pipeline, "three_point", meta={"palette_rgb": _PALETTE})
    bg = _PALETTE["background"]
    assert (s.ambient_sky_r, s.ambient_sky_g, s.ambient_sky_b) == (bg["r"], bg["g"], bg["b"])
    # equator is the scene template's own background*0.6 gradient
    assert s.ambient_equator_r == pytest.approx(bg["r"] * 0.6, abs=1e-4)


def test_rim_light_takes_the_accent_color(pipeline):
    s = _fixed(pipeline, "three_point", meta={"palette_rgb": _PALETTE})
    rim = next(l for l in s.lights if l.role == "rim")
    acc = _PALETTE["accent"]
    assert (rim.color_r, rim.color_g, rim.color_b) == pytest.approx(
        (acc["r"], acc["g"], acc["b"]), abs=1e-3)


def test_near_black_palette_slot_is_lifted_keeping_hue(pipeline):
    palette = {**_PALETTE, "background": {"r": 0.02, "g": 0.0, "b": 0.04}}
    s = _fixed(pipeline, "three_point", meta={"palette_rgb": palette})
    fill = next(l for l in s.lights if l.role == "fill")
    assert fill.color_b > fill.color_r > fill.color_g       # hue preserved
    assert _luminance(fill.color_r, fill.color_g, fill.color_b) >= 0.24


# ── Emitted C# ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", VARIANTS)
def test_writes_script_and_readme(pipeline, variant, tmp_path):
    s = _fixed(pipeline, variant)
    count = pipeline.write_files(s, variant, tmp_path)
    assert count == 2
    assert (tmp_path / "Scripts" / f"{s.name}.cs").is_file()
    assert (tmp_path / "README.md").is_file()


def test_script_runs_before_other_systems(pipeline, tmp_path):
    s = _fixed(pipeline, "three_point")
    pipeline.write_files(s, "three_point", tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    # EntitySpawner/MaterialPalette build materials in Awake; the guard must win.
    assert "[DefaultExecutionOrder(-500)]" in cs


@pytest.mark.parametrize("variant", VARIANTS)
def test_script_always_writes_render_settings(pipeline, variant, tmp_path):
    s = _fixed(pipeline, variant)
    pipeline.write_files(s, variant, tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    for member in ("RenderSettings.ambientMode", "RenderSettings.ambientSkyColor",
                   "RenderSettings.ambientLight", "DynamicGI.UpdateEnvironment"):
        assert member in cs, f"{variant} missing {member}"


def test_script_measures_the_probe_before_trusting_skybox_ambient(pipeline, tmp_path):
    """The skybox fallback is measured, not guessed — that's the whole point."""
    s = _fixed(pipeline, "outdoor_sun")
    pipeline.write_files(s, "outdoor_sun", tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    assert "RenderSettings.ambientProbe" in cs
    assert "AmbientProbeLuminance() < ambientLuminanceFloor" in cs


def test_flat_2d_emits_no_light_objects(pipeline, tmp_path):
    s = _fixed(pipeline, "flat_2d")
    pipeline.write_files(s, "flat_2d", tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    assert "MakeLight(" in cs                      # the helper is still declared
    assert 'MakeLight("Light_' not in cs           # but nothing calls it


def test_rig_adopts_the_scenes_directional_light(pipeline, tmp_path):
    """The scene's time-of-day sun stays authoritative — we only raise it."""
    s = _fixed(pipeline, "three_point")
    pipeline.write_files(s, "three_point", tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    assert "FindBrightestDirectional()" in cs
    assert "if (_key.intensity <" in cs


def test_rig_build_is_idempotent(pipeline, tmp_path):
    s = _fixed(pipeline, "interior_practical")
    pipeline.write_files(s, "interior_practical", tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    assert "if (_built) return;" in cs
    assert "transform.Find(RigRootName)" in cs


def test_atmosphere_projects_keep_ownership_of_render_settings(pipeline, tmp_path):
    s = _fixed(pipeline, "outdoor_sun")
    pipeline.write_files(s, "outdoor_sun", tmp_path)
    cs = (tmp_path / "Scripts" / f"{s.name}.cs").read_text(encoding="utf-8")
    assert "HasAtmosphereController" in cs
    assert "AtmosphereBootstrap" in cs


def test_emitted_script_is_byte_identical_across_runs(pipeline, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        s = _fixed(pipeline, "three_point", meta={"palette_rgb": _PALETTE})
        pipeline.write_files(s, "three_point", out)
    assert ((a / "Scripts" / "LightingDirector.cs").read_bytes()
            == (b / "Scripts" / "LightingDirector.cs").read_bytes())


# ── Composed end-to-end ──────────────────────────────────────────────────────

def test_composer_emits_lighting_scripts(tmp_path):
    from src.unity_compose import MicroComposer, subsystems_for
    assets = tmp_path / "Assets"
    units = MicroComposer("LitProject").emit(subsystems_for("3D", []), assets)
    unit = next(u for u in units if u.kind == "lighting")
    assert unit.class_name == "LightingDirector"
    assert (assets / "Micros" / "lighting" / "Scripts" / "LightingDirector.cs").is_file()
    assert (assets / "Micros" / "lighting" / "Scripts" / "LightingDirector.cs.meta").is_file()


# ── Scene YAML ↔ runtime guard agreement ─────────────────────────────────────

def _project(**kw):
    from src.unity_schema import ColorPalette, EnvironmentSpec, UnitySchema
    schema = UnitySchema(
        project_name=kw.pop("project_name", "AmbientAgreement"),
        description=kw.pop("description", "a test project"),
        game_type=kw.pop("game_type", "3D"), platform="PC",
        render_pipeline="URP", unity_version="6000.0.23f1",
        visual_style=kw.pop("visual_style", "realistic"),
        color_palette=ColorPalette(**kw.pop("palette", dict(
            name="Murk", primary="#0D1B2A", secondary="#1B3A4B",
            accent="#E07A5F", background="#0B1015", trim="#C9D6DF"))),
        scenes=["Main"], **kw)
    tod = kw.get("time_of_day")
    if tod:
        schema.environment = EnvironmentSpec(time_of_day=tod)
    return schema


def _scene_and_script(schema, tmp_path):
    """Generate the scene YAML and the composed LightingDirector.cs for a project."""
    from src.unity_compose import MicroComposer, subsystems_for
    from src.unity_generator import UnityProjectGenerator
    from src.unity_pipeline import _compose_palette_rgb

    subs = subsystems_for(
        schema.game_type, schema.features,
        user_prompt=schema.meta.get("user_prompt", ""),
        environment=schema.environment, visual_style=schema.visual_style,
        palette_rgb=_compose_palette_rgb(schema))
    units = MicroComposer(schema.project_name).plan(subs)
    MicroComposer(schema.project_name).emit(subs, tmp_path / "Assets")
    cs = (tmp_path / "Assets" / "Micros" / "lighting" / "Scripts"
          / "LightingDirector.cs").read_text(encoding="utf-8")
    files = UnityProjectGenerator("templates/unity").generate(
        schema, {}, composed_units=units)
    return files[f"{schema.project_name}/Assets/Scenes/Main.unity"], cs


def _yaml_color(scene, key):
    import re
    m = re.search(rf"{key}: \{{r: ([\d.]+), g: ([\d.]+), b: ([\d.]+)", scene)
    return tuple(round(float(x), 4) for x in m.groups())


def _cs_color(cs, field):
    import re
    m = re.search(rf"{field}\s*= new Color\(([\d.]+)f, ([\d.]+)f, ([\d.]+)f\)", cs)
    return tuple(round(float(x), 4) for x in m.groups())


@pytest.mark.parametrize("kw", [
    {},                                                    # three_point
    {"description": "derelict mine tunnels",
     "meta": {"user_prompt": "derelict mine tunnels"}},    # interior_practical
    {"description": "desert highway", "time_of_day": "day",
     "meta": {"user_prompt": "desert highway"}},           # outdoor_sun
    {"game_type": "2D", "visual_style": "pixel_art"},      # flat_2d
])
def test_scene_yaml_matches_the_runtime_guard(kw, tmp_path):
    """The scene at rest must equal the scene at Play.

    Before this, scene.unity.j2 re-derived the ambient gradient from the raw
    palette while the runtime guard applied a luminance floor — so a dark
    palette rendered black in the Editor until you pressed Play.
    """
    import re
    scene, cs = _scene_and_script(_project(**kw), tmp_path)

    mode_num  = re.search(r"m_AmbientMode: (\d+)", scene).group(1)
    mode_name = re.search(r"ambientMode\s*= AmbientMode\.(\w+)", cs).group(1)
    assert {"0": "Skybox", "1": "Trilight", "3": "Flat"}[mode_num] == mode_name

    assert float(re.search(r"m_AmbientIntensity: ([\d.]+)", scene).group(1)) == \
        pytest.approx(float(re.search(r"ambientIntensity = ([\d.]+)f", cs).group(1)))

    for yaml_key, cs_field in (("m_AmbientSkyColor", "ambientSky"),
                               ("m_AmbientEquatorColor", "ambientEquator"),
                               ("m_AmbientGroundColor", "ambientGround")):
        assert _yaml_color(scene, yaml_key) == _cs_color(cs, cs_field), cs_field


def test_scene_yaml_carries_the_luminance_floor(tmp_path):
    """A near-black palette must not produce a near-black scene at rest."""
    scene, _ = _scene_and_script(_project(palette=dict(
        name="Pitch", primary="#0D1B2A", secondary="#03060A",
        accent="#E07A5F", background="#020406", trim="#C9D6DF")), tmp_path)
    sky = _yaml_color(scene, "m_AmbientSkyColor")
    assert _luminance(*sky) >= AMBIENT_LUMINANCE_FLOOR - 1e-6


def test_non_composed_project_still_gets_a_lit_scene(tmp_path):
    """No --compose means no lighting unit — the generator must still resolve one."""
    from src.unity_generator import UnityProjectGenerator
    schema = _project(description="derelict mine tunnels",
                      meta={"user_prompt": "derelict mine tunnels"})
    files = UnityProjectGenerator("templates/unity").generate(schema, {})
    scene = files[f"{schema.project_name}/Assets/Scenes/Main.unity"]
    sky = _yaml_color(scene, "m_AmbientSkyColor")
    assert _luminance(*sky) >= AMBIENT_LUMINANCE_FLOOR - 1e-6


def test_composed_lighting_lands_on_a_systems_holder(tmp_path):
    from src.unity_compose import MicroComposer, subsystems_for
    units = MicroComposer("LitTarget").plan(subsystems_for("3D", []))
    unit = next(u for u in units if u.kind == "lighting")
    assert unit.target not in ("camera", "player", "asset")
