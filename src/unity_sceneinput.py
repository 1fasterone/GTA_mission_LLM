"""
Deterministic scene-layout front door (the `unity-scene` CLI command, main.py):
turns a **Scene Compiler** layout — the browser tool at
`Documents/Blender/` that arranges FBX/GLB models on a grid and saves
`layouts/<name>.json` — into a canonical SCENE FILE that
`unity-create --scene` feeds to the pipeline.

The sibling of src/unity_pathinput.py: that module authors a *route*, this one
authors a whole *cast + placement*. Neither ever calls an LLM.

What the scene file is FOR
--------------------------
A layout says "this exact model, at this exact spot, facing this way". The
pipeline already has a slot for every one of those facts:

    layout item .file       -> the entity's resolved model  (Tier A, below)
    layout item .position   -> EntitySpec.instance_positions
    layout item .yaw_deg    -> EntitySpec.instance_yaws
    layout item .scale      -> EntitySpec.scale
    N items of one file     -> one EntitySpec with count = N

So NO schema field is invented here: authored placement rides the same baked
arrays src/unity_racetrack.py's start grid uses, and src/unity_layout.py
already treats any group carrying `instance_positions` as PREPLACED — packed
around, never repacked. Binding happens in src/unity_scenebind.py.

Coordinates
-----------
The Scene Compiler viewer is three.js Y-up right-handed; Unity is Y-up
LEFT-handed. The layout file states the conversion itself
(`coordinate_note`) and this module is its only implementation:

    position (x, y, z) -> (x, y, -z)
    yaw_deg            -> -yaw_deg

`base_scale`
------------
The viewer silently multiplies a model by 0.01 when its loaded bounding
sphere exceeds 100 units ("looked centimeter-scaled"), and that correction is
part of what the user SAW but is not part of `scale`. Scene Compiler builds
that record `base_scale` per item are reproduced exactly. Builds that do not
(older saves) are assumed 1.0 with a loud warning naming the remedy — never a
silent guess, because the failure mode is a 100x-wrong world.

GLB / glTF
----------
The Unity side of this repo is FBX end to end (catalog, measurement, .meta
emission, ModelSwap). GLB items are converted to FBX by headless Blender —
the same converter Sketchfab downloads already use — into a cache keyed by
source path + mtime, and with the converter's unit-guess sanity rescale
DISABLED: the author already sized this model by eye, so nothing downstream
may second-guess it.

Everything here is deterministic: pure parsing, arithmetic and lookup, fixed
ordering, no RNG, no LLM.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .ident import HashRef, chash, fhash
from .ident.tags import ASSET

_log = logging.getLogger(__name__)

SCENE_VERSION = 1
# EntitySpec.instance_positions / count are validator-clamped to this many.
MAX_INSTANCES = 200
# EntitySpec position components are clamped to +/- this by the schema.
MAX_COORD = 2000.0
MODEL_EXTS = {".fbx", ".glb", ".gltf"}
SPRITE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tga"}
# The viewer's own auto-correction threshold, mirrored here only to explain
# the warning text — never applied speculatively.
_VIEWER_CM_HINT = ("re-save the layout in the Scene Compiler (newer builds "
                   "record `base_scale`) or set it by hand in the layout JSON")


class SceneInputError(ValueError):
    """A layout that cannot be turned into a scene file. Raised instead of
    degrading: a half-read layout would place the author's models somewhere
    they never put them."""


# ── layout reading ───────────────────────────────────────────────────────────

def load_layout(path: str | Path) -> dict:
    """Read and shallow-validate a Scene Compiler `layouts/<name>.json`."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SceneInputError(f"layout file not found: {p}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SceneInputError(f"unreadable layout {p}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise SceneInputError(
            f"{p} is not a Scene Compiler layout (no `items` array)")
    if not raw["items"]:
        raise SceneInputError(f"{p} has no items — nothing to place")
    return raw


# ── naming ───────────────────────────────────────────────────────────────────

