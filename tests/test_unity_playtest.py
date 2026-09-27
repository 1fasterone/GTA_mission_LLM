"""
PlayMode smoke test harness (src/unity_playtest.py) — the fast, default-suite
half. Covers the pure-Python logic (expectations derivation, manifest
merging, NUnit XML parsing) without launching Unity.

The opt-in half that actually launches Unity lives at the bottom of this
file, under the `unity_playtest` marker (mirrors tests/test_unity_compile.py
exactly — same skip conditions, same reasoning).

`test_build_expectations_*` is the important coverage here: a naive
"expected == entities[i].count" comparison false-fails on ordinary,
already-shipped generator output (skip:-routed entities, tiled path groups,
2D archetype kind exclusions, scene-scoped groups — see
src/unity_playtest.py's module docstring). Each test below locks in one of
those branches against the exact template behavior it mirrors.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import unity_cli
from src.unity_playtest import (
    analyze_render_health,
    build_expectations,
    merge_test_framework_manifest,
    parse_nunit_results,
    TEST_FRAMEWORK_VERSION,
)
from src.unity_schema import EntitySpec, SceneSpec, UnitySchema

ROOT = Path(__file__).resolve().parent.parent
UNITY_OUT = ROOT / "output" / "unity"
TEMPLATE_DIR = ROOT / "templates" / "unity"


def _schema(entities=None, *, scenes=None, scene_specs=None,
            game_type="3D", locomotion="walk", meta=None):
    return UnitySchema(
        project_name="PlayTest", company_name="C", description="",
        game_type=game_type, platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=scenes or ["Main"], scene_specs=scene_specs or [],
        packages=[], locomotion=locomotion,
        entities=entities if entities is not None else [],
        meta=meta or {},
    )


# ── build_expectations ──────────────────────────────────────────────────────

class TestBuildExpectations:

    def test_normal_entity_expects_exact_schema_count(self):
        s = _schema([EntitySpec(name="Coin", kind="collectible", count=6)])
        exp = build_expectations(s)
        [e] = exp.entities
        assert (e.name, e.expected_count, e.exact) == ("Coin", 6, True)

    def test_skip_prefixed_entity_expects_zero(self):
        # Mirrors src/unity_generator.py stamping "skip:merged-into-player"
        # (or any "skip:*") onto schema.meta["entity_model_paths"].
        s = _schema(
            [EntitySpec(name="RiderHat", kind="prop", count=1)],
            meta={"entity_model_paths": {"RiderHat": "skip:merged-into-player"}},
        )
        [e] = build_expectations(s).entities
        assert (e.expected_count, e.exact) == (0, True)

    def test_tiled_path_group_expects_at_least_one(self):
        # EntitySpawner.cs.j2's SpawnTiledGroup derives the real count at
        # runtime from the resolved model's measured bounds vs. arc length —
        # Python cannot predict it, so this must be existence-only.
        s = _schema([EntitySpec(name="Guardrail", kind="prop", count=8,
                                 placement="path", path_width=2.0,
                                 path_points=[[0, 0, 0], [10, 0, 0]])])
        [e] = build_expectations(s).entities
        assert (e.expected_count, e.exact) == (1, False)

    def test_path_placement_without_width_is_not_tiled(self):
        # path_width defaults to 0.0 -> legacy "spread count along the
        # polyline" behavior, not SpawnTiledGroup — exact count applies.
        s = _schema([EntitySpec(name="Waypoints", kind="prop", count=5,
                                 placement="path",
                                 path_points=[[0, 0, 0], [10, 0, 0]])])
        [e] = build_expectations(s).entities
        assert (e.expected_count, e.exact) == (5, True)

    def test_scene_scoped_out_entity_expects_zero(self):
        # SceneSpec.entities is an explicit allow-list of NAMES for that
        # scene; a group not named there never spawns when that scene loads.
        # SceneManager.LoadScene(0, ...) always loads schema.scenes[0].
        s = _schema(
            [EntitySpec(name="Beacon", kind="prop", count=1),
             EntitySpec(name="BossRoomOnly", kind="enemy", count=3)],
            scenes=["Main", "BossRoom"],
            scene_specs=[SceneSpec(name="Main", entities=["Beacon"])],
        )
        exp = build_expectations(s)
        by_name = {e.name: e for e in exp.entities}
        assert (by_name["Beacon"].expected_count, by_name["Beacon"].exact) == (1, True)
        assert (by_name["BossRoomOnly"].expected_count, by_name["BossRoomOnly"].exact) == (0, True)

    def test_scene_spec_none_entities_means_all_active(self):
        # No SceneSpec for scenes[0] at all -> the default (None) meaning:
        # every group is active. Must NOT be treated as an empty allow-list.
        s = _schema([EntitySpec(name="Coin", kind="collectible", count=4)],
                     scenes=["Main"], scene_specs=[])
        [e] = build_expectations(s).entities
        assert (e.expected_count, e.exact) == (4, True)

    def test_2d_side_scroller_excludes_player_road_ground(self):
        # BuildSideScroller2D (EntitySpawner.cs.j2) excludes
        # kind in {player, road, ground} outright. The player entity itself
        # is excluded upstream of this rule (see
        # test_player_entity_excluded_in_favor_of_tag_check) — asserted here
        # only as "absent", not attributed to this 2D-specific rule.
        s = _schema(
            [EntitySpec(name="Hero", kind="player", count=1, tag="Player"),
             EntitySpec(name="MainRoad", kind="road", count=1),
             EntitySpec(name="Floor", kind="ground", count=1),
             EntitySpec(name="Coin", kind="collectible", count=6)],
            game_type="2D",
        )
        by_name = {e.name: e for e in build_expectations(s).entities}
        assert "Hero" not in by_name
        for excluded in ("MainRoad", "Floor"):
            assert (by_name[excluded].expected_count, by_name[excluded].exact) == (0, True), excluded
        assert (by_name["Coin"].expected_count, by_name["Coin"].exact) == (6, True)

    def test_2d_topdown_excludes_only_player(self):
        # BuildTopDown2D excludes kind=="player" only — "road"/"ground" are
        # ordinary spawned groups there (no ground/gravity concept). Hero's
        # absence here comes from the player-entity exclusion (see below),
        # not from this rule — Floor staying IS this rule's own coverage.
        s = _schema(
            [EntitySpec(name="Hero", kind="player", count=1, tag="Player"),
             EntitySpec(name="Floor", kind="ground", count=1)],
            game_type="2d_topdown",
        )
        by_name = {e.name: e for e in build_expectations(s).entities}
        assert "Hero" not in by_name
        assert (by_name["Floor"].expected_count, by_name["Floor"].exact) == (1, True)

    def test_player_entity_excluded_in_favor_of_tag_check(self):
        # The player doesn't get a fresh "{name}_{i}" GameObject from
        # EntitySpawner like other groups — the generator merges it into the
        # scene's own pre-placed Player object (EntitySpawner.cs.j2's
        # player-merge). PlayModeSmoke.PlayerTagCardinality already checks
        # its presence, more robustly (by tag, independent of naming/merge
        # mechanics) — confirmed live: without this exclusion,
        # EntityGroupSpawned false-fails "expected exactly 1, found 0" on
        # every ordinary embodied project, since no "Player_0" GameObject is
        # ever created.
        s = _schema([EntitySpec(name="Hero", kind="player", count=1, tag="Player"),
                     EntitySpec(name="Coin", kind="collectible", count=3)])
        by_name = {e.name: e for e in build_expectations(s).entities}
        assert "Hero" not in by_name
        assert "Coin" in by_name

    def test_ui_board_2d_does_not_apply_2d_kind_exclusions(self):
        # EntitySpawner.cs.j2's own guard: `{% if game_type == "2D" and not
        # ui_board %}`. A board/UI game (locomotion="none") falls through to
        # the generic per-entity loop regardless of game_type — only skip:
        # zeroes a group there, kind exclusions never apply.
        s = _schema(
            [EntitySpec(name="Token", kind="ground", count=3)],
            game_type="2D", locomotion="none",
        )
        [e] = build_expectations(s).entities
        assert (e.expected_count, e.exact) == (3, True)

    def test_locomotion_none_disables_player_tag_check(self):
        s = _schema([], locomotion="none")
        exp = build_expectations(s)
        assert exp.check_player_tag is False
        assert exp.locomotion == "none"

    def test_embodied_locomotion_enables_player_tag_check(self):
        s = _schema([], locomotion="drive")
        assert build_expectations(s).check_player_tag is True


# ── merge_test_framework_manifest ───────────────────────────────────────────

class TestMergeManifest:

    def test_inserts_when_absent(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"dependencies": {"com.unity.textmeshpro": "3.0.6"}}))
        changed = merge_test_framework_manifest(manifest)
        assert changed is True
        data = json.loads(manifest.read_text())
        assert data["dependencies"]["com.unity.test-framework"] == TEST_FRAMEWORK_VERSION
        assert data["dependencies"]["com.unity.textmeshpro"] == "3.0.6"   # untouched

    def test_idempotent_second_call_is_noop(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"dependencies": {}}))
        merge_test_framework_manifest(manifest)
        changed_again = merge_test_framework_manifest(manifest)
        assert changed_again is False

    def test_never_downgrades_an_existing_pin(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(
            {"dependencies": {"com.unity.test-framework": "9.9.9"}}))
        changed = merge_test_framework_manifest(manifest)
        assert changed is False
        data = json.loads(manifest.read_text())
        assert data["dependencies"]["com.unity.test-framework"] == "9.9.9"


# ── parse_nunit_results ──────────────────────────────────────────────────────

_XML_ALL_PASSED = """<?xml version="1.0" encoding="utf-8"?>
<test-run>
  <test-suite>
    <test-suite>
      <test-case name="PlayModeSmoke.NoConsoleErrorsDuringPlay" result="Passed" />
      <test-case name="PlayModeSmoke.PlayerTagCardinality" result="Passed" />
    </test-suite>
  </test-suite>
