"""
FastAPI web app — local control panel + generation UI for the Mission &
Project Creator.

The server is a thin wrapper: it imports and runs the existing pipelines
(MissionPipeline, UnityPipeline, micro REGISTRY) rather than reimplementing
logic. The CLI (`python main.py ...`) keeps working unchanged.

Live progress: the pipelines log through a module-level Rich `console`. For a
run we swap those module consoles for one that writes into a thread-safe queue,
run the pipeline in a background thread, and tail the queue out over SSE.

Run launch with: `python main.py serve` (see main.py) or
`uvicorn webui.server:app`.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rich.console import Console

import src.pipeline as gta_pipeline
import src.unity_pipeline as unity_pipeline
import src.micro_pipeline as micro_pipeline
import src.resolver as resolver_mod
from src.backends import check_chat, check_rag, active_chat_cfg, rag_enabled
from src.errors import classify, catalog_as_dicts
from src.themes import list_themes
from src.micro import REGISTRY as MICRO_REGISTRY

import setup_anythingllm

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Mission & Project Creator")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def _mask(cfg: dict) -> dict:
    """Return a copy with the AnythingLLM API key masked for display."""
    import copy
    masked = copy.deepcopy(cfg)
    key = masked.get("anythingllm", {}).get("api_key", "")
    if key:
        masked["anythingllm"]["api_key"] = "••••••" + key[-4:] if len(key) > 4 else "••••••"
    masked["anythingllm"]["api_key_set"] = bool(key)
    return masked


# ── Live-progress capture ──────────────────────────────────────────────────────

class _QueueWriter:
    """File-like object: buffers writes and pushes completed lines to a queue."""

    def __init__(self, q: "queue.Queue[Optional[str]]"):
        self.q = q
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(line)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self.q.put(self._buf)
            self._buf = ""


# Modules whose module-level `console` we redirect during a run.
_CONSOLE_MODULES = [gta_pipeline, unity_pipeline, micro_pipeline, resolver_mod]

# Serialize runs: the console swap is process-global, so one run at a time.
_run_lock = threading.Lock()

# run_id -> {"queue": Queue, "result": dict|None, "done": bool}
_runs: dict[str, dict[str, Any]] = {}


# ── Branding (single source of truth — rebrand here when entering academia) ──
APP_NAME    = "Bacon & Eggs"
APP_TAGLINE = "Describe a game — get a wired Unity project, composed from parts."


class GenerateRequest(BaseModel):
    mode: str                      # "unity" | "gta" | micro kind (controller, camera, ...)
    prompt: str
    theme: Optional[str] = None
    mashup_with: Optional[str] = None
    variant: Optional[str] = None
    seed: Optional[int] = None
    compose: bool = False          # unity mode: assemble micros into a wired project
    model: Optional[str] = None    # override the configured LM Studio model for this run
    extra_args: dict[str, Any] = {}  # parsed flags from the Prompt page's Custom Args field


def _apply_extra_args(cfg: dict, args: dict) -> None:
    """
    Apply one-shot Custom Args flags to a config dict in-place.

    Each flag listed in the Advanced help table maps to a real config knob:
      --show-prompts          → cfg.logging.show_prompts = True
      --max-tokens=<int>      → cfg.lm_studio.max_tokens
      --temperature=<float>   → cfg.lm_studio.concept_temperature
      --basic                 → cfg.basic_mode = True (forces RAG off for this run)
    (--debug and --verbose are handled separately — they change log level, not config.)
    """
    if args.get("show_prompts") or args.get("show-prompts"):
        cfg.setdefault("logging", {})["show_prompts"] = True
    if args.get("basic"):
        cfg["basic_mode"] = True
    mt = args.get("max_tokens") or args.get("max-tokens")
    if mt is not None:
        try:
            cfg.setdefault("lm_studio", {})["max_tokens"] = int(mt)
        except (TypeError, ValueError):
            pass
    tmp = args.get("temperature")
    if tmp is not None:
        try:
            cfg.setdefault("lm_studio", {})["concept_temperature"] = float(tmp)
        except (TypeError, ValueError):
            pass


def _run_pipeline(run_id: str, req: GenerateRequest) -> None:
    q: "queue.Queue[Optional[str]]" = _runs[run_id]["queue"]
    qwriter = _QueueWriter(q)
    capture = Console(file=qwriter, force_terminal=False, soft_wrap=True)

    # Honor --debug / --verbose from the Prompt page's Custom Args field by
    # bumping the root log level for the duration of the run.
    args = req.extra_args or {}
    log_level = (
        logging.DEBUG if args.get("debug")
        else logging.INFO if args.get("verbose")
        else None
    )

    # Pipe Python `logging` records into the same SSE queue as Rich console
    # output so --debug streams raw LLM I/O into the in-app console (was
    # previously visible only on uvicorn's stdout / the PowerShell window).
    log_handler = logging.StreamHandler(stream=qwriter) if log_level is not None else None
    if log_handler is not None:
        log_handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
        log_handler.setLevel(log_level)

    with _run_lock:
        originals = [m.console for m in _CONSOLE_MODULES]
        for m in _CONSOLE_MODULES:
            m.console = capture
        root_logger = logging.getLogger()
        prev_level = root_logger.level
        if log_level is not None:
            root_logger.setLevel(log_level)
            root_logger.addHandler(log_handler)
        try:
            cfg = load_config()
            _apply_extra_args(cfg, args)
            if req.model:
                cfg.setdefault("lm_studio", {})["model"] = req.model
            if req.mode == "gta":
                out = gta_pipeline.MissionPipeline(cfg).run(
                    req.prompt, theme=req.theme,
                    mashup_with=req.mashup_with, seed=req.seed,
                )
            elif req.mode == "unity":
                out = unity_pipeline.UnityPipeline(cfg).run(
                    req.prompt, theme=req.theme,
                    mashup_with=req.mashup_with, seed=req.seed,
                    compose=req.compose,
                )
            elif req.mode in MICRO_REGISTRY:
                out = MICRO_REGISTRY[req.mode](cfg).run(
                    req.prompt, variant=req.variant, theme=req.theme,
                    mashup_with=req.mashup_with, seed=req.seed, out=None,
                )
            else:
                raise ValueError(f"Unknown mode: {req.mode}")
            # Always hand the browser an absolute path so "Copy path" / "Open
            # file location" work against the real disk location.
            _runs[run_id]["result"] = {
                "ok":     True,
                "output": str(Path(out).resolve()),
                "mode":   req.mode,
            }
        except Exception as exc:  # noqa: BLE001 — surface as classified error
            _runs[run_id]["result"] = {"ok": False, "error": classify(exc)}
        finally:
            for m, orig in zip(_CONSOLE_MODULES, originals):
                m.console = orig
            if log_handler is not None:
                root_logger.removeHandler(log_handler)
            if log_level is not None:
                root_logger.setLevel(prev_level)
            _runs[run_id]["done"] = True
            q.put(None)  # sentinel


# ── API ─────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config() -> dict:
    return _mask(load_config())


@app.post("/api/config")
def post_config(patch: dict) -> dict:
    """Shallow-merge a config patch and persist. An empty/placeholder api_key is ignored."""
    cfg = load_config()
    for section, value in patch.items():
        if isinstance(value, dict) and isinstance(cfg.get(section), dict):
            for k, v in value.items():
                # Don't overwrite a stored key with the masked placeholder.
                if section == "anythingllm" and k == "api_key" and (v is None or "••" in str(v)):
                    continue
                cfg[section][k] = v
        else:
            cfg[section] = value
    save_config(cfg)
    return _mask(cfg)


@app.get("/api/status")
def get_status() -> dict:
    cfg = load_config()
    return {"chat": check_chat(cfg), "rag": check_rag(cfg)}


@app.post("/api/test/{target}")
def test_connection(target: str) -> dict:
    cfg = load_config()
    if target == "chat":
        return check_chat(cfg)
    if target == "rag":
        return check_rag(cfg)
    raise HTTPException(status_code=404, detail=f"Unknown target: {target}")


@app.post("/api/setup-workspace")
def setup_workspace(target: str = "both") -> dict:
    """target ∈ {'both', 'gta', 'unity'} — controls which workspace(s) to bootstrap."""
    if target not in ("both", "gta", "unity"):
        raise HTTPException(status_code=400, detail=f"Unknown target: {target}")
    return setup_anythingllm.bootstrap(load_config(), target=target)


# ── Library / Ratings / Open / Tutorial ───────────────────────────────────────

def _output_dirs(cfg: dict) -> tuple[Path, Path]:
    gta = ROOT / cfg.get("pipeline", {}).get("output_dir", "output")
    unity = ROOT / cfg.get("unity_pipeline", {}).get("unity_output_dir", "output/unity")
    return gta, unity


def _ratings_path(cfg: dict) -> Path:
    gta, _ = _output_dirs(cfg)
    gta.mkdir(parents=True, exist_ok=True)
    return gta / ".ratings.json"


def _load_ratings(cfg: dict) -> dict[str, int]:
    p = _ratings_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — bad rating file shouldn't crash the UI
        return {}


_ratings_lock = threading.Lock()


def _read_unity_concept(project_root: Path) -> tuple[str, str]:
    """Read a Unity project's .concept.txt sidecar. Returns (concept, user_prompt)."""
    sidecar = project_root / ".concept.txt"
    if not sidecar.exists():
        return "", ""
    try:
        txt = sidecar.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return "", ""
    # Sidecar layout: "# <title>\n\n<user_prompt>\n\n---\n\n<concept>\n"
    parts = txt.split("\n---\n", 1)
    head = parts[0]
    concept = parts[1].strip() if len(parts) == 2 else ""
    user_prompt = ""
    for line in head.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        user_prompt = line.strip()
        break
    return concept, user_prompt


