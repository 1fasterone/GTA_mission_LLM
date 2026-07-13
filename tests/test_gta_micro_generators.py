"""
Tests for the GTA5 micro layer: GTA_REGISTRY completeness, per-kind schema
clamps, deterministic template rendering (every kind x variant), atom-stub
emission via the gta5 backend, and the SHVDN lint over the extra_* channel.

No LLM / network — schemas are constructed directly and rendered through
write_files, mirroring tests/test_micro_generators.py.
"""
from pathlib import Path

import pytest

from src.micro import GTA_REGISTRY
from src.micro.code_fields import GtaCodeExtensibleSchema, lint_gta_extensible
from src.micro.gta5 import (
    GtaHudPipeline,
    GtaMenuPipeline,
    GtaSaveLoadPipeline,
    GtaSpawnerPipeline,
    GtaWavesPipeline,
)


@pytest.fixture
def dummy_config() -> dict:
    """LLM client is constructed but never called — write_files is direct."""
    return {"lm_studio": {"base_url": "http://localhost:9", "model": "test",
                          "temperature": 0.7, "max_tokens": 64, "timeout": 1.0}}


_ALL_KIND_VARIANTS = [
    (kind, variant)
    for kind, cls in GTA_REGISTRY.items()
    for variant in cls.variants
]


# ══════════════════════════════════════════════════════════════════════════════
# Registry
# ══════════════════════════════════════════════════════════════════════════════

class TestGtaRegistry:
    def test_registry_has_the_minigame_serving_set(self):
        assert set(GTA_REGISTRY) == {"menu", "hud", "spawner", "waves", "saveload"}

    def test_every_pipeline_is_gta5_engine(self, dummy_config):
        for kind, cls in GTA_REGISTRY.items():
            pipe = cls(dummy_config)
            assert pipe.engine == "gta5", kind
            assert pipe.variants, kind
            assert pipe.kind == kind
            assert "gta5" in str(pipe.template_root)

    def test_schemas_are_gta_extensible(self):
        for kind, cls in GTA_REGISTRY.items():
            assert issubclass(cls.schema_cls, GtaCodeExtensibleSchema), kind

    def test_prompts_mention_shvdn_not_unity(self):
        for kind, cls in GTA_REGISTRY.items():
            blob = cls.concept_prompt + cls.schema_prompt
            assert "never Unity" in blob or "NOT Unity" in blob, kind


# ══════════════════════════════════════════════════════════════════════════════
# Schema clamps
# ══════════════════════════════════════════════════════════════════════════════

class TestGtaMicroSchemas:
    def test_unknown_variants_clamp_to_default(self):
        for kind, cls in GTA_REGISTRY.items():
            schema = cls.schema_cls(variant="does_not_exist")
            assert schema.variant == cls.default_variant, kind

    def test_menu_hotkey_clamps(self):
        s = GtaMenuPipeline.schema_cls(hotkey="Escape")
        assert s.hotkey == "F6"
        assert GtaMenuPipeline.schema_cls(hotkey="f9").hotkey == "F9"

    def test_menu_rows_truncated_to_eight(self):
        s = GtaMenuPipeline.schema_cls(rows=[f"Row {i}" for i in range(12)])
        assert len(s.rows) == 8

    def test_spawner_model_ids_validated(self):
        s = GtaSpawnerPipeline.schema_cls(ped_model="Angry Biker!!",
                                          vehicle_model="Police Cruiser",
                                          prop_model="prop_barrel_02a")
        assert s.ped_model == "g_m_y_lost_01"      # non-ID falls back
        assert s.vehicle_model == "police2"
        assert s.prop_model == "prop_barrel_02a"   # valid ID kept

    def test_spawner_count_and_radius_clamped(self):
        s = GtaSpawnerPipeline.schema_cls(count=99, radius=999.0)
        assert s.count == 12
        assert s.radius == 100.0

    def test_waves_params_clamped(self):
        s = GtaWavesPipeline.schema_cls(base_count=50, growth=9.0,
                                        max_waves=0, intermission_seconds="soon")
        assert s.base_count == 8
        assert s.growth == 3.0
        assert s.max_waves == 1
        assert s.intermission_seconds == 8

    def test_saveload_ini_name_sanitized(self):
        s = GtaSaveLoadPipeline.schema_cls(ini_name="My Scores!.ini", section="High Scores")
        assert s.ini_name == "my_scores_ini"
        assert s.section == "HighScores"


