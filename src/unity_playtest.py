"""
PlayMode smoke test — Step 7's live counterpart.

src/unity_playability.py grades a schema statically: it never touches a
running scene, so it cannot see a null ref in Start(), a tag that silently
failed to bind, or an entity group that spawned zero instances instead of
the requested count. This module closes that gap (docs/COMPILER_AUDIT.md
Gap C) by driving a real Unity Editor batchmode run through Unity Test
Framework's PlayMode runner (`-runTests -testPlatform PlayMode`) against
templates/unity/editor/PlayModeSmoke.cs, then parsing the NUnit3 XML it
writes.

Pure logic, no printing — mirrors src/unity_playability.py's shape.
unity_cli.py owns Console/Rich and prints `PlaytestPhaseResult.render()`,
the same way it already prints `PlayabilityReport.render()`.

The trap this module exists to avoid: naively comparing a live spawn count
against `EntitySpec.count` false-fails on ordinary, already-shipped
generator output. `build_expectations()` derives what SHOULD spawn from
the same data templates/unity/EntitySpawner.cs.j2 itself gates spawning
on — `schema.meta["entity_model_paths"]` (skip:/fx: routing), scene
scoping (`SceneSpec.entities`), the 2D archetypes' kind exclusions, and
tiled path groups' runtime-computed counts — not from the raw schema
alone. See each branch's comment for the exact template line it mirrors.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.unity_layout_archetype import pick_player_entity
from src.unity_package_cache import CacheReport, ensure_cached
from src.unity_schema import UnitySchema, is_embodied

# ── Constants ────────────────────────────────────────────────────────────────

TEST_FRAMEWORK_PACKAGE_ID = "com.unity.test-framework"
# Pinned, deliberately NOT looked up via unity_resolver's version table:
# lookups/unity/packages.json currently resolves com.unity.test-framework to
# 1.4.5/1.4.6 depending on unity_version, but only 1.1.24 is actually
# vendored under cache/unity_packages/packages.unity.com/. Resolving the
# "correct" version here would silently require network access on every
# first run, defeating the reason this pins a version at all. Its transitive
# dep com.unity.ext.nunit@1.0.6 is also vendored locally; ensure_cached()'s
# closure walk picks it up automatically from this one entry.
TEST_FRAMEWORK_VERSION = "1.1.24"

_PLAYTEST_DIR = "Assets/PlayModeSmoke"
_WARMUP_DIR = "Assets/PlayModeSmokeWarmup"
_EXPECTATIONS_FILENAME = "playtest_expectations.json"
_RESULTS_FILENAME = "playtest_results.xml"
_LOG_FILENAME = "playtest.log"
_WARMUP_LOG_FILENAME = "playtest_warmup.log"
_SCREENSHOT_FILENAME = "playtest_screenshot.png"  # must match PlayModeSmoke.ScreenshotFilename

# analyze_render_health() thresholds — heuristic starting points, not tuned
# against a corpus (there is no golden-reference corpus to tune against: every
# generated project's content is different by design). Documented here the
# same way _scatter_positions() documented its own approximation before
# Bridson's algorithm replaced it (docs/COMPILER_AUDIT.md Gap G).
_BLACK_LUMA_MAX = 12          # per-channel (0-255) cutoff for "this pixel is black"
_BLACK_FRACTION_THRESHOLD = 0.95   # this much of the frame black -> nothing rendered
_MAGENTA_TOLERANCE = 30       # per-channel distance from pure (255,0,255) still counted
_MAGENTA_FRACTION_THRESHOLD = 0.30  # this much of the frame magenta -> shader-error dominant
_ANALYSIS_MAX_DIM = 200        # downsample before analysis; only the color-fraction signal matters


# ── Expectations (what SHOULD spawn) ────────────────────────────────────────

@dataclass
class EntityExpectation:
    name: str
    expected_count: int
    exact: bool   # False = "at least" (a runtime-computed count we can't predict)


@dataclass
class PlaytestExpectations:
    locomotion: str
    check_player_tag: bool
    entities: list[EntityExpectation] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "locomotion": self.locomotion,
            "check_player_tag": self.check_player_tag,
            "entities": [
                {"name": e.name, "expected_count": e.expected_count, "exact": e.exact}
                for e in self.entities
            ],
        }


def build_expectations(schema: UnitySchema) -> PlaytestExpectations:
    """Derive per-entity-group spawn expectations from the same signals
    EntitySpawner.cs.j2 itself gates spawning on — not from `entities[i].count`
    alone. See the module docstring for why that distinction matters."""
    entity_model_paths: dict = schema.meta.get("entity_model_paths") or {}

    main_scene = schema.scenes[0] if schema.scenes else None
    scene_spec = next((s for s in schema.scene_specs if s.name == main_scene), None) \
        if main_scene else None
    # SceneSpec.entities: None (default) = every group active; [] = empty
    # scene; otherwise an explicit allow-list of entity NAMES (SceneSpec
    # docstring, src/unity_schema.py).
    scene_entities = scene_spec.entities if scene_spec is not None else None

    # EntitySpawner.cs.j2's own `{% if game_type == "2D" and not ui_board %}`
    # guard: a board/UI game (locomotion="none") always falls through to the
    # generic per-entity loop regardless of game_type, so its kind/fx:
    # exclusions never apply there. ui_board = not is_embodied(schema)
    # mirrors src/unity_generator.py's own derivation exactly (shared
    # predicate — see is_embodied()'s docstring on why there must be only one).
    ui_board = not is_embodied(schema)
    is_2d = schema.game_type == "2D" and not ui_board
    is_2d_topdown = schema.game_type == "2d_topdown" and not ui_board
    # BuildSideScroller2D excludes kind in {player,road,ground}
    # (EntitySpawner.cs.j2 ~line 923/931); BuildTopDown2D excludes only
    # kind=="player" (~line 1041). Both additionally exclude fx:-routed
    # entities — 3D's per-entity loop (~line 114) excludes neither, only skip:.
    kind_exclude: frozenset = (
        frozenset({"player", "road", "ground"}) if is_2d else
        frozenset({"player"}) if is_2d_topdown else
        frozenset()
    )

    # The player entity doesn't get a fresh "{name}_{i}" GameObject from
    # EntitySpawner like other groups — the generator merges it into the
    # scene's own pre-placed Player object (player-merge requires count==1;
    # see EntitySpawner.cs.j2's own docs). PlayerTagCardinality already
    # checks its presence, more robustly (by tag, independent of naming/merge
    # mechanics) — confirmed live: without this exclusion, EntityGroupSpawned
    # false-fails "Player: expected exactly 1, found 0" on every ordinary
    # embodied project, because no "Player_0" GameObject is ever created.
    player_entity, _why = pick_player_entity(schema.entities)
    player_name = player_entity.name if player_entity is not None else None

    out: list[EntityExpectation] = []
    for e in schema.entities:
        if e.name == player_name:
            continue
        path = entity_model_paths.get(e.name, "")
        if scene_entities is not None and e.name not in scene_entities:
            out.append(EntityExpectation(e.name, 0, True))
        elif path.startswith("skip:"):
            out.append(EntityExpectation(e.name, 0, True))
        elif (is_2d or is_2d_topdown) and (e.kind.lower() in kind_exclude or path.startswith("fx:")):
            out.append(EntityExpectation(e.name, 0, True))
        elif e.placement == "path" and e.path_width > 0:
            # SpawnTiledGroup derives the real instance count at runtime from
            # the resolved model's measured bounds vs. the path's arc length —
            # Python cannot predict it before Unity measures the mesh.
            out.append(EntityExpectation(e.name, 1, False))
        else:
            out.append(EntityExpectation(e.name, e.count, True))

    return PlaytestExpectations(
        locomotion=schema.locomotion,
        check_player_tag=is_embodied(schema),
        entities=out,
    )


def write_expectations(expectations: PlaytestExpectations, project_root: Path) -> Path:
    path = project_root / _EXPECTATIONS_FILENAME
    path.write_text(json.dumps(expectations.to_json_dict(), indent=2), encoding="utf-8")
    return path


# ── Drop-in / cleanup (mirrors unity_cli._drop_compilecheck / _cleanup_compilecheck) ──

def drop_playtest_assembly(project_root: Path, template_dir: Path) -> Path:
    """Copy PlayModeSmoke.cs + .asmdef into the project. Returns the dir created."""
    src_dir = template_dir / "editor"
    dst_dir = project_root / _PLAYTEST_DIR
    dst_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("PlayModeSmoke.cs", "PlayModeSmoke.Tests.asmdef"):
        src = src_dir / fname
        if not src.exists():
            raise FileNotFoundError(f"Missing PlayMode smoke-test template: {src}")
        shutil.copy2(src, dst_dir / fname)
    return dst_dir


def cleanup_playtest_assembly(project_root: Path) -> None:
    """Remove the dropped PlayModeSmoke dir and its .meta (best effort). Leaves
    playtest_expectations.json / playtest_results.xml / playtest.log on disk —
    debugging artifacts, same convention as compile_report.json/compile.log."""
    dst_dir = project_root / _PLAYTEST_DIR
    shutil.rmtree(dst_dir, ignore_errors=True)
    meta = project_root / (_PLAYTEST_DIR + ".meta")
    try:
        meta.unlink()
    except OSError:
        pass


def _drop_warmup_compilecheck(project_root: Path, template_dir: Path) -> Path:
    """Reuses CompileCheck.cs/.asmdef verbatim (templates/unity/editor/) for a
    throwaway warm-up pass — not a real gate (unity_cli._run_compile_check
    already ran the real one before this module was ever called). Confirmed
    live (2026-09): `-runTests -testPlatform PlayMode` can query NUnit's
    TypeCache for available tests BEFORE Unity's own reimport of a
    just-merged package (com.unity.test-framework) + a just-dropped assembly
    (PlayModeSmoke.Tests) has actually settled — a second domain reload,
    triggered by e.g. URP material upgrades, lands AFTER the test runner
    already logged its ExecutionSettings, and the run reports 0 discovered
    tests, never a failure. CompileCheck.Run() exists precisely to wait on
    CompilationPipeline.compilationFinished rather than trust batchmode/-quit
    timing — running it once, in its own process, immediately before
    -runTests launches in a SEPARATE process, guarantees that process opens
    an already-settled Library/."""
    src_dir = template_dir / "editor"
    dst_dir = project_root / _WARMUP_DIR
    dst_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("CompileCheck.cs", "CompileCheck.Editor.asmdef"):
        src = src_dir / fname
        if not src.exists():
            raise FileNotFoundError(f"Missing compile-checker template: {src}")
        shutil.copy2(src, dst_dir / fname)
    return dst_dir


def _cleanup_warmup_compilecheck(project_root: Path) -> None:
    dst_dir = project_root / _WARMUP_DIR
    shutil.rmtree(dst_dir, ignore_errors=True)
    meta = project_root / (_WARMUP_DIR + ".meta")
    try:
        meta.unlink()
    except OSError:
        pass
    report = project_root / "compile_report.json"
    try:
        report.unlink()
    except OSError:
        pass


# ── Package manifest + cache priming ────────────────────────────────────────

def merge_test_framework_manifest(
    manifest_path: Path,
    package_id: str = TEST_FRAMEWORK_PACKAGE_ID,
    version: str = TEST_FRAMEWORK_VERSION,
) -> bool:
    """Insert `package_id` into manifest_path's "dependencies" if absent.
    Never downgrades or overwrites an already-pinned version. Returns True iff
    the file was modified. No existing helper does this (unity_resolver.
    build_manifest_deps only builds a fresh in-memory dict pre-generation) —
    intentionally standalone rather than importing unity_cli's overlay-merge
    helper, which merges two whole manifests for a different command."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    deps = data.setdefault("dependencies", {})
    if package_id in deps:
        return False
    deps[package_id] = version
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def prime_test_framework_cache(
    cache_dir: Path, global_root: Optional[Path] = None,
) -> CacheReport:
    """Ensure com.unity.test-framework is in Unity's global package cache so
    batchmode resolves it offline. download_missing=False: the whole point of
    the pinned version above is that it's already vendored locally — this
    should never need network."""
    return ensure_cached(
        {TEST_FRAMEWORK_PACKAGE_ID: TEST_FRAMEWORK_VERSION},
        cache_dir=cache_dir, global_root=global_root, download_missing=False,
    )