def _read_micro_concept(out_dir: Path) -> tuple[str, str]:
    """Read a micro generator's *.schema.json meta.concept."""
    for schema_path in out_dir.glob("*.schema.json"):
        try:
            data = json.loads(schema_path.read_text(encoding="utf-8"))
            meta = data.get("meta") or {}
            return meta.get("concept") or "", meta.get("user_prompt") or ""
        except Exception:  # noqa: BLE001
            continue
    return "", ""


@app.get("/api/library/gta")
def get_library_gta() -> list[dict]:
    """List GTA5 outputs: every *.schema.json paired with its *.3.cs sibling."""
    cfg = load_config()
    gta_dir, _ = _output_dirs(cfg)
    ratings = _load_ratings(cfg)
    items: list[dict] = []
    if not gta_dir.exists():
        return items
    for schema_path in sorted(gta_dir.glob("*.schema.json")):
        cs_path = schema_path.with_suffix("").with_suffix(".3.cs")
        title = schema_path.stem.replace(".schema", "")
        concept = ""
        user_prompt = ""
        try:
            data = json.loads(schema_path.read_text(encoding="utf-8"))
            title = data.get("mission_name") or title
            meta = data.get("meta") or {}
            concept = meta.get("concept") or ""
            user_prompt = meta.get("user_prompt") or ""
        except Exception:  # noqa: BLE001
            pass
        items.append({
            "title":       title,
            "schema":      str(schema_path),
            "script":      str(cs_path) if cs_path.exists() else None,
            "mtime":       schema_path.stat().st_mtime,
            "rating":      ratings.get(str(schema_path), 0),
            "concept":     concept,
            "user_prompt": user_prompt,
        })
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