_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def stem_words(filename: str) -> list[str]:
    """The file stem split into words on separators AND camelCase boundaries.

    `monacoGP.fbx` -> ["monaco", "GP"];  `player_monobike.fbx` -> ["player",
    "monobike"]. Shared by `entity_name_for` and `role_for` so the name a
    group gets and the role it declares can never disagree about where the
    first word ends.
    """
    words: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", Path(filename).stem):
        if not chunk:
            continue
        words.extend(w for w in _CAMEL_SPLIT.split(chunk) if w)
    return words


def entity_name_for(filename: str) -> str:
    """`monacoGP.fbx` -> `MonacoGP`; `big-head-cromulon.fbx` -> `BigHeadCromulon`.

    Deterministic PascalCase from the file stem: split on separators and
    camelCase boundaries, capitalize each word's first letter but keep the
    rest as authored (so `GP` survives as `GP`, not `Gp`).
    """
    words = stem_words(filename)
    if not words:
        return "AuthoredItem"
    out = "".join(w[0].upper() + w[1:] for w in words)
    return out if out[0].isalpha() else f"M{out}"


# ── role prefixes ────────────────────────────────────────────────────────────
# The author's channel for saying WHAT A MODEL IS, not just which mesh it is.
#
# Binding (src/unity_scenebind.py) scores an authored group against the entity
# names the Pipeline LLM invented at Step 3. That works when the two happen to
# share a word and fails silently when they do not: `victorian_monobike.fbx`
# binds an entity the model called `PlayerMonobike`, and binds NOTHING when it
# called the same thing `Racer` — in which case the file is injected as
# `kind="prop"`, lands in exactly the right spot, and is wired as scenery. The
# author cannot predict the LLM's word choice, so keyword overlap can never be
# the control surface for "this one is the player".
#
# A leading token on the model's own FILENAME can be, and it is the one channel
# that needs no change to the Scene Compiler app: rename the file in models/
# and the role travels with it through every layout that references it.
#
#     player_monobike.fbx   enemy_drone.fbx   pickup_scrap.fbx
#
# A stem whose first word is not a role word (`victorian_monobike.fbx`) yields
# None and behaves exactly as it always has — this is additive, and every
# layout written before it keeps its old binding.
#
# Values are the `EntitySpec.kind` strings the rest of the pipeline already
# reads: the spawner's player/enemy/npc/character/vehicle wiring, the objective
# kinds in unity_playability._OBJECTIVE_KINDS, and its scenery set.
ROLE_PREFIXES: dict[str, str] = {
    # actors
    "player": "player", "hero": "player", "avatar": "player",
    "enemy": "enemy", "foe": "enemy", "rival": "enemy", "boss": "boss",
    "npc": "npc", "ally": "npc", "character": "character",
    "vehicle": "vehicle", "car": "vehicle",
    # objectives
    "pickup": "pickup", "collectible": "collectible", "item": "item",
    "goal": "goal", "objective": "objective",
    # scenery
    "prop": "prop", "building": "building", "tree": "tree", "rock": "rock",
    "decor": "decor", "wall": "wall", "crowd": "crowd", "scenery": "scenery",
    "furniture": "furniture", "terrain": "terrain", "ground": "ground",
    "road": "road", "track": "track", "street": "street", "rail": "rail",
    "water": "water", "fx": "fx", "floor": "floor",
}


def role_for(filename: str) -> Optional[str]:
    """The `EntitySpec.kind` a filename's leading token declares, or None.

    `player_monobike.fbx` / `playerMonobike.fbx` / `Player-Monobike.fbx` all
    give "player"; `victorian_monobike.fbx` gives None.

    A role word must PREFIX something — `vehicle.fbx`, `tree.fbx`, `car.fbx`
    are ordinary asset names people ship without meaning to declare anything,
    and reading them as declarations would let one generic file claim an
    entity outright (the case `MIN_BIND_SCORE` exists to prevent). Two words
    minimum, so the intent is unmistakable.
    """
    words = stem_words(filename)
    if len(words) < 2:
        return None
    return ROLE_PREFIXES.get(words[0].lower())


def _dedupe(name: str, taken: set[str]) -> str:
    if name not in taken:
        taken.add(name)
        return name
    n = 2
    while f"{name}{n}" in taken:
        n += 1
    taken.add(f"{name}{n}")
    return f"{name}{n}"


# ── coordinate conversion ────────────────────────────────────────────────────