def test_framework_is_editor_bundled(editor: Path) -> bool:
    """True when this specific Editor install already ships com.unity.
    test-framework as a built-in package (confirmed live, 2026-09: Unity
    6000.1.4f1 does — Editor/Data/Resources/PackageManager/BuiltInPackages/
    com.unity.test-framework — the same mechanism URP/ugui use).

    Matters because forcing the manifest merge below when it's unneeded
    doesn't just waste a step: on a bundled editor it drags in
    com.unity.test-framework.performance as an extra resolved dependency,
    whose IPrebuildSetup (Unity.PerformanceTesting.Editor.TestRunBuilder)
    logs a spurious '[Error] FindAllObjectsOfType: The type has to be
    derived from UnityEngine.Object' for any plain (non-MonoBehaviour)
    [RuntimeInitializeOnLoadMethod] class in the project (confirmed live
    against AtmosphereBootstrap.cs.j2) — noise from a package we never
    needed, not a defect in the generated project. Best-effort: an
    unexpected editor layout (non-Windows, or a future Unity restructuring)
    falls through to False, which just means the merge below still runs —
    the safe, previously-only behavior — never a hard failure."""
    try:
        return (editor.parent / "Data" / "Resources" / "PackageManager"
                / "BuiltInPackages" / TEST_FRAMEWORK_PACKAGE_ID).is_dir()
    except OSError:
        return False