</test-run>
"""

_XML_ONE_FAILED = """<?xml version="1.0" encoding="utf-8"?>
<test-run>
  <test-suite>
    <test-case name="PlayModeSmoke.NoConsoleErrorsDuringPlay" result="Passed" />
    <test-case name="PlayModeSmoke.EntityGroupSpawned(&quot;Rivals&quot;)" result="Failed">
      <failure>
        <message><![CDATA[Rivals: expected exactly 3, found 2]]></message>
      </failure>
    </test-case>
  </test-suite>
</test-run>
"""

_XML_WITH_SKIPPED = """<?xml version="1.0" encoding="utf-8"?>
<test-run>
  <test-suite>
    <test-case name="PlayModeSmoke.NoConsoleErrorsDuringPlay" result="Passed" />
    <test-case name="PlayModeSmoke.PlayerTagCardinality" result="Skipped" />
  </test-suite>
</test-run>
"""

_XML_ZERO_CASES = """<?xml version="1.0" encoding="utf-8"?>
<test-run>
  <test-suite />
</test-run>
"""


class TestParseNunitResults:

    def test_all_passed(self, tmp_path):
        xml = tmp_path / "r.xml"
        xml.write_text(_XML_ALL_PASSED)
        result = parse_nunit_results(xml)
        assert result.harness_ok is True
        assert result.failures == []
        assert len(result.outcomes) == 2

    def test_one_failed_carries_message(self, tmp_path):
        xml = tmp_path / "r.xml"
        xml.write_text(_XML_ONE_FAILED)
        result = parse_nunit_results(xml)
        assert result.harness_ok is True
        [fail] = result.failures
        assert "expected exactly 3, found 2" in fail.message

    def test_skipped_case_is_not_a_failure(self, tmp_path):
        xml = tmp_path / "r.xml"
        xml.write_text(_XML_WITH_SKIPPED)
        result = parse_nunit_results(xml)
        assert result.harness_ok is True
        assert result.failures == []
        assert any(o.outcome == "skipped" for o in result.outcomes)

    def test_missing_file_is_harness_failure_never_pass(self, tmp_path):
        result = parse_nunit_results(tmp_path / "does_not_exist.xml")
        assert result.harness_ok is False

    def test_malformed_xml_is_harness_failure(self, tmp_path):
        xml = tmp_path / "r.xml"
        xml.write_text("<not><valid")
        result = parse_nunit_results(xml)
        assert result.harness_ok is False

    def test_zero_test_cases_is_harness_failure_not_silent_pass(self, tmp_path):
        # The "abstain loudly" case: PlayModeSmoke never ran to completion
        # (compile error in the dropped assembly, license prompt, crash) —
        # 0 checks reported must never read as "0 failures = PASS".
        xml = tmp_path / "r.xml"
        xml.write_text(_XML_ZERO_CASES)
        result = parse_nunit_results(xml)
        assert result.harness_ok is False


# ── analyze_render_health (Gap C solution #4 — "does it look right") ────────

def _make_png(tmp_path, name, fill):
    from PIL import Image
    path = tmp_path / name
    Image.new("RGB", (64, 64), fill).save(path)
    return path


class TestAnalyzeRenderHealth:

    def test_normal_frame_passes(self, tmp_path):
        # A plausible scene color (sky blue) — not black, not magenta.
        png = _make_png(tmp_path, "ok.png", (120, 160, 220))
        result = analyze_render_health(png)
        assert result.ok is True

    def test_solid_black_frame_fails(self, tmp_path):
        png = _make_png(tmp_path, "black.png", (0, 0, 0))
        result = analyze_render_health(png)
        assert result.ok is False
        assert "black" in result.reason.lower()

    def test_dominant_magenta_frame_fails(self, tmp_path):
        # Unity's classic missing-shader color.
        png = _make_png(tmp_path, "magenta.png", (255, 0, 255))
        result = analyze_render_health(png)
        assert result.ok is False
        assert "magenta" in result.reason.lower()

    def test_a_little_magenta_is_fine(self, tmp_path):
        # A scene can legitimately have some pink/magenta (a light, a
        # material) without every pixel being it — only DOMINANCE fails.
        from PIL import Image
        img = Image.new("RGB", (64, 64), (120, 160, 220))
        for x in range(4):          # a small magenta patch, well under threshold
            for y in range(4):
                img.putpixel((x, y), (255, 0, 255))
        path = tmp_path / "mostly_ok.png"
        img.save(path)
        result = analyze_render_health(path)
        assert result.ok is True

    def test_missing_screenshot_is_harness_failure_not_silent_pass(self, tmp_path):
        result = analyze_render_health(tmp_path / "does_not_exist.png")
        assert result.ok is False


def test_visual_comparison_capture_removes_ui_and_atmosphere_residue():
    source = (TEMPLATE_DIR / "editor" / "PlayModeSmoke.cs").read_text(encoding="utf-8")

    assert "VisualComparisonCaptureState.Begin" in source
    assert "RenderSettings.fog = false" in source
    assert "captureCam.cullingMask &= ~(1 << 5)" in source
    assert '"renderPostProcessing"' in source
    assert "token == \"canvas\"" in source
    assert "token.Contains(\"atmosphere\")" in source
    assert "captureState.Restore()" in source


# ── Opt-in real gate (mirrors tests/test_unity_compile.py exactly) ─────────

def _first_project() -> Path | None:
    if not UNITY_OUT.exists():
        return None
    for d in sorted(UNITY_OUT.iterdir()):
        if d.is_dir() and d.name != "micro" and (d / "Assets").is_dir():
            return d
    return None


@pytest.mark.unity_playtest
def test_generated_project_playtests_clean() -> None:
    from src.unity_playtest import run_playtest_phase
    from src.unity_package_cache import unity_cache_root

    if unity_cli._find_editor() is None:
        pytest.skip("No Unity Editor installed — skipping PlayMode smoke gate.")

    project = _first_project()
    if project is None:
        pytest.skip("No full Unity project under output/unity/ — generate one first.")

    schema_candidates = sorted(project.glob("*.schema.json"))
    if not schema_candidates:
        pytest.skip(f"No *.schema.json sidecar next to {project} — cannot build expectations.")

    unity_version = unity_cli._read_unity_version(project)
    compile_rc = unity_cli._run_compile_check(
        project_root=project, template_dir=TEMPLATE_DIR,
        unity_version=unity_version, timeout=1500)
    assert compile_rc == 0, f"{project.name} failed to compile — fix that before playtesting"

    editor = unity_cli._find_editor(unity_version)
    data = json.loads(schema_candidates[0].read_text(encoding="utf-8"))
    result = run_playtest_phase(
        project, UnitySchema.model_validate(data),
        template_dir=TEMPLATE_DIR, editor=editor,
        cache_dir=ROOT / "cache" / "unity_packages",
        global_cache_root=unity_cache_root(), timeout=1500,
    )
    assert result.parse.harness_ok, result.render()
    assert result.exit_code == 0, result.render()
    assert result.render_health is not None and result.render_health.ok, result.render()
