#!/usr/bin/env python3
"""
unity_cli.py  --  Unity project builder + live editor integration

Typical workflow
----------------
  # Full pipeline: LLM -> schema -> project files -> open in Unity
  python unity_cli.py new "Top-down RPG with NavMesh enemies" --open

  # Read prompt from file (long structured briefs)
  python unity_cli.py new --file prompts/neon_fugitive.txt --open

  # Re-bake a saved schema without re-running the LLM
  python unity_cli.py from-schema output/NeonFugitive_schema.json --open

  # Copy generated files into an existing Unity project + live-refresh
  python unity_cli.py overlay output/unity/NeonFugitive --target D:/Projects/NeonFugitive

  # Overlay directly into the project Unity has open right now
  python unity_cli.py overlay output/unity/NeonFugitive --running

  # Tell a running Unity Editor to re-import all changed assets
  python unity_cli.py refresh

  # Discover Unity installations on this machine
  python unity_cli.py list-editors
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import typer
import yaml

# Force UTF-8 stdout/stderr so Rich output (which uses arrows, box glyphs, etc.)
# doesn't crash with UnicodeEncodeError when redirected to a file on Windows
# (default console code page is cp1252). Harmless if already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr,attr-defined]
    except Exception:  # noqa: BLE001 — older Python / non-reconfigurable stream
        pass

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

# -- project pipeline imports --------------------------------------------------
from src.unity_pipeline import UnityPipeline, _programmatic_fix, _check_consistency
from src.unity_generator import UnityProjectGenerator
from src.unity_resolver import UnityResolver
from src.unity_schema import UnitySchema

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

_BRIDGE_HOST = "127.0.0.1"
_BRIDGE_PORT = 7890
_BRIDGE_TIMEOUT = 5.0          # seconds to wait for Unity to respond

# Files/dirs never copied during overlay
_OVERLAY_SKIP = frozenset({
    "Library", "Temp", "obj", "Logs",
    "UserSettings", ".git", ".vs", ".idea",
    "__pycache__",
})

# Unity Hub executable search paths (Windows, macOS, Linux)
_HUB_CANDIDATES: list[Path] = [
    # Windows
    Path(r"C:\Program Files\Unity Hub\UnityHub.exe"),
    Path(r"C:\Program Files (x86)\Unity Hub\UnityHub.exe"),
    # macOS
    Path("/Applications/Unity Hub.app/Contents/MacOS/Unity Hub"),
    # Linux
    Path("/usr/bin/unityhub"),
    Path(Path.home() / "Applications/Unity Hub/UnityHub"),
]

# Unity Editor root directories (inside these, subdirs are version numbers)
_EDITOR_ROOTS: list[Path] = [
    # Windows
    Path(r"C:\Program Files\Unity\Hub\Editor"),
    Path(r"C:\Program Files (x86)\Unity\Hub\Editor"),
    # macOS
    Path("/Applications/Unity/Hub/Editor"),
    # Linux
    Path(Path.home() / ".local/share/unityhub"),
    Path("/opt/unity/editors"),
]

# -----------------------------------------------------------------------------
# App / console setup
# -----------------------------------------------------------------------------

console = Console()

app = typer.Typer(
    name            = "unity-cli",
    help            = "Unity project builder -- generate, overlay, and sync with the editor.",
    add_completion  = False,
    no_args_is_help = True,
    rich_markup_mode= "rich",
)


# -----------------------------------------------------------------------------
# Unity installation discovery
# -----------------------------------------------------------------------------

@dataclass
class EditorInstall:
    version: str
    path:    Path          # full path to Unity.exe / Unity


def _find_hub() -> Optional[Path]:
    """Return the first Unity Hub executable found on this machine."""
    for candidate in _HUB_CANDIDATES:
        if candidate.exists():
            return candidate
    # Try PATH
    hub = shutil.which("unityhub") or shutil.which("UnityHub")
    return Path(hub) if hub else None


def _discover_editors() -> list[EditorInstall]:
    """Walk standard Unity Hub editor roots and return all installed versions."""
    editors: list[EditorInstall] = []
    seen: set[str] = set()

    exe_name = "Unity.exe" if sys.platform == "win32" else "Unity"

    for root in _EDITOR_ROOTS:
        if not root.is_dir():
            continue
        for version_dir in sorted(root.iterdir(), reverse=True):
            if not version_dir.is_dir():
                continue
            exe = version_dir / "Editor" / exe_name
            if exe.exists() and version_dir.name not in seen:
                seen.add(version_dir.name)
                editors.append(EditorInstall(version=version_dir.name, path=exe))

    return editors


def _find_editor(unity_version: Optional[str] = None) -> Optional[Path]:
    """
    Find the Unity Editor executable.
    If unity_version is given, prefer the closest matching install.
    Falls back to any available editor.
    """
    editors = _discover_editors()
    if not editors:
        return None

    if unity_version:
        # Exact match first
        for e in editors:
            if e.version.startswith(unity_version):
                return e.path
        # Major/minor match (e.g. "6000.0" matches "6000.0.66f2")
        major = unity_version.split(".")[0]
        for e in editors:
            if e.version.startswith(major):
                return e.path

    return editors[0].path   # most-recently-installed fallback


# -----------------------------------------------------------------------------
# Unity HTTP bridge  (talks to a running Unity Editor via unity-mcp)
# -----------------------------------------------------------------------------

class BridgeError(Exception):
    pass


def _bridge_is_alive(port: int = _BRIDGE_PORT) -> bool:
    """Return True if the Unity MCP bridge is listening on the given port."""
    try:
        req = urllib.request.Request(
            f"http://{_BRIDGE_HOST}:{port}/api/health",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_BRIDGE_TIMEOUT):
            return True
    except Exception:
        return False


def _bridge_exec(code: str, port: int = _BRIDGE_PORT) -> dict:
    """
    Execute C# code in the running Unity Editor via the MCP bridge.
    Returns the parsed JSON response dict.
    Raises BridgeError on connection failure or non-200 status.
    """
    payload = json.dumps({"code": code}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{_BRIDGE_HOST}:{port}/api/execute",
        data    = payload,
        method  = "POST",
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_BRIDGE_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BridgeError(f"HTTP {e.code}: {e.reason}") from e
    except OSError as e:
        raise BridgeError(f"Cannot reach Unity bridge on port {port}: {e}") from e


def _bridge_get_project_path(port: int = _BRIDGE_PORT) -> Optional[Path]:
    """Ask the running Unity Editor for its project root path."""
    try:
        result = _bridge_exec(
            "return UnityEngine.Application.dataPath.Replace(\"/Assets\", \"\");",
            port=port,
        )
        raw = result.get("result", "").strip().strip('"')
        if raw:
            return Path(raw)
    except BridgeError:
        pass
    return None


def _bridge_refresh(port: int = _BRIDGE_PORT, force_reimport: bool = False) -> None:
    """Trigger AssetDatabase.Refresh() (or ImportAll) in the running Unity Editor."""
    if force_reimport:
        code = "UnityEditor.AssetDatabase.ImportAllAssets(UnityEditor.ImportAssetOptions.ForceUpdate);"
    else:
        code = "UnityEditor.AssetDatabase.Refresh();"
    _bridge_exec(code, port=port)


# -----------------------------------------------------------------------------
# Overlay engine  (copy generated project -> target Unity project)
# -----------------------------------------------------------------------------

@dataclass
class OverlayStats:
    added:   int = 0
    updated: int = 0
    skipped: int = 0
    errors:  list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.added + self.updated


def _merge_manifests(src_manifest: dict, dst_manifest: dict) -> dict:
    """
    Merge source package manifest into destination.
    Adds new dependencies; never removes or downgrades existing ones.
    Preserves all other manifest keys from the destination.
    """
    result = {**dst_manifest}
    src_deps = src_manifest.get("dependencies", {})
    dst_deps = result.setdefault("dependencies", {})
    for pkg_id, version in src_deps.items():
        if pkg_id not in dst_deps:
            dst_deps[pkg_id] = version
    return result


def _overlay(
    source: Path,
    target: Path,
    *,
    force:   bool = False,
    dry_run: bool = False,
    progress: Optional[Progress] = None,
    task_id = None,
) -> OverlayStats:
    """
    Recursively copy source -> target, skipping Unity-generated directories.

    Special handling
    ----------------
    * Packages/manifest.json  -- merged rather than overwritten
    * Library/, Temp/ etc.    -- always skipped
    * .meta files             -- copied alongside their asset
    """
    stats  = OverlayStats()
    target.mkdir(parents=True, exist_ok=True)

    # Collect all source files first (for accurate progress)
    all_files: list[tuple[Path, Path]] = []   # (src, dst)
    for src_item in source.rglob("*"):
        # Skip entire skip-listed directories
        parts = set(src_item.relative_to(source).parts)
        if parts & _OVERLAY_SKIP:
            continue
        if src_item.is_dir():
            continue
        rel  = src_item.relative_to(source)
        dst_item = target / rel
        all_files.append((src_item, dst_item))

    if progress and task_id is not None:
        progress.update(task_id, total=len(all_files))

    for src_item, dst_item in all_files:
        if progress and task_id is not None:
            progress.advance(task_id)

        rel = src_item.relative_to(source)

        # -- special case: manifest merge --------------------------------------
        if rel == Path("Packages") / "manifest.json":
            if dst_item.exists() and not force:
                try:
                    src_data = json.loads(src_item.read_text("utf-8"))
                    dst_data = json.loads(dst_item.read_text("utf-8"))
                    merged   = _merge_manifests(src_data, dst_data)
                    if not dry_run:
                        dst_item.write_text(
                            json.dumps(merged, indent=2), encoding="utf-8"
                        )
                    stats.updated += 1
                    continue
                except (json.JSONDecodeError, OSError) as exc:
                    stats.errors.append(f"manifest merge failed: {exc}")
                    # Fall through to plain copy below

        # -- normal file -------------------------------------------------------
        dst_item.parent.mkdir(parents=True, exist_ok=True)
        if dst_item.exists():
            if not force:
                # Skip unchanged files (byte-exact comparison)
                try:
                    if src_item.read_bytes() == dst_item.read_bytes():
                        stats.skipped += 1
                        continue
                except OSError:
                    pass
            if not dry_run:
                try:
                    shutil.copy2(src_item, dst_item)
                except OSError as exc:
                    stats.errors.append(f"{rel}: {exc}")
                    continue
            stats.updated += 1
        else:
            if not dry_run:
                try:
                    shutil.copy2(src_item, dst_item)
                except OSError as exc:
                    stats.errors.append(f"{rel}: {exc}")
                    continue
            stats.added += 1

    return stats


# -----------------------------------------------------------------------------
# Open in Unity
# -----------------------------------------------------------------------------

def _open_project(project_path: Path, unity_version: Optional[str] = None) -> bool:
    """
    Launch the Unity Editor with the given project.
    Returns True if the process was started successfully.
    """
    editor = _find_editor(unity_version)

    if editor:
        console.print(f"  [dim]Editor:[/] {editor}")
        subprocess.Popen(
            [str(editor), "-projectPath", str(project_path.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    # Fallback: Unity Hub
    hub = _find_hub()
    if hub:
        console.print(f"  [dim]Unity Hub:[/] {hub}")
        subprocess.Popen(
            [str(hub), "--", "--open", "--projectPath", str(project_path.resolve())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    return False


# -----------------------------------------------------------------------------
# Config helper
# -----------------------------------------------------------------------------

def _load_cfg(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        console.print(f"[red]Config not found:[/] {config_path}")
        raise typer.Exit(1)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _unity_pipe_cfg(cfg: dict) -> dict:
    return cfg.get("unity_pipeline", cfg.get("pipeline", {}))


# -----------------------------------------------------------------------------
# Shared post-generation step  (overlay + open + refresh)
# -----------------------------------------------------------------------------

def _post_generate(
    generated_root: Path,
    unity_version:  Optional[str],
    *,
    target:       Optional[Path],
    open_unity:   bool,
    use_running:  bool,
    force:        bool,
    bridge_port:  int,
) -> None:
    """
    After files are written to generated_root, handle:
      1. Overlay into target directory (if --target or --running given)
      2. Refresh running Unity Editor (if bridge reachable)
      3. Open project in Unity (if --open)
    """
    # -- Resolve overlay target -------------------------------------------------
    overlay_target: Optional[Path] = None

    if use_running:
        # Ask the running Unity Editor where its project is
        proj = _bridge_get_project_path(bridge_port)
        if proj:
            overlay_target = proj
            console.print(f"\n[cyan]Detected running project:[/] {proj}")
        else:
            console.print(
                "[yellow]Could not reach Unity bridge "
                f"(port {bridge_port}) -- overlay skipped.[/]"
            )
    elif target is not None:
        overlay_target = target

    # -- Overlay ----------------------------------------------------------------
    if overlay_target is not None:
        console.print(f"\n[bold]Overlaying -> [cyan]{overlay_target}[/]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as prog:
            task = prog.add_task("Copying files…", total=None)
            stats = _overlay(
                generated_root,
                overlay_target,
                force=force,
                progress=prog,
                task_id=task,
            )

        status_parts = [f"[green]+{stats.added} added[/]"]
        if stats.updated:
            status_parts.append(f"[yellow]~{stats.updated} updated[/]")
        if stats.skipped:
            status_parts.append(f"[dim]{stats.skipped} unchanged[/]")
        console.print("  " + "  ".join(status_parts))
        for err in stats.errors:
            console.print(f"  [red]ERROR:[/] {err}")

        # -- Refresh running Unity ----------------------------------------------
        if _bridge_is_alive(bridge_port):
            console.print(f"\n[bold]Refreshing Unity Editor[/] (port {bridge_port})…", end=" ")
            try:
                _bridge_refresh(bridge_port)
                console.print("[green]OK[/]")
            except BridgeError as exc:
                console.print(f"[yellow]Skipped:[/] {exc}")
        else:
            console.print(
                f"\n[dim]Unity bridge not reachable on port {bridge_port} "
                "-- open the project in Unity to import assets.[/]"
            )

    # -- Open in Unity ----------------------------------------------------------
    if open_unity:
        launch_path = overlay_target if overlay_target else generated_root
        console.print(f"\n[bold]Opening project in Unity…[/]")
        console.print(f"  [dim]Path:[/] {launch_path}")
        ok = _open_project(launch_path, unity_version)
        if not ok:
            console.print(
                "[yellow]Unity Editor not found.[/] Open this path manually:\n"
                f"  [cyan]{launch_path}[/]"
            )


# -----------------------------------------------------------------------------
# CLI Commands
# -----------------------------------------------------------------------------

@app.command()
def new(
    prompt: Optional[str] = typer.Argument(
        None, help="Project idea in plain English."
    ),
    file: Optional[Path] = typer.Option(
        None, "--file", "-f",
        help="Read the prompt from a .txt file (for long structured briefs).",
    ),
    config: str = typer.Option(
        "config.yaml", "--config", "-c", help="Pipeline config YAML."
    ),
    output_dir: str = typer.Option(
        "", "--output-dir", "-o",
        help="Where to write generated files. Defaults to config unity_output_dir.",
    ),
    target: Optional[Path] = typer.Option(
        None, "--target", "-t",
        help="Overlay generated Assets into this existing Unity project directory.",
    ),
    running: bool = typer.Option(
        False, "--running", "-r",
        help="Overlay into the Unity project currently open in the Editor.",
    ),
    open_unity: bool = typer.Option(
        False, "--open", help="Open the project in Unity Editor after generation."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite files without asking."
    ),
    bridge_port: int = typer.Option(
        _BRIDGE_PORT, "--port", help="Unity MCP bridge port."
    ),
) -> None:
    """
    [bold]Full pipeline:[/] LLM -> schema -> Unity project files -> [optional] overlay + open.

    \b
    Examples:
      python unity_cli.py new "A 2D platformer with Cinemachine" --open
      python unity_cli.py new --file prompts/neon_fugitive.txt --open
      python unity_cli.py new "Top-down RPG" --target D:/Projects/MyRPG --running
    """
    # -- Resolve prompt ------------------------------------------------------
    if file is not None:
        if not file.exists():
            console.print(f"[red]File not found:[/] {file}")
            raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()

    if not prompt:
        console.print("[red]Provide a prompt or use[/] --file <path>")
        console.print("  [dim]python unity_cli.py new \"A 2D platformer\"[/]")
        raise typer.Exit(1)

    cfg      = _load_cfg(config)
    pipe_cfg = _unity_pipe_cfg(cfg)
    if output_dir:
        pipe_cfg["unity_output_dir"] = output_dir

    # -- Run LLM pipeline ---------------------------------------------------
    pipeline = UnityPipeline(cfg)
    generated_root = pipeline.run(prompt)

    # -- Extract unity version from generated schema for editor matching ----
    unity_version = _read_unity_version(generated_root)

    # -- Post-generation: overlay + open ------------------------------------
    _post_generate(
        generated_root,
        unity_version,
        target=target,
        open_unity=open_unity,
        use_running=running,
        force=force,
        bridge_port=bridge_port,
    )

    console.print(Panel(
        f"[bold green]Project ready[/]\n"
        f"Source:  [cyan]{generated_root}[/]\n"
        f"Open in Unity Hub or run:  [dim]python unity_cli.py open {generated_root}[/]",
        style="green",
    ))


# -----------------------------------------------------------------------------
# Headless compile verification
# -----------------------------------------------------------------------------

# Matches a Unity / Roslyn compiler error line in the editor log, e.g.
#   Assets/Scripts/Foo.cs(12,5): error CS1002: ; expected
_LOG_ERROR_RE = re.compile(
    r"(?P<file>[^\s(].*?\.cs)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>CS\d+):\s*(?P<msg>.*)"
)

# Where verify drops the isolated compile-checker inside the project.
_COMPILECHECK_DIR = "Assets/CompileCheck"


def _drop_compilecheck(project_root: Path, template_dir: Path) -> Path:
    """Copy CompileCheck.cs + asmdef into the project. Returns the dir created."""
    src_dir = template_dir / "editor"
    dst_dir = project_root / _COMPILECHECK_DIR
    dst_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("CompileCheck.cs", "CompileCheck.Editor.asmdef"):
        src = src_dir / fname
        if not src.exists():
            raise FileNotFoundError(f"Missing compile-checker template: {src}")
        shutil.copy2(src, dst_dir / fname)
    return dst_dir


def _cleanup_compilecheck(project_root: Path) -> None:
    """Remove the dropped CompileCheck dir and its .meta (best effort)."""
    dst_dir = project_root / _COMPILECHECK_DIR
    shutil.rmtree(dst_dir, ignore_errors=True)
    meta = project_root / (_COMPILECHECK_DIR + ".meta")
    try:
        meta.unlink()
    except OSError:
        pass


def _rel_cs(path_str: str, project_root: Path) -> str:
    """Normalize a compiler-message file path to project-relative forward slashes."""
    p = path_str.replace("\\", "/").strip()
    root = project_root.as_posix().rstrip("/") + "/"
    if p.startswith(root):
        p = p[len(root):]
    return p


def _short_file(rel: str) -> str:
    """Compact a long package-cache path to '<pkg>/<file>' for the error table."""
    marker = "Library/PackageCache/"
    if marker in rel:
        tail = rel.split(marker, 1)[1]
        pkg, _, rest = tail.partition("/")
        pkg = pkg.split("@", 1)[0]                  # drop the @hash suffix
        return f"{pkg}/{rest.rsplit('/', 1)[-1]}"   # pkg + filename only
    return rel


def _parse_log_errors(log_text: str, project_root: Path) -> list[dict]:
    """Extract compiler errors from the Unity editor log as a fallback to the JSON."""
    out: list[dict] = []
    for line in log_text.splitlines():
        m = _LOG_ERROR_RE.search(line)
        if m:
            out.append({
                "file": _rel_cs(m.group("file"), project_root),
                "line": int(m.group("line")),
                "message": f"{m.group('code')}: {m.group('msg')}".strip(),
            })
    return out


def _dedup_errors(errors: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in errors:
        key = (e.get("file", ""), e.get("line", 0), e.get("message", ""))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _run_compile_check(
    project_root: Path,
    template_dir: Path,
    unity_version: Optional[str],
    timeout: int,
) -> int:
    """
    Drop the compile-checker, run Unity in batchmode, and report errors.
    Returns process-style exit code: 0 = clean, 1 = compile errors, 2 = harness failure.
    """
    editor = _find_editor(unity_version)
    if editor is None:
        console.print(
            "[red]No Unity Editor found.[/] Install one via Unity Hub, then re-run.\n"
            "[dim]python unity_cli.py list-editors[/]"
        )
        return 2

    report_path = project_root / "compile_report.json"
    log_path    = project_root / "compile.log"
    # Stale artifacts from a previous run would mask a crash — clear them.
    for stale in (report_path, log_path):
        try:
            stale.unlink()
        except OSError:
            pass

    console.print(f"  [dim]Editor:[/]  {editor}")
    console.print(f"  [dim]Project:[/] {project_root}")
    if unity_version:
        console.print(f"  [dim]Version:[/] {unity_version}")

    _drop_compilecheck(project_root, template_dir)

    cmd = [
        str(editor),
        "-batchmode",
        "-nographics",
        "-projectPath", str(project_root.resolve()),
        "-executeMethod", "CompileCheck.Run",
        "-logFile", str(log_path.resolve()),
    ]

    rc: Optional[int] = None
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as prog:
            prog.add_task("Compiling in Unity (first import can take minutes)…", total=None)
            try:
                proc = subprocess.run(cmd, timeout=timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                console.print(f"[yellow]Unity batch run exceeded {timeout}s — treating as failure.[/]")
                rc = None
    finally:
        _cleanup_compilecheck(project_root)

    # -- Gather errors from both sources (JSON report + log) --------------------
    report_errors: list[dict] = []
    report_ok: Optional[bool] = None
    if report_path.exists():
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            report_ok = data.get("ok")
            report_errors = data.get("errors", [])
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"[yellow]Could not read compile_report.json:[/] {exc}")

    log_text = ""
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    log_errors = _parse_log_errors(log_text, project_root)

    errors = _dedup_errors(report_errors + log_errors)

    # -- Map files -> templates -------------------------------------------------
    tpl_map: dict[str, str] = {}
    cmap = project_root / ".compile_map.json"
    if cmap.exists():
        try:
            tpl_map = json.loads(cmap.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # -- Decide outcome ---------------------------------------------------------
    if not errors:
        if report_ok is None:
            # CompileCheck.Run's Finish() unconditionally writes compile_report.json
            # before Unity exits (templates/unity/editor/CompileCheck.cs), so a
            # MISSING report means CompileCheck never ran to completion — Unity
            # crashed, hit a license prompt, or exited during package resolution.
            # "0 errors parsed from the log" in that case is not evidence of a
            # clean compile, it's evidence nothing was actually checked. Hit live
            # twice: once when the project was already open in another Editor
            # instance (HandleProjectAlreadyOpenInAnotherInstance), and once when
            # a git-sourced UPM package failed to resolve — both silently reported
            # PASS before this check existed.
            low = log_text.lower()
            if "alreadyopeninanotherinstance" in low:
                reason = ("the project is already open in another Unity Editor "
                          "instance — close it, or point --source at a separate "
                          "copy of the project, then retry")
            elif "error occurred while resolving packages" in low or "invalid dependencies" in low:
                reason = "package resolution failed before compilation started — see compile.log"
            elif not log_text:
                reason = "Unity produced no log output — it may have failed to launch (license?)"
            else:
                reason = "see compile.log for details"
            console.print(f"[red]FAIL[/] — no compile report written: {reason}.")
            return 2
        console.print(Panel("[bold green]PASS[/] - compiles clean, 0 errors", style="green"))
        return 0

    # Whether every error is inside an imported package (not our generated code).
    all_in_pkg = all("Library/PackageCache/" in _rel_cs(e.get("file", ""), project_root)
                     for e in errors)

    # -- Print error table ------------------------------------------------------
    table = Table(title=f"{len(errors)} compile error(s)", show_lines=False)
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Ln", justify="right", style="dim")
    table.add_column("Error", style="red", no_wrap=False)
    table.add_column("Template", style="magenta", no_wrap=False)

    for e in sorted(errors, key=lambda x: (x.get("file", ""), x.get("line", 0))):
        rel = _rel_cs(e.get("file", ""), project_root)
        tpl = tpl_map.get(rel, "package (not generated code)" if "PackageCache/" in rel else "-")
        table.add_row(_short_file(rel), str(e.get("line", "")), e.get("message", ""), tpl)
    console.print(table)
    note = "  [dim](all errors are inside imported packages, not generated scripts)[/]" \
        if all_in_pkg else ""
    console.print(Panel(f"[bold red]FAIL[/] - {len(errors)} compile error(s){note}", style="red"))
    return 1


@app.command()
def verify(
    prompt: Optional[str] = typer.Argument(
        None, help="Project idea — runs the full LLM pipeline, then compile-checks it."
    ),
    source: Optional[Path] = typer.Option(
        None, "--source", "-s",
        help="Compile-check an already-generated project dir (skips the LLM).",
    ),
    file: Optional[Path] = typer.Option(
        None, "--file", "-f", help="Read the prompt from a .txt file.",
    ),
    config: str = typer.Option(
        "config.yaml", "--config", "-c", help="Pipeline config YAML."
    ),
    output_dir: str = typer.Option(
        "", "--output-dir", "-o",
        help="Where to write generated files. Defaults to config unity_output_dir.",
    ),
    timeout: int = typer.Option(
        900, "--timeout", help="Max seconds for the Unity batch compile."
    ),
) -> None:
    """
    [bold]Headless compile-check:[/] generate (or load) a Unity project, then compile it
    in Unity batchmode and report every C# error mapped back to its template.

    \b
    Examples:
      python unity_cli.py verify "A 2D platformer with Cinemachine"
      python unity_cli.py verify --source output/unity/NeonFugitive
      python unity_cli.py verify --file tests/unity_corpus.txt   # first line only
    """
    cfg      = _load_cfg(config)
    pipe_cfg = _unity_pipe_cfg(cfg)
    template_dir = Path(pipe_cfg.get("unity_template_dir", "templates/unity"))

    # -- Resolve the project to check ------------------------------------------
    if source is not None:
        if not source.is_dir():
            console.print(f"[red]Source directory not found:[/] {source}")
            raise typer.Exit(2)
        project_root = source
    else:
        if file is not None:
            if not file.exists():
                console.print(f"[red]File not found:[/] {file}")
                raise typer.Exit(2)
            prompt = file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if not prompt:
            console.print("[red]Provide a prompt, --file, or --source.[/]")
            raise typer.Exit(2)

        if output_dir:
            pipe_cfg["unity_output_dir"] = output_dir
        console.print(Panel(f"[bold]Generate -> compile-check[/]\n{prompt}", style="blue"))
        pipeline = UnityPipeline(cfg)
        project_root = pipeline.run(prompt)

    unity_version = _read_unity_version(project_root)

    console.print("\n[bold cyan]COMPILE CHECK[/]")
    rc = _run_compile_check(project_root, template_dir, unity_version, timeout)
    raise typer.Exit(rc)


@app.command("e2e-verify")
def e2e_verify(
    source: Path = typer.Argument(
        ..., help="Generated Unity project directory to verify, e.g. output/unity/MyGame."
    ),
    config: str = typer.Option(
        "config.yaml", "--config", "-c", help="Pipeline config YAML."
    ),
    timeout: int = typer.Option(
        900, "--timeout", help="Max seconds for the Unity batch compile."
    ),
    min_score: int = typer.Option(
        0, "--min-score", help="Playability: exit non-zero below this 0-100 score."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Playability: exit non-zero on any playability ERROR."
    ),
    keep: bool = typer.Option(
        False, "--keep", help="Keep the isolated verify copy even when everything passes."
    ),
) -> None:
    """
    [bold]Self-verify an E2E run:[/] real Unity compile + structural playability,
    in one command, without touching `source`.

    An E2E generation run has been failing silently for a while: the LLM ships
    a defect (unbalanced braces, a type name where a namespace belongs), it
    only surfaces as a Unity console error days later, and the only way to
    catch it has been pasting that error back into a chat session by hand.
    This closes the loop — run it right after generation and it tells you
    pass/fail on both axes generation-time checks cannot see:

    \b
    - COMPILE: the real Unity compiler (binding-level errors like CS0138
      'X is a type not a namespace', not just parse-level syntax — see
      src/csharp_syntax_gate.py's own documented boundary for why the two
      are different gates).
    - PLAYABILITY: Step 7's structural grade (avatar, objective, reachable
      content, brief coverage) against the `<name>.schema.json` sidecar
      `write()` always stamps next to `source`.

    The compile check NEVER runs against `source` itself — only an isolated
    sibling copy (`_verify_<name>` next to it). Two real failure modes hit
    live motivated this: a same-path batchmode run collides with `source`
    open in a live Editor (Unity's own
    HandleProjectAlreadyOpenInAnotherInstance crash) and *_run_compile_check
    used to misreport that as PASS* (fixed alongside this command — a
    missing compile report is now always a failure, never inferred success);
    and a copy nested deep under a long temp path can blow Windows' path
    length limit during git-sourced UPM package resolution. A short sibling
    directory dodges both. The copy is deleted on a full PASS and left in
    place (path printed) on any FAILURE, for follow-up debugging.

      python unity_cli.py e2e-verify output/unity/RepoFlowSim
      python unity_cli.py e2e-verify C:/u/RepoFlowSim --min-score 70 --strict
    """
    from src.unity_playability import evaluate, gate

    source = source.resolve()
    if not source.is_dir():
        console.print(f"[red]Source directory not found:[/] {source}")
        raise typer.Exit(2)

    schema_candidates = sorted(source.glob("*.schema.json"))
    if not schema_candidates:
        console.print(f"[red]No *.schema.json sidecar found in {source} — cannot "
                      "grade playability (write() always stamps one next to the "
                      "project).[/]")
        raise typer.Exit(2)
    schema_file = schema_candidates[0]

    cfg          = _load_cfg(config)
    pipe_cfg     = _unity_pipe_cfg(cfg)
    template_dir = Path(pipe_cfg.get("unity_template_dir", "templates/unity"))

    console.print(Panel(f"[bold]E2E self-verify[/]\n{source}", style="blue"))

    # -- Isolated copy: never batch-compile `source` directly (see docstring) --
    scratch = source.parent / f"_verify_{source.name}"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    shutil.copytree(source, scratch, ignore=shutil.ignore_patterns(*_OVERLAY_SKIP))

    console.print("\n[bold cyan]COMPILE CHECK[/]  (isolated copy — safe with source open in the Editor)")
    unity_version = _read_unity_version(scratch)
    compile_rc = _run_compile_check(scratch, template_dir, unity_version, timeout)

    console.print("\n[bold cyan]PLAYABILITY[/]")
    data   = json.loads(schema_file.read_text(encoding="utf-8"))
    report = evaluate(UnitySchema.model_validate(data))
    console.print(report.render(), markup=False)   # findings carry field paths rich reads as markup
    play_reason = gate(report, min_score=min_score, fail_on_error=strict)
    if play_reason:
        console.print(f"[red]FAIL[/] {play_reason}")

    ok = compile_rc == 0 and not play_reason
    if ok and not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    elif not ok:
        console.print(f"\n[dim]Verify copy kept for debugging: {scratch}[/]")

    summary = (f"compile={'OK' if compile_rc == 0 else 'FAIL'}  "
               f"playability={report.score}/100" +
               (f"  ({play_reason})" if play_reason else ""))
    console.print(Panel(
        f"[bold {'green' if ok else 'red'}]{'PASS' if ok else 'FAIL'}[/]  {summary}",
        style="green" if ok else "red",
    ))
    raise typer.Exit(0 if ok else 1)


@app.command("from-schema")
def from_schema(
    schema_file: Path = typer.Argument(..., help="Path to a Unity JSON schema file."),
    config: str = typer.Option(
        "config.yaml", "--config", "-c", help="Pipeline config YAML."
    ),
    output_dir: str = typer.Option(
        "", "--output-dir", "-o",
        help="Where to write generated files. Defaults to config unity_output_dir.",
    ),
    target: Optional[Path] = typer.Option(
        None, "--target", "-t",
        help="Overlay generated Assets into this existing Unity project directory.",
    ),
    running: bool = typer.Option(
        False, "--running", "-r",
        help="Overlay into the Unity project currently open in the Editor.",
    ),
    open_unity: bool = typer.Option(
        False, "--open", help="Open the project in Unity Editor after generation."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite files without asking."
    ),
    bridge_port: int = typer.Option(
        _BRIDGE_PORT, "--port", help="Unity MCP bridge port."
    ),
) -> None:
    """
    [bold]Skip the LLM:[/] recompile a saved JSON schema -> project files -> [optional] overlay + open.

    \b
    Examples:
      python unity_cli.py from-schema saved.json --open
      python unity_cli.py from-schema saved.json --running --force
    """
    if not schema_file.exists():
        console.print(f"[red]Schema file not found:[/] {schema_file}")
        raise typer.Exit(1)

    cfg      = _load_cfg(config)
    pipe_cfg = _unity_pipe_cfg(cfg)
    if output_dir:
        pipe_cfg["unity_output_dir"] = output_dir

    out_root = pipe_cfg.get("unity_output_dir", "output/unity")

    console.print(Panel(
        f"[bold]Recompile schema -> Unity project[/]\n{schema_file}",
        style="blue",
    ))

    # -- Load + resolve schema ----------------------------------------------
    console.print("\n[bold cyan]STEP 1[/] -- Load and validate schema")
    raw_data = json.loads(schema_file.read_text(encoding="utf-8"))
    schema   = UnitySchema.model_validate(raw_data)
    console.print(f"  [green]OK[/]   {schema.project_name}  "
                  f"{schema.game_type} / {schema.render_pipeline} / {schema.unity_version}")

    console.print("\n[bold cyan]STEP 2[/] -- Resolve packages")
    resolver = UnityResolver(lookups_dir=pipe_cfg.get("lookups_dir", "lookups"))
    schema   = resolver.resolve_schema(schema)
    schema   = resolver.inject_required_packages(schema)
    _inject_bridge = pipe_cfg.get("inject_mcp_bridge", True)
    deps     = resolver.build_manifest_deps(schema, inject_mcp_bridge=_inject_bridge)
    console.print(f"  [green]OK[/]   {len(deps)} packages resolved")

    console.print("\n[bold cyan]STEP 3[/] -- Programmatic fixes + consistency")
    schema = _programmatic_fix(schema)
    schema, fix_msgs = _check_consistency(schema)
    for msg in fix_msgs:
        console.print(f"  [magenta]FIX[/]  {msg}")
    schema = resolver.resolve_schema(schema)
    deps   = resolver.build_manifest_deps(schema, inject_mcp_bridge=_inject_bridge)

    console.print("\n[bold cyan]STEP 4[/] -- Generate project files")
    generator = UnityProjectGenerator(
        template_dir=pipe_cfg.get("unity_template_dir", "templates/unity")
    )
    generated_root, file_count = generator.write(
        schema, deps, out_root,
        lookups_dir=pipe_cfg.get("lookups_dir", "lookups"),
    )
    console.print(f"  [green]OK[/]   {file_count} files -> {generated_root}")

    # -- Post-generation -----------------------------------------------------
    _post_generate(
        generated_root,
        schema.unity_version,
        target=target,
        open_unity=open_unity,
        use_running=running,
        force=force,
        bridge_port=bridge_port,
    )


@app.command()
def overlay(
    source: Path = typer.Argument(
        ..., help="Generated project directory (e.g. output/unity/NeonFugitive)."
    ),
    target: Optional[Path] = typer.Option(
        None, "--target", "-t",
        help="Destination Unity project. Defaults to source (in-place).",
    ),
    running: bool = typer.Option(
        False, "--running", "-r",
        help="Auto-detect destination from the Unity project currently open in the Editor.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite unchanged files too."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would change without writing anything."
    ),
    no_refresh: bool = typer.Option(
        False, "--no-refresh", help="Skip AssetDatabase.Refresh() even if Unity is running."
    ),
    bridge_port: int = typer.Option(
        _BRIDGE_PORT, "--port", help="Unity MCP bridge port."
    ),
) -> None:
    """
    [bold]Copy[/] generated Assets/Scripts/Prefabs/Scenes from a generated project
    into an existing Unity project, then [bold]refresh[/] the running Unity Editor.

    \b
    Examples:
      # Overlay into explicit directory
      python unity_cli.py overlay output/unity/NeonFugitive --target D:/Projects/NeonFugitive

      # Overlay directly into the project Unity currently has open
      python unity_cli.py overlay output/unity/NeonFugitive --running

      # Preview changes without writing
      python unity_cli.py overlay output/unity/NeonFugitive --target D:/Projects/NeonFugitive --dry-run
    """
    if not source.is_dir():
        console.print(f"[red]Source directory not found:[/] {source}")
        raise typer.Exit(1)

    # -- Resolve destination -------------------------------------------------
    dest: Optional[Path] = target

    if running:
        proj = _bridge_get_project_path(bridge_port)
        if proj:
            dest = proj
            console.print(f"[cyan]Running project detected:[/] {proj}")
        else:
            console.print(
                f"[yellow]Unity bridge not reachable on port {bridge_port}.[/] "
                "Use [bold]--target[/] to specify the project directory."
            )
            raise typer.Exit(1)

    if dest is None:
        dest = source   # in-place (files are already there, but useful with --force)

    label = "[dim](dry run)[/] " if dry_run else ""
    console.print(Panel(
        f"{label}[bold]Overlay[/]\n"
        f"Source : [cyan]{source}[/]\n"
        f"Target : [cyan]{dest}[/]",
        style="blue",
    ))

    # -- Run overlay ---------------------------------------------------------
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as prog:
        task  = prog.add_task("Scanning files…", total=None)
        stats = _overlay(source, dest, force=force, dry_run=dry_run,
                         progress=prog, task_id=task)

    # -- Results table --------------------------------------------------------
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[green]Added[/]",    str(stats.added))
    table.add_row("[yellow]Updated[/]", str(stats.updated))
    table.add_row("[dim]Skipped[/]",    str(stats.skipped))
    if stats.errors:
        table.add_row("[red]Errors[/]", str(len(stats.errors)))
    console.print(table)
    for err in stats.errors:
        console.print(f"  [red]*[/] {err}")

    if dry_run:
        console.print("\n[dim]Dry-run complete -- no files were written.[/]")
        return

    # -- Live refresh ---------------------------------------------------------
    if not no_refresh:
        if _bridge_is_alive(bridge_port):
            console.print(f"\nRefreshing Unity Editor…", end=" ")
            try:
                _bridge_refresh(bridge_port)
                console.print("[green]OK[/]")
            except BridgeError as exc:
                console.print(f"[yellow]{exc}[/]")
        else:
            console.print(
                f"\n[dim]Unity Editor not running on port {bridge_port}. "
                "Open the project in Unity to import the new assets.[/]"
            )


@app.command("open")
def open_project(
    project_path: Path = typer.Argument(
        ..., help="Unity project directory to open."
    ),
    unity_version: Optional[str] = typer.Option(
        None, "--unity-version", "-u",
        help="Preferred Unity version (e.g. 6000.0, 2022.3).",
    ),
) -> None:
    """
    Open a Unity project in the Editor.

    \b
    Examples:
      python unity_cli.py open output/unity/NeonFugitive
      python unity_cli.py open D:/Projects/MyGame --unity-version 2022.3
    """
    if not project_path.is_dir():
        console.print(f"[red]Directory not found:[/] {project_path}")
        raise typer.Exit(1)

    if unity_version is None:
        unity_version = _read_unity_version(project_path)

    console.print(f"[bold]Opening:[/] [cyan]{project_path}[/]")
    if unity_version:
        console.print(f"[bold]Version:[/] {unity_version}")

    ok = _open_project(project_path, unity_version)
    if ok:
        console.print("[green]Unity Editor launched.[/]")
    else:
        console.print(
            "[yellow]Unity Editor not found on this machine.[/]\n"
            "Install Unity Hub from [cyan]https://unity.com/download[/] "
            "and open the project manually."
        )
        raise typer.Exit(1)


@app.command()
def refresh(
    force_reimport: bool = typer.Option(
        False, "--force",
        help="Use ImportAllAssets(ForceUpdate) instead of a normal Refresh.",
    ),
    bridge_port: int = typer.Option(
        _BRIDGE_PORT, "--port", help="Unity MCP bridge port."
    ),
) -> None:
    """
    Tell a running Unity Editor to re-import all changed assets.

    Uses AssetDatabase.Refresh() -- or AssetDatabase.ImportAllAssets(ForceUpdate)
    with [bold]--force[/].

    \b
    Example:
      python unity_cli.py refresh
      python unity_cli.py refresh --force
    """
    if not _bridge_is_alive(bridge_port):
        console.print(
            f"[red]Unity bridge not reachable on port {bridge_port}.[/]\n"
            "Make sure Unity is running with the unity-mcp plugin active."
        )
        raise typer.Exit(1)

    action = "ImportAllAssets(ForceUpdate)" if force_reimport else "Refresh()"
    console.print(f"Sending [bold]AssetDatabase.{action}[/]…", end=" ")
    try:
        _bridge_refresh(bridge_port, force_reimport=force_reimport)
        console.print("[green]OK[/]")
    except BridgeError as exc:
        console.print(f"\n[red]Error:[/] {exc}")
        raise typer.Exit(1)


@app.command("list-editors")
def list_editors() -> None:
    """
    Discover Unity Editor installations on this machine.

    \b
    Example:
      python unity_cli.py list-editors
    """
    editors = _discover_editors()
    hub     = _find_hub()

    console.print()

    # Unity Hub
    hub_text = str(hub) if hub else "[dim]not found[/]"
    console.print(f"[bold]Unity Hub:[/]  {hub_text}")
    console.print()

    if not editors:
        console.print("[yellow]No Unity Editor installations found.[/]")
        console.print(
            "Expected editors at:\n"
            + "\n".join(f"  {r}" for r in _EDITOR_ROOTS if r.is_dir() or True)
        )
        return

    table = Table(title="Installed Unity Editors", show_lines=True)
    table.add_column("Version",    style="cyan",  no_wrap=True)
    table.add_column("Path",       style="dim")
    table.add_column("Status",     justify="center")

    for editor in editors:
        status = "[green]OK[/]" if editor.path.exists() else "[red]missing[/]"
        table.add_row(editor.version, str(editor.path), status)

    console.print(table)

    # Bridge status
    console.print()
    if _bridge_is_alive(_BRIDGE_PORT):
        proj = _bridge_get_project_path(_BRIDGE_PORT)
        proj_str = str(proj) if proj else "[dim]unknown[/]"
        console.print(
            f"[green]Unity bridge alive[/] on port {_BRIDGE_PORT}  "
            f"|  active project: {proj_str}"
        )
    else:
        console.print(
            f"[dim]Unity bridge not running on port {_BRIDGE_PORT}[/]"
        )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _read_unity_version(project_root: Path) -> Optional[str]:
    """Read the Unity version from ProjectSettings/ProjectVersion.txt."""
    version_file = project_root / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.exists():
        return None
    try:
        for line in version_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("m_EditorVersion:"):
                full = line.split(":", 1)[1].strip()   # e.g. "6000.0.66f2"
                # Return the major.minor part (e.g. "6000.0")
                parts = full.split(".")
                if len(parts) >= 2:
                    return f"{parts[0]}.{parts[1]}"
                return full
    except OSError:
        pass
    return None




# -----------------------------------------------------------------------------
# GTA mission compile gate (SHVDN stub assemblies — no game SDK required)
# -----------------------------------------------------------------------------

def _gta_verify_real_dll(targets: list[Path]) -> bool:
    """Compile-check against the real, vendored ScriptHookVDotNet3.dll via
    Roslyn. Strictly stronger than the stub project: no stub-completeness
    gap, so it can't false-pass invented members the stub happens to accept
    or false-fail real ones the stub forgot. Returns True iff every target
    compiled clean. Caller has already confirmed the toolchain is available."""
    from src.gta5_compile_check import compile_check, format_findings, gate_available

    _, info = gate_available()
    console.print(Panel(
        f"[bold]GTA COMPILE GATE (real ScriptHookVDotNet3.dll)[/]\n"
        f"DLL:      [cyan]{info['dll']}[/]\n"
        f"Missions: [cyan]{len(targets)}[/]", style="blue"))

    all_ok = True
    for tgt in targets:
        ok, findings, _info = compile_check(str(tgt))
        if ok:
            console.print(f"  [green]PASS[/] {tgt.name}")
            continue
        all_ok = False
        console.print(f"  [red]FAIL[/] {tgt.name}")
        console.print(format_findings(findings))

    if not all_ok:
        console.print(Panel(
            f"[bold red]FAIL[/] — one or more missions failed to compile "
            f"against the real SHVDN3 DLL", style="red"))
        return False
    console.print(Panel(
        f"[bold green]PASS[/] — all {len(targets)} mission(s) compile clean "
        f"against the real ScriptHookVDotNet3.dll", style="green"))
    return True


def _gta_verify_stubs(targets: list[Path], stubs: Path) -> bool:
    """Compile-check against stubs/shvdn/GtaStubs.cs via `dotnet build`.
    Fallback path for machines without the real-DLL toolchain (e.g. non-
    Windows CI, which has no .NET Framework reference assemblies). Every
    stub member is a no-op, so this can miss a mismatch its author didn't
    think to stub - the real-DLL gate above doesn't have that gap. Returns
    True iff every target compiled clean."""
    import tempfile

    if shutil.which("dotnet") is None:
        console.print("[red]dotnet SDK not found on PATH — install .NET SDK 8+.[/]")
        raise typer.Exit(2)
    if not (stubs / "GtaStubs.cs").exists():
        console.print(f"[red]Stubs not found at {stubs} — expected GtaStubs.cs + MissionCheck.csproj.[/]")
        raise typer.Exit(2)

    console.print(Panel(
        f"[bold]GTA COMPILE GATE (stubs — reduced fidelity)[/]\n"
        f"Stubs:    [cyan]{stubs}[/]\n"
        f"Missions: [cyan]{len(targets)}[/]", style="yellow"))

    failures: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory(prefix="gta_gate_") as tmp:
        tmp_path = Path(tmp)
        for f in stubs.iterdir():
            if f.is_file():
                shutil.copy2(f, tmp_path / f.name)
        under_test = tmp_path / "Mission_UnderTest.cs"

        for tgt in targets:
            under_test.write_text(tgt.read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(
                ["dotnet", "build", str(tmp_path), "-nologo", "-v", "q"],
                capture_output=True, text=True)
            if proc.returncode == 0:
                console.print(f"  [green]PASS[/] {tgt.name}")
                continue
            errs = []
            for line in (proc.stdout + proc.stderr).splitlines():
                if "): error " in line:
                    line = line.replace(str(under_test), tgt.name)
                    line = line.split(" [", 1)[0].strip()
                    if line not in errs:
                        errs.append(line)
            failures[tgt.name] = errs or ["(build failed — no parsable errors)"]
            console.print(f"  [red]FAIL[/] {tgt.name}")
            for e in errs[:6]:
                console.print(f"       [dim]{e}[/]")

    if failures:
        console.print(Panel(
            f"[bold red]FAIL[/] — {len(failures)}/{len(targets)} mission(s) with compile errors",
            style="red"))
        return False
    console.print(Panel(
        f"[bold green]PASS[/] — all {len(targets)} mission(s) compile clean against SHVDN stubs",
        style="green"))
    return True


@app.command("gta-verify")
def gta_verify(
    mission: Optional[Path] = typer.Argument(
        None, help="A generated .3.cs mission file to compile-check."),
    all_missions: bool = typer.Option(
        False, "--all", help="Verify every *.3.cs under --output-dir."),
    output_dir: str = typer.Option("output", "--output-dir", "-o"),
    stubs_dir: str = typer.Option("stubs/shvdn", "--stubs",
                                  help="SHVDN stub project directory (fallback gate)."),
    stubs_only: bool = typer.Option(
        False, "--stubs-only",
        help="Skip the real-DLL gate even if available; use the stub "
             "project directly."),
) -> None:
    """Compile-check GTA missions against ScriptHookVDotNet3.

    Prefers the real, vendored ScriptHookVDotNet3.dll (stubs/shvdn/real_dll/)
    via Roslyn when the toolchain is available — 100% compiler fidelity, no
    stub-completeness gap. Falls back to the hand-written stub project
    (stubs/shvdn/GtaStubs.cs, every member a no-op) when it isn't, e.g. on a
    non-Windows machine with no .NET Framework reference assemblies. This is
    the GTA equivalent of the Unity headless verify gate.

    Examples:
      python unity_cli.py gta-verify output/AirportFigureEight.3.cs
      python unity_cli.py gta-verify --all
      python unity_cli.py gta-verify --all --stubs-only
    """
    if all_missions:
        targets = sorted(Path(output_dir).glob("*.3.cs"))
    elif mission is not None:
        targets = [mission]
    else:
        console.print("[red]Pass a mission .3.cs file or --all.[/]")
        raise typer.Exit(2)
    if not targets:
        console.print(f"[yellow]No *.3.cs files found under {output_dir}.[/]")
        raise typer.Exit(2)

    use_real_dll = False
    if not stubs_only:
        from src.gta5_compile_check import gate_available
        use_real_dll, _info = gate_available()

    ok = (_gta_verify_real_dll(targets) if use_real_dll
          else _gta_verify_stubs(targets, Path(stubs_dir)))
    if not ok:
        raise typer.Exit(1)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    app()