@app.get("/api/library/unity")
def get_library_unity() -> list[dict]:
    """List Unity outputs: every project folder under output/unity/ (excluding 'micro')."""
    cfg = load_config()
    _, unity_dir = _output_dirs(cfg)
    ratings = _load_ratings(cfg)
    items: list[dict] = []
    if not unity_dir.exists():
        return items
    for child in sorted(unity_dir.iterdir()):
        if not child.is_dir() or child.name == "micro":
            continue
        concept, user_prompt = _read_unity_concept(child)
        items.append({
            "title":       child.name,
            "path":        str(child),
            "mtime":       child.stat().st_mtime,
            "rating":      ratings.get(str(child), 0),
            "kind":        "project",
            "concept":     concept,
            "user_prompt": user_prompt,
        })
    # Roll up micro/<kind>/<name> as second-class rows.
    micro_root = unity_dir / "micro"
    if micro_root.exists():
        for kind_dir in sorted(micro_root.iterdir()):
            if not kind_dir.is_dir():
                continue
            for child in sorted(kind_dir.iterdir()):
                if not child.is_dir():
                    continue
                concept, user_prompt = _read_micro_concept(child)
                items.append({
                    "title":       f"{kind_dir.name}: {child.name}",
                    "path":        str(child),
                    "mtime":       child.stat().st_mtime,
                    "rating":      ratings.get(str(child), 0),
                    "kind":        f"micro/{kind_dir.name}",
                    "concept":     concept,
                    "user_prompt": user_prompt,
                })
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


class RatingUpdate(BaseModel):
    path: str
    rating: int  # 0..5 ; 0 clears


@app.get("/api/ratings")
def get_ratings() -> dict[str, int]:
    return _load_ratings(load_config())