# ══════════════════════════════════════════════════════════════════════════════
# Rendering — every kind x variant emits balanced SHVDN C#
# ══════════════════════════════════════════════════════════════════════════════

class TestGtaMicroRendering:
    @pytest.mark.parametrize("kind,variant", _ALL_KIND_VARIANTS)
    def test_kind_variant_renders_balanced_script(self, dummy_config, tmp_path, kind, variant):
        pipe = GTA_REGISTRY[kind](dummy_config)
        schema = pipe.default_fix(pipe.schema_cls(variant=variant), variant)
        count = pipe.write_files(schema, variant, tmp_path)
        assert count == 2
        cs_files = list(tmp_path.glob("*.3.cs"))
        assert len(cs_files) == 1
        cs = cs_files[0].read_text(encoding="utf-8")
        assert ": Script" in cs
        assert "UnityEngine" not in cs
        assert cs.count("{") == cs.count("}"), f"{kind}/{variant} braces"
        assert cs.count("(") == cs.count(")"), f"{kind}/{variant} parens"
        assert (tmp_path / "README.md").exists()

    @pytest.mark.parametrize("kind,variant", _ALL_KIND_VARIANTS)
    def test_rendering_is_deterministic(self, dummy_config, tmp_path, kind, variant):
        pipe = GTA_REGISTRY[kind](dummy_config)
        schema = pipe.default_fix(pipe.schema_cls(variant=variant), variant)
        a_dir, b_dir = tmp_path / "a", tmp_path / "b"
        a_dir.mkdir(); b_dir.mkdir()
        pipe.write_files(schema, variant, a_dir)
        pipe.write_files(schema, variant, b_dir)
        a = {p.name: p.read_bytes() for p in a_dir.iterdir()}
        b = {p.name: p.read_bytes() for p in b_dir.iterdir()}
        assert a == b

    def test_saveload_uses_explicit_scriptsettings_path(self, dummy_config, tmp_path):
        pipe = GtaSaveLoadPipeline(dummy_config)
        schema = pipe.default_fix(pipe.schema_cls(), "highscore_table")
        pipe.write_files(schema, "highscore_table", tmp_path)
        cs = (tmp_path / f"{schema.name}.3.cs").read_text(encoding="utf-8")
        assert 'ScriptSettings.Load("scripts\\\\minigame_vault.ini")' in cs
        assert "try" in cs and "catch" in cs      # degrade-on-readonly guard

    def test_spawner_weapon_reaches_generated_code(self, dummy_config, tmp_path):
        pipe = GtaSpawnerPipeline(dummy_config)
        schema = pipe.default_fix(pipe.schema_cls(weapon="rifle"), "radial_arena")
        pipe.write_files(schema, "radial_arena", tmp_path)
        cs = (tmp_path / f"{schema.name}.3.cs").read_text(encoding="utf-8")
        assert '"weapon_assaultrifle"' in cs
        assert "GET_HASH_KEY" in cs

    def test_menu_rows_reach_generated_code(self, dummy_config, tmp_path):
        pipe = GtaMenuPipeline(dummy_config)
        schema = pipe.default_fix(
            pipe.schema_cls(rows=["Volume", "Difficulty"], hotkey="F8"), "settings_panel")
        pipe.write_files(schema, "settings_panel", tmp_path)
        cs = (tmp_path / f"{schema.name}.3.cs").read_text(encoding="utf-8")
        assert '"Volume"' in cs and '"Difficulty"' in cs
        assert "Keys.F8" in cs

    def test_waves_variant_changes_sizing_math(self, dummy_config, tmp_path):
        pipe = GtaWavesPipeline(dummy_config)
        for variant, marker in (("exponential", "Math.Pow"), ("linear", "linear ramp")):
            out = tmp_path / variant
            out.mkdir()
            schema = pipe.default_fix(pipe.schema_cls(variant=variant), variant)
            pipe.write_files(schema, variant, out)
            cs = (out / f"{schema.name}.3.cs").read_text(encoding="utf-8")
            assert marker in cs, variant