def to_unity_position(pos) -> list[float]:
    """three.js Y-up right-handed [x, y, z] -> Unity [x, y, -z], clamped to the
    schema's +/-2000 position range."""
    try:
        x, y, z = (float(pos[0]), float(pos[1]), float(pos[2]))
    except (TypeError, ValueError, IndexError) as exc:
        raise SceneInputError(f"unparseable position {pos!r}") from exc
    out = [round(max(-MAX_COORD, min(MAX_COORD, v)), 3) for v in (x, y, -z)]
    # The z flip turns 0.0 into -0.0, which serializes as "-0" and makes two
    # identical layouts diff. Normalize it away.
    return [v if v else 0.0 for v in out]


def to_unity_yaw(yaw_deg) -> float:
    """three.js CCW-about-+Y -> Unity CW-about-+Y (handedness flip), wrapped
    into [0, 360)."""
    try:
        y = -float(yaw_deg or 0.0)
    except (TypeError, ValueError) as exc:
        raise SceneInputError(f"unparseable yaw {yaw_deg!r}") from exc
    return round(y % 360.0, 3)


# ── scene building ───────────────────────────────────────────────────────────

def build_scene(layout: dict,
                models_dir: str | Path,
                layout_path: str | Path | None = None) -> tuple[dict, list[str]]:
    """Canonicalize a parsed layout into the scene file `unity-create --scene`
    consumes. Returns (scene, warnings).

    One GROUP per (file, kind, orientation, effective scale) — items of the
    same model at the same size become ONE entity with `count` baked
    instances, which is exactly how the pipeline's spawner wants them. A
    differently-scaled copy of the same file becomes its own group, because
    EntitySpec carries a single group-level `scale` (no schema change).

    Group and instance ordering follow first appearance in the layout, so the
    same layout always produces the same scene file, byte for byte.
    """
    root = Path(models_dir)
    if not root.is_dir():
        raise SceneInputError(f"models dir not found: {root}")

    warnings: list[str] = []
    missing: list[str] = []
    no_base_scale: list[str] = []
    # (file, type, orient, scale) -> group dict
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []

    for item in layout["items"]:
        if not isinstance(item, dict):
            raise SceneInputError(f"malformed layout item: {item!r}")
        rel = str(item.get("file") or "").strip()
        if not rel:
            raise SceneInputError(f"layout item {item.get('name')!r} has no file")
        src = (root / rel)
        if not src.is_file():
            missing.append(rel)
            continue

        ext = src.suffix.lower()
        if ext in SPRITE_EXTS:
            kind = "sprite"
        elif ext in MODEL_EXTS:
            kind = "model"
        else:
            raise SceneInputError(
                f"unsupported asset type {ext!r} for {rel} — Scene Compiler "
                f"models are {sorted(MODEL_EXTS)}, sprites {sorted(SPRITE_EXTS)}")

        # base_scale: the viewer's hidden centimeter auto-correction. Absent =
        # an older Scene Compiler save; assume 1.0 but say so, loudly, once
        # per file (see module docstring).
        if "base_scale" in item and item["base_scale"] is not None:
            try:
                base = float(item["base_scale"])
            except (TypeError, ValueError) as exc:
                raise SceneInputError(
                    f"unparseable base_scale on {item.get('name')!r}") from exc
        else:
            base = 1.0
            if rel not in no_base_scale:
                no_base_scale.append(rel)
        raw_scale = item.get("scale")
        # `or 1.0` would silently turn an authored 0 into 1 — the one value
        # that must fail instead (nothing is placed at zero size on purpose).
        raw_scale = 1.0 if raw_scale is None else raw_scale
        try:
            scale = round(float(raw_scale) * base, 6)
        except (TypeError, ValueError) as exc:
            raise SceneInputError(
                f"unparseable scale on {item.get('name')!r}") from exc
        if scale <= 0:
            raise SceneInputError(
                f"non-positive scale {scale} on {item.get('name')!r}")

        orient = item.get("orient") or None
        key = (rel, kind, orient, scale)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "name": "",                      # assigned after grouping
                "file": rel,
                "source_file": rel,
                "src": str(src.resolve()),
                "type": kind,
                "orient": orient,
                # A pure function of `rel`, which is part of the group key —
                # so every instance in a group always declares the same role.
                "role": role_for(rel),
                "scale": scale,
                "count": 0,
                "positions": [],
                "yaws": [],
                "items": [],
            }
            order.append(key)
        if g["count"] >= MAX_INSTANCES:
            warnings.append(
                f"{rel}: more than {MAX_INSTANCES} instances — the schema caps "
                f"a group there; extra copies dropped")
            continue
        g["positions"].append(to_unity_position(item.get("position") or [0, 0, 0]))
        g["yaws"].append(to_unity_yaw(item.get("yaw_deg")))
        g["items"].append(str(item.get("name") or rel))
        g["count"] += 1

    if missing:
        raise SceneInputError(
            "layout references models that are not in the models dir:\n  "
            + "\n  ".join(sorted(set(missing)))
            + f"\nmodels dir: {root}\nremedy: point --models-dir at the Scene "
              "Compiler's own models/ folder, or re-export the layout as a "
              "Bundle .zip so the files travel with it")
    if no_base_scale:
        warnings.append(
            "layout carries no `base_scale` for "
            + ", ".join(sorted(no_base_scale))
            + f" — assuming 1.0. If the Scene Compiler reported \"looked "
              f"centimeter-scaled\" for any of them the world will be 100x "
              f"off; {_VIEWER_CM_HINT}.")

    taken: set[str] = set()
    for key in order:
        g = groups[key]
        g["name"] = _dedupe(entity_name_for(g["file"]), taken)

    scene = {
        "version": SCENE_VERSION,
        "source": "scene-compiler",
        "layout": Path(layout_path).stem if layout_path else "",
        "layout_path": str(Path(layout_path).resolve()) if layout_path else "",
        "models_dir": str(root.resolve()),
        "view_mode": str(layout.get("view_mode") or "top"),
        "units": str(layout.get("units") or "meters"),
        "groups": [groups[k] for k in order],
    }
    return scene, warnings