@app.post("/api/ratings")
def set_rating(update: RatingUpdate) -> dict:
    if not (0 <= update.rating <= 5):
        raise HTTPException(status_code=400, detail="rating must be 0..5")
    cfg = load_config()
    with _ratings_lock:
        data = _load_ratings(cfg)
        if update.rating == 0:
            data.pop(update.path, None)
        else:
            data[update.path] = update.rating
        _ratings_path(cfg).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"ok": True, "rating": update.rating}


class OpenRequest(BaseModel):
    path: str


@app.post("/api/open")
def open_path(req: OpenRequest) -> dict:
    """Reveal a file or folder in the OS file manager. Refuses paths outside the project."""
    target = Path(req.path).resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path is outside the project root")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path does not exist")
    try:
        if sys.platform.startswith("win"):
            # /select, reveals + highlights the file in Explorer; for folders, just open.
            if target.is_file():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target))   # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if target.is_file() else "", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Open failed: {exc}")
    return {"ok": True, "opened": str(target)}


@app.get("/api/tutorial")
def get_tutorial() -> dict:
    tut = Path(__file__).resolve().parent / "tutorial.json"
    return json.loads(tut.read_text(encoding="utf-8"))


# ── Themes / modes / errors ───────────────────────────────────────────────────

@app.get("/api/themes")
def get_themes() -> list[dict]:
    return [{"name": t.name, "display": t.display} for t in list_themes()]


@app.get("/api/meta")
def get_meta() -> dict:
    """Product branding — the frontend reads name/tagline from here so a rebrand
    is a one-line change in this file."""
    return {"name": APP_NAME, "tagline": APP_TAGLINE}


# One-line description per micro kind (served so the UI never drifts from the
# registry the way the old hardcoded app.js map did).
_MICRO_BLURB: dict[str, str] = {
    "controller":   "Player movement (FPS, third-person, top-down, 2D, vehicle, flying, swim).",
    "camera":       "Camera rig (follow, orbit, first-person, Cinemachine, RTS, dolly).",
    "physics":      "Physics body (rigidbody, kinematic, ragdoll, vehicle, trigger, buoyancy).",
    "hud":          "On-screen HUD (health bar, ammo, minimap, crosshair, prompt, pause).",
    "input":        "Input System action map + reader (keyboard, gamepad, touch, VR).",
    "audio":        "Audio system (2D/3D, music layers, crossfade zones, mixer).",
    "ai":           "Enemy AI (NavMesh patrol/chase, FSM, behaviour tree, flocking, cover).",
    "spawner":      "Spawner (waves, object pool, trigger, point, radial).",
    "saveload":     "Save/load (JSON file, PlayerPrefs, slots, checkpoint, autosave).",
    "inventory":    "Inventory (slot, grid, weight, equipment, hotbar).",
    "dialogue":     "Dialogue (linear, branching, typewriter, cutscene, barks).",
    "menu":         "Menu UI (main, pause, settings, game-over, level select).",
    "interactable": "Interactable (pickup, door, lever, switch, NPC, readable).",
    "transition":   "Scene transition (instant, fade, loading bar, async, trigger door).",
    "health":       "Health system (simple, regenerating, shielded, armor, lives).",
}


@app.get("/api/modes")
def get_modes() -> dict:
    """Generation modes + the micro variants the UI populates its dropdowns from."""
    micro = {
        kind: {
            "display":  cls.display_name,
            "variants": list(cls.variants),
            "help":     _MICRO_BLURB.get(kind, cls.display_name),
        }
        for kind, cls in MICRO_REGISTRY.items()
    }
    return {"core": ["unity", "gta"], "micro": micro}


@app.get("/api/errors")
def get_errors() -> list[dict]:
    return catalog_as_dicts()


@app.post("/api/generate")
def start_generate(req: GenerateRequest) -> dict:
    run_id = uuid.uuid4().hex
    _runs[run_id] = {"queue": queue.Queue(), "result": None, "done": False}
    threading.Thread(target=_run_pipeline, args=(run_id, req), daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/generate/{run_id}/stream")
def stream_generate(run_id: str) -> StreamingResponse:
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Unknown run")

    def event_stream():
        import json as _json
        q: "queue.Queue[Optional[str]]" = _runs[run_id]["queue"]
        while True:
            line = q.get()
            if line is None:
                result = _runs[run_id]["result"] or {"ok": False}
                yield f"event: done\ndata: {_json.dumps(result)}\n\n"
                break
            yield f"data: {_json.dumps({'line': line})}\n\n"
        _runs.pop(run_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Static frontend (mounted last so /api/* wins) ───────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