# ══════════════════════════════════════════════════════════════════════════════
# Atom stubs (_after_write via the gta5 backend)
# ══════════════════════════════════════════════════════════════════════════════

class TestGtaMicroAtomStubs:
    def test_mapped_kinds_emit_default_behavior_stub(self, dummy_config, tmp_path):
        for kind in ("hud", "spawner", "waves"):
            out = tmp_path / kind
            out.mkdir()
            pipe = GTA_REGISTRY[kind](dummy_config)
            schema = pipe.default_fix(pipe.schema_cls(), pipe.default_variant)
            pipe._after_write(schema, pipe.default_variant, out, schema.name)
            stubs = list(out.glob("*DefaultBehavior.3.cs"))
            assert len(stubs) == 1, kind
            cs = stubs[0].read_text(encoding="utf-8")
            assert ": Script" in cs
            assert "_ran" in cs
            assert cs.count("{") == cs.count("}")

    def test_unmapped_kinds_skip_stub(self, dummy_config, tmp_path):
        for kind in ("menu", "saveload"):
            out = tmp_path / kind
            out.mkdir()
            pipe = GTA_REGISTRY[kind](dummy_config)
            schema = pipe.default_fix(pipe.schema_cls(), pipe.default_variant)
            pipe._after_write(schema, pipe.default_variant, out, schema.name)
            assert list(out.glob("*DefaultBehavior.3.cs")) == [], kind

    def test_gta5_map_uses_only_gta5_capable_atoms(self):
        from src.atoms import ATOM_REGISTRY
        from src.atoms.behavior_map import GTA5_MICROGEN_ATOM_MAP
        for kind, invs in GTA5_MICROGEN_ATOM_MAP.items():
            for inv in invs:
                adef = ATOM_REGISTRY[inv["atom"]]
                assert "gta5" in adef.engines, f"{kind}: {inv['atom']}"

    def test_microgen_atoms_engine_selector(self):
        from src.atoms.behavior_map import microgen_atoms
        # Unity default unchanged (backward compat)…
        assert [a.atom for a in microgen_atoms("spawner")] == ["SpawnEntity"]
        # …gta5 selects the new map.
        gta = [a.atom for a in microgen_atoms("spawner", engine="gta5")]
        assert gta == ["SpawnEntity", "PlaySFX"]
        assert microgen_atoms("menu", engine="gta5") == []


# ══════════════════════════════════════════════════════════════════════════════
# SHVDN lint over the extra_* channel
# ══════════════════════════════════════════════════════════════════════════════

class TestGtaExtensibleLint:
    def test_invented_native_neutralized(self):
        schema = GtaCodeExtensibleSchema(
            on_tick_extra="Function.Call(Hash.SET_MISSION_PASSED, 1);")
        lint_gta_extensible(schema, reserved_fields=set(),
                            template_usings={"GTA", "GTA.Native"})
        # Neutralization = the statement is commented out, never left executable.
        live = [l for l in schema.on_tick_extra.splitlines()
                if "SET_MISSION_PASSED" in l and not l.strip().startswith("//")]
        assert live == []

    def test_reserved_field_dropped(self):
        schema = GtaCodeExtensibleSchema(
            extra_fields=[{"name": "_wave", "type": "int", "default": "0"},
                          {"name": "_mine", "type": "int", "default": "0"}])
        lint_gta_extensible(schema, reserved_fields={"_wave"},
                            template_usings={"GTA"})
        names = [f.name for f in schema.extra_fields]
        assert "_wave" not in names and "_mine" in names

    def test_lifecycle_fragments_survive_lint(self):
        schema = GtaCodeExtensibleSchema(
            on_start_extra='GTA.UI.Notification.PostTicker("~y~hi", false);',
            on_tick_extra="if (Game.Player.Character.IsDead) return;",
            on_abort_extra="// nothing to clean")
        lint_gta_extensible(schema, reserved_fields=set(), template_usings={"GTA"})
        assert "PostTicker" in schema.on_start_extra
        assert "IsDead" in schema.on_tick_extra