# ── Render health (Gap C solution #4 — "does it look right") ──────────────────

@dataclass
class RenderHealthResult:
    ok: bool
    reason: str = ""
    black_fraction: float = 0.0
    magenta_fraction: float = 0.0


def analyze_render_health(png_path: Path) -> RenderHealthResult:
    """Structural checks on the screenshot PlayModeSmoke.CaptureMainCameraScreenshot
    captures — the same spirit as tests/test_blender_render.py's manifold checks:
    not a comparison against a golden reference image (there is no fixed
    "correct" render for an arbitrary LLM-generated project — every generation
    is different by design), but detection of the specific, unambiguous shapes
    a broken render actually takes:
      - solid black: nothing rendered (dead/misaimed camera, no graphics device)
      - Unity's own magenta missing-shader color, dominant: a material/shader
        failed to compile or load

    Mirrors Unity's own mechanism/judgment split — CaptureMainCameraScreenshot
    (C#) owns capturing the pixels; this owns judging them, matching how Step 7
    (src/unity_playability.py) grades the schema in Python, never in-engine."""
    if not png_path.exists():
        return RenderHealthResult(ok=False, reason=f"no screenshot written: {png_path}")

    from PIL import Image

    try:
        img = Image.open(png_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001 — any Pillow decode failure is the same "abstain" case
        return RenderHealthResult(ok=False, reason=f"could not open screenshot: {exc}")

    img.thumbnail((_ANALYSIS_MAX_DIM, _ANALYSIS_MAX_DIM))
    get_data = getattr(img, "get_flattened_data", img.getdata)  # Pillow >=11; getdata deprecated 14
    pixels = list(get_data())
    total = len(pixels)
    if total == 0:
        return RenderHealthResult(ok=False, reason="screenshot has zero pixels")

    black = sum(1 for (r, g, b) in pixels
                if r <= _BLACK_LUMA_MAX and g <= _BLACK_LUMA_MAX and b <= _BLACK_LUMA_MAX)
    magenta = sum(1 for (r, g, b) in pixels
                  if abs(r - 255) <= _MAGENTA_TOLERANCE and g <= _MAGENTA_TOLERANCE
                  and abs(b - 255) <= _MAGENTA_TOLERANCE)
    black_fraction = black / total
    magenta_fraction = magenta / total

    if black_fraction >= _BLACK_FRACTION_THRESHOLD:
        return RenderHealthResult(
            ok=False,
            reason=f"{black_fraction:.0%} of the frame is solid black — nothing rendered "
                   "(camera pointed at empty space, or no graphics device)",
            black_fraction=black_fraction, magenta_fraction=magenta_fraction)
    if magenta_fraction >= _MAGENTA_FRACTION_THRESHOLD:
        return RenderHealthResult(
            ok=False,
            reason=f"{magenta_fraction:.0%} of the frame is Unity's magenta "
                   "missing-shader color — a material/shader failed to compile or load",
            black_fraction=black_fraction, magenta_fraction=magenta_fraction)
    return RenderHealthResult(ok=True, black_fraction=black_fraction, magenta_fraction=magenta_fraction)


# ── NUnit3 XML results ──────────────────────────────────────────────────────

@dataclass
class TestCaseOutcome:
    name: str
    outcome: str  # "passed" | "failed" | "skipped"
    message: str = ""


@dataclass
class PlaytestParseResult:
    harness_ok: bool
    reason: str = ""
    outcomes: list[TestCaseOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[TestCaseOutcome]:
        return [o for o in self.outcomes if o.outcome == "failed"]


def parse_nunit_results(xml_path: Path) -> PlaytestParseResult:
    """A missing/malformed/empty results file is ALWAYS a harness failure,
    never inferred PASS — the same rule unity_cli._run_compile_check already
    enforces for a missing compile_report.json (a crash, license prompt, or
    a compile error inside the dropped test assembly itself all look like
    "no XML written", and none of them mean the checks passed)."""
    if not xml_path.exists():
        return PlaytestParseResult(harness_ok=False,
                                    reason=f"no test-results file written: {xml_path}")
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        return PlaytestParseResult(harness_ok=False,
                                    reason=f"could not parse {xml_path}: {exc}")

    outcomes: list[TestCaseOutcome] = []
    for case in root.iter("test-case"):
        name = case.get("name", "?")
        result = (case.get("result") or "").strip().lower()
        if result == "passed":
            outcome = "passed"
        elif result in ("skipped", "ignored"):
            outcome = "skipped"
        else:
            outcome = "failed"
        message = ""
        if outcome == "failed":
            msg_el = case.find("./failure/message")
            if msg_el is not None and msg_el.text:
                message = msg_el.text.strip()
        outcomes.append(TestCaseOutcome(name=name, outcome=outcome, message=message))

    if not outcomes:
        return PlaytestParseResult(
            harness_ok=False,
            reason="0 <test-case> elements in test-results — PlayModeSmoke never "
                   "ran to completion (compile error inside the dropped assembly, "
                   "license prompt, or crash — see playtest.log)")
    return PlaytestParseResult(harness_ok=True, outcomes=outcomes)


# ── Orchestration ────────────────────────────────────────────────────────────

@dataclass
class PlaytestPhaseResult:
    exit_code: int   # 0 clean, 1 test failure(s), 2 harness failure
    parse: PlaytestParseResult
    log_path: Optional[Path] = None
    cache_report: Optional[CacheReport] = None
    render_health: Optional[RenderHealthResult] = None
    screenshot_path: Optional[Path] = None

    def render(self) -> str:
        """Plain-text summary, worst-first — mirrors PlayabilityReport.render()'s
        convention (unity_cli.py prints this the same way, via console.print(...,
        markup=False))."""
        if not self.parse.harness_ok:
            return f"PLAYTEST HARNESS FAILURE — {self.parse.reason}"
        n_pass = sum(1 for o in self.parse.outcomes if o.outcome == "passed")
        n_fail = len(self.parse.failures)
        if self.render_health is not None and not self.render_health.ok:
            n_fail += 1
        n_skip = sum(1 for o in self.parse.outcomes if o.outcome == "skipped")
        lines = [f"PLAYTEST {n_pass} passed, {n_fail} failed, {n_skip} skipped"]
        for o in sorted(self.parse.failures, key=lambda o: o.name):
            msg = o.message.splitlines()[0] if o.message else ""
            lines.append(f"  FAIL  {o.name}: {msg}")
        if self.render_health is not None:
            if not self.render_health.ok:
                lines.append(f"  FAIL  RenderedFrameHealth: {self.render_health.reason}")
            elif self.screenshot_path is not None:
                lines.append(f"  OK    RenderedFrameHealth: looks fine "
                              f"(black={self.render_health.black_fraction:.0%}, "
                              f"magenta={self.render_health.magenta_fraction:.0%}) "
                              f"— {self.screenshot_path}")
        if self.cache_report and self.cache_report.failed:
            lines.append(
                "  WARN  package cache priming failed for: "
                f"{', '.join(self.cache_report.failed)} — batchmode run may have "
                "needed network")
        return "\n".join(lines)


def run_playtest_phase(
    project_root: Path,
    schema: UnitySchema,
    *,
    template_dir: Path,
    editor: Path,
    cache_dir: Path,
    global_cache_root: Optional[Path] = None,
    timeout: int = 900,
) -> PlaytestPhaseResult:
    """Drop the PlayMode test assembly, run Unity in batchmode against
    `project_root`, and report pass/fail. Caller resolves `editor` (mirrors
    every _run_compile_check call site — this module never imports
    unity_cli, which would be circular)."""
    project_root = Path(project_root)
    results_path = project_root / _RESULTS_FILENAME
    log_path = project_root / _LOG_FILENAME
    screenshot_path = project_root / _SCREENSHOT_FILENAME
    # Stale artifacts from a previous run would mask a crash — clear them,
    # same as unity_cli._run_compile_check does for compile_report.json/compile.log.
    for stale in (results_path, log_path, screenshot_path):
        try:
            stale.unlink()
        except OSError:
            pass

    expectations = build_expectations(schema)
    write_expectations(expectations, project_root)

    # Only pin/prime an explicit package version when this Editor install
    # doesn't already ship one built in — see test_framework_is_editor_bundled's
    # docstring for why forcing it unconditionally is worse than a no-op on a
    # bundled editor (confirmed live: drags in a noisy, unneeded companion
    # package). cache_report stays None on the bundled path — nothing was
    # primed, so there is nothing to report.
    cache_report: Optional[CacheReport] = None
    if not test_framework_is_editor_bundled(editor):
        manifest_path = project_root / "Packages" / "manifest.json"
        if manifest_path.exists():
            merge_test_framework_manifest(manifest_path)
        cache_report = prime_test_framework_cache(cache_dir, global_cache_root)

    drop_playtest_assembly(project_root, template_dir)
    try:
        # Warm-up pass in its OWN process launch first — see
        # _drop_warmup_compilecheck's docstring for why -runTests races an
        # in-flight reimport of the package/assembly just added above.
        _drop_warmup_compilecheck(project_root, template_dir)
        try:
            warmup_cmd = [
                str(editor),
                "-batchmode", "-nographics",
                "-projectPath", str(project_root.resolve()),
                "-executeMethod", "CompileCheck.Run",
                "-logFile", str((project_root / _WARMUP_LOG_FILENAME).resolve()),
            ]
            try:
                subprocess.run(warmup_cmd, timeout=timeout)
            except subprocess.TimeoutExpired:
                pass  # best-effort — -runTests below still runs regardless
        finally:
            _cleanup_warmup_compilecheck(project_root)

        cmd = [
            str(editor),
            # No -nographics here (unlike the warm-up pass above): Gap C
            # solution #4's CaptureMainCameraScreenshot needs a real graphics
            # device to render a frame into. -batchmode alone still runs with
            # no visible window — this is Unity's own standard way to capture
            # screenshots in batchmode PlayMode tests.
            "-batchmode",
            "-projectPath", str(project_root.resolve()),
            "-runTests", "-testPlatform", "PlayMode",
            "-testResults", str(results_path.resolve()),
            "-logFile", str(log_path.resolve()),
        ]
        # No -quit: the test runner owns Editor exit itself once the run
        # completes, same principle as CompileCheck.Run() calling
        # EditorApplication.Exit() directly instead of relying on a flag.
        try:
            subprocess.run(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            pass  # fall through — parse_nunit_results reports the missing/partial file
    finally:
        cleanup_playtest_assembly(project_root)

    parse = parse_nunit_results(results_path)
    # CaptureMainCameraScreenshot's own NUnit result already covers "did the
    # capture mechanism work" (parse.failures catches a null/empty texture);
    # this is the deeper, separate judgment of what the pixels actually show,
    # only meaningful once a screenshot genuinely exists.
    render_health = analyze_render_health(screenshot_path) if screenshot_path.exists() else None

    if not parse.harness_ok:
        exit_code = 2
    elif parse.failures or (render_health is not None and not render_health.ok):
        exit_code = 1
    else:
        exit_code = 0

    return PlaytestPhaseResult(
        exit_code=exit_code, parse=parse, log_path=log_path, cache_report=cache_report,
        render_health=render_health,
        screenshot_path=screenshot_path if screenshot_path.exists() else None,
    )