def load_scene(path: str | Path) -> tuple[dict, list[str]]:
    """Read either a canonical scene file (this module's own output) or a raw
    Scene Compiler layout, returning (scene, warnings).

    `unity-create --scene` accepts both so the two-step normalize is optional:
    a layout is recognized by its `items` array, a scene file by `groups`.
    A raw layout resolves its models against `models_dir` — the layout file's
    own `../models` sibling unless the caller overrides it.
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SceneInputError(f"scene file not found: {p}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SceneInputError(f"unreadable scene file {p}: {exc}") from exc

    if isinstance(raw, dict) and isinstance(raw.get("groups"), list):
        if not raw["groups"]:
            raise SceneInputError(f"{p} has no groups — nothing to place")
        missing = [g["src"] for g in raw["groups"]
                   if not Path(g.get("src", "")).is_file()]
        if missing:
            raise SceneInputError(
                "scene file references models that no longer exist:\n  "
                + "\n  ".join(sorted(set(missing)))
                + "\nremedy: re-run `python main.py unity-scene <layout.json>`")
        return raw, []
    return build_scene(raw, default_models_dir(p), layout_path=p)


def default_models_dir(layout_path: str | Path) -> Path:
    """The Scene Compiler keeps `layouts/` and `models/` as siblings, so a
    layout's models dir is derivable from its own path."""
    return Path(layout_path).resolve().parent.parent / "models"


# ── GLB -> FBX ───────────────────────────────────────────────────────────────

def _gltf_external_uris(src: Path) -> list[str]:
    """Relative URIs of every external buffer and image a .gltf references —
    the files Blender reads alongside it. Embedded `data:` URIs are already
    part of the .gltf's own bytes. Sorted and de-duplicated."""
    doc = json.loads(src.read_text(encoding="utf-8"))
    uris = {unquote(item["uri"])
            for key in ("buffers", "images")
            for item in (doc.get(key) or [])
            if isinstance(item, dict) and isinstance(item.get("uri"), str)
            and not item["uri"].startswith("data:")}
    return sorted(uris)


def _cache_key(src: Path) -> str:
    """Content identity of a conversion input (src/ident): what the model IS,
    not where it lives. A .glb is its own bytes. A .gltf is its bytes plus the
    bytes of every external buffer/image it references — an edited .bin
    beside an untouched .gltf is a different model.

    Replaces a sha1 of (absolute path, size, mtime), which missed the cache on
    every other machine or worktree, missed it again after a checkout touched
    the mtime, and never saw a .gltf's external files change at all.
    Raises OSError / ValueError when the source or a referenced file cannot be
    read or parsed.
    """
    if src.suffix.lower() != ".gltf":
        return fhash(src)
    external = [[uri, fhash(src.parent / uri)] for uri in _gltf_external_uris(src)]
    return chash(ASSET, {"gltf": HashRef.of_file(src), "external": external})


def convert_gltf_groups(scene: dict,
                        cache_dir: str | Path,
                        blender_cfg: Optional[dict] = None) -> list[str]:
    """Convert every .glb/.gltf group in `scene` to FBX, in place.

    Reuses src/blender/sketchfab_convert.convert_gltf_to_fbx (headless
    Blender) with `category="as_authored"` — the converter's unit-guess
    rescale is DISABLED for that category, because the author already sized
    this model by eye in the Scene Compiler and a downstream "correction"
    would silently relocate everything they placed.

    Results are cached under `cache_dir/<content key>/` (`_cache_key`), so a
    second build of the same model does no Blender work at all — on any
    machine, from any path. Returns log messages. Raises SceneInputError when
    a source cannot be read or a conversion fails: a missing model is not
    something to degrade around.
    """
    pending = [g for g in scene.get("groups", [])
               if Path(g["src"]).suffix.lower() in (".glb", ".gltf")]
    if not pending:
        return []

    from .blender.sketchfab_convert import convert_gltf_to_fbx

    cache = Path(cache_dir)
    msgs: list[str] = []
    failed: list[str] = []
    unreadable: list[str] = []
    done: dict[str, str] = {}          # src -> converted fbx path
    for g in pending:
        src = Path(g["src"])
        if str(src) in done:
            g["src"] = done[str(src)]
            g["file"] = Path(done[str(src)]).name
            continue
        try:
            key = _cache_key(src)
        except (OSError, ValueError) as exc:
            unreadable.append(f"{src.name}: {exc}")
            continue
        out_dir = cache / key
        fbx_out = out_dir / f"{src.stem}.fbx"
        if fbx_out.is_file():
            msgs.append(f"scene: {src.name} -> cached FBX")
        else:
            got = convert_gltf_to_fbx(src, fbx_out, out_dir / "Textures",
                                      blender_cfg=blender_cfg,
                                      category="as_authored")
            if got is None or not fbx_out.is_file():
                failed.append(src.name)
                continue
            msgs.append(f"scene: {src.name} -> FBX (headless Blender)")
        done[str(src)] = str(fbx_out.resolve())
        g["src"] = str(fbx_out.resolve())
        g["file"] = fbx_out.name

    if unreadable:
        raise SceneInputError(
            "GLB/glTF source could not be read:\n  " + "\n  ".join(sorted(unreadable))
            + "\nremedy: re-export these items from the Scene Compiler — a .gltf "
              "must ship with every external buffer/image it references, or "
              "export a single-file .glb instead")
    if failed:
        raise SceneInputError(
            "GLB/glTF conversion failed for:\n  " + "\n  ".join(sorted(failed))
            + "\nremedy: check `blender.blender_path` in config.yaml points at "
              "a working Blender install, or export these items as FBX from "
              "the Scene Compiler and re-save the layout")
    return msgs


def describe(scene: dict) -> list[str]:
    """Human-readable one-line-per-group summary for the CLI."""
    out = []
    for g in scene.get("groups", []):
        pos = g["positions"][0] if g["positions"] else [0, 0, 0]
        where = (f"({pos[0]:g}, {pos[1]:g}, {pos[2]:g})" if g["count"] == 1
                 else f"{g['count']} instances")
        role = f"  role={g['role']}" if g.get("role") else ""
        out.append(f"{g['name']:<28} {g['file']:<38} scale {g['scale']:g}  "
                   f"{where}{role}")
    return out
