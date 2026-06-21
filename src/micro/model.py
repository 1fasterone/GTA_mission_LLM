"""
3D model micro-generator — procedural Blender bases (car, plane) → Unity FBX.

Unlike the other micros (which are LLM → C# script), this one produces GEOMETRY:
it maps a prompt to a base + parameter "knobs", builds a textured mesh in Blender
(headless), exports a Unity-friendly FBX, and overlays it into a Unity project's
Assets/ folder via the same overlay path the Blender Job Manager uses.

The two bases come from the repo-root `vehicle_generator` module (the detailed
SpaceshipGenerator-style builders):
  * car   — generate_vehicle / rig_vehicle   (knobs: VEH PARAM_SCHEMA)
  * plane — generate_plane   / rig_plane      (knobs: PLANE_PARAM_SCHEMA)

Execution:
  * headless (default) — `blender --background --factory-startup --python <script>`
    via src/blender/locator; reads the result JSON the script writes.
  * emit (--emit)      — only write the bpy script + plan (for the live MCP path).

This is the bridge that lets `main.py` populate Unity projects with real meshes,
complementing the C# micros that wire up behaviour.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

console = Console()

# Repo root on sys.path so the root-level generator modules import cleanly when
# this runs as part of `python main.py ...`.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Variants backed by vehicle_generator's detailed FBX builder. (plane now uses the
# ported planegen builder in base_models instead — see _variant_table.)
_VG_VARIANTS = {"car"}

# Per-variant: (param schema, clamp fn, keyword-map fn, asset_type).
def _variant_table():
    import vehicle_generator as vg
    from src.blender.base_models import BASE_PARAM_SCHEMA, clamp_params as bm_clamp, keyword_hints

    def _bm_clamp(base):
        return lambda raw: bm_clamp(base, raw)

    def _bm_keyword(base):
        return lambda prompt: keyword_hints(prompt, base)

    return {
        "car":      (vg.PARAM_SCHEMA, vg.clamp_params, vg.keyword_vehicle_params, "vehicle"),
        "plane":     (BASE_PARAM_SCHEMA["plane"],     _bm_clamp("plane"),     _bm_keyword("plane"),     "aircraft"),
        "boat":      (BASE_PARAM_SCHEMA["boat"],      _bm_clamp("boat"),      _bm_keyword("boat"),      "vehicle"),
        "humanoid":  (BASE_PARAM_SCHEMA["humanoid"],  _bm_clamp("humanoid"),  _bm_keyword("humanoid"),  "character"),
        "bike":      (BASE_PARAM_SCHEMA["bike"],      _bm_clamp("bike"),      _bm_keyword("bike"),      "vehicle"),
        "furniture": (BASE_PARAM_SCHEMA["furniture"], _bm_clamp("furniture"), _bm_keyword("furniture"), "prop"),
        "weapon":    (BASE_PARAM_SCHEMA["weapon"],    _bm_clamp("weapon"),    _bm_keyword("weapon"),    "weapon"),
    }


def _build_base_code_with_fbx(base: str, params: dict, coll: str,
                               out_dir: str, fbx_path: str,
                               rig: bool = False, demo: bool = False) -> str:
    """Wrap base_models.build_base_code with an FBX export + result JSON tail.

    build_base_code emits a pure scene-construction script; this appends the
    bpy FBX exporter call and the manifest write so the headless backend gets
    the same result dict shape as vehicle_generator."""
    from src.blender.base_models import build_base_code
    code = build_base_code(base, params, coll, ox=0.0, rig=rig, demo=demo)
    out_fwd = str(out_dir).replace("\\", "/")
    fbx_fwd = str(fbx_path).replace("\\", "/")
    code += f'\n_OUT_DIR = r"{out_fwd}"\n'

    # Full Unity pipeline (rigged bases): smart-project UVs, build a convex COL_
    # collider hull and LOD0_/LOD1_ decimated copies before export. Gated to the
    # ported, properly-rigged bases; the others stay untouched until they're ported.
    if base in ("humanoid", "plane", "boat"):
        code += '''
# --- full Unity pipeline: UVs + collider + LODs ---
_meshes = [o for o in _parts if o.type == "MESH"]
for _o in _meshes:
    uv_unwrap(_o)
_col = collider_hull(COLL, _parts)
_lods = make_lods(_parts, levels=2)
result["collider"] = bool(_col)
result["lods"] = len(_lods)
result["objects"] = [o.name for o in _coll().objects]
print("[proc] unity pipeline: collider=%s lods=%d" % (bool(_col), len(_lods)))
'''

    code += f'''
import bpy as _bpy, json as _json, os as _os
_fbx = r"{fbx_fwd}"
# Export ONLY this model's collection — never the scene's default cube/camera/light.
_bpy.ops.object.select_all(action="DESELECT")
for _o in _coll().objects:
    _o.select_set(True)
_bpy.ops.export_scene.fbx(
    filepath=_fbx,
    use_selection=True,
    global_scale=1.0,
    apply_unit_scale=True,
    bake_space_transform=False,
    object_types={{"MESH", "ARMATURE"}},
    use_mesh_modifiers=True,
    add_leaf_bones=False,
    path_mode="COPY",
    embed_textures=True,
)
result["fbx"] = _fbx
result["tris"] = sum(len(o.data.polygons) * 2 for o in _bpy.data.objects if o.type == "MESH")
_os.makedirs(_OUT_DIR, exist_ok=True)
_p = _os.path.join(_OUT_DIR, COLL + ".result.json")
with open(_p, "w", encoding="utf-8") as _f:
    _json.dump(result, _f, indent=2)
print("[proc] wrote", _p)
'''
    return code


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:40] or "model"


def _to_pascal(s: str, fallback: str = "Model") -> str:
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", s or "").split()
    out = "".join(p.capitalize() for p in parts)
    return out or fallback


class ModelPipeline:
    """Procedural 3D model micro-generator (car / plane → FBX → Unity)."""

    kind            = "model"
    display_name    = "Procedural 3D Model (Blender → FBX)"
    variants        = ["car", "plane", "boat", "humanoid", "bike", "furniture", "weapon"]
    default_variant = "car"

    def __init__(self, config: dict):
        self.config       = config or {}
        bl                = self.config.get("blender", {})
        self.blender_path = bl.get("blender_path") or ""
        self.blender_out  = Path(bl.get("output_dir", "output/blender"))
        self.timeout      = int(bl.get("headless_timeout_s", 300))

    # ── knob schema (for CLI help / validation) ──────────────────────────────
    @classmethod
    def knob_schema(cls, variant: str) -> dict:
        schema, *_ = _variant_table()[variant if variant in cls.variants else "car"]
        return schema

    # ── prompt → params ──────────────────────────────────────────────────────
    def _map_params(self, prompt: str, variant: str, use_llm: bool) -> tuple[dict, str]:
        schema, clamp, keyword, _ = _variant_table()[variant]
        # Only the detailed car/plane builders have an LLM param mapper; the
        # base_models variants (humanoid/bike/furniture) use their own knob
        # schema, so route them through the deterministic keyword mapper.
        if use_llm and prompt and variant in _VG_VARIANTS:
            try:
                import procedural_pipeline as pp
                mapper = pp.map_plane_prompt if variant == "plane" else pp.map_vehicle_prompt
                res = mapper(prompt, use_llm=True)
                return res["params"], res["source"]
            except Exception as exc:  # noqa: BLE001 - any failure -> deterministic path
                console.print(f"  [yellow]LLM map failed ({type(exc).__name__}); keyword fallback[/]")
        return clamp(keyword(prompt or "")), "fallback"

    def _apply_knobs(self, variant: str, params: dict, knobs: Optional[dict]) -> dict:
        """Override mapped params with explicit knobs, then re-clamp."""
        if not knobs:
            return params
        schema, clamp, _, _ = _variant_table()[variant]
        merged = dict(params)
        for k, v in knobs.items():
            if k == "color" or k in schema:
                merged[k] = v
            else:
                console.print(f"  [yellow]unknown knob '{k}' for {variant} (ignored)[/]")
        return clamp(merged)

    def _resolve_variant(self, variant: Optional[str], prompt: Optional[str]) -> str:
        if variant in self.variants:
            return variant
        if prompt:
            try:
                from src.blender.base_models import keyword_base
                kb = keyword_base(prompt)
                if kb in self.variants:
                    return kb
            except Exception:  # noqa: BLE001
                pass
        return self.default_variant

    # ── build one FBX headless (the reusable core; no Unity overlay/printing) ──
    def build_fbx(self, prompt: Optional[str] = None, *,
                  variant: Optional[str] = None,
                  knobs: Optional[dict] = None,
                  seed: Optional[int] = None,
                  rig: bool = False,
                  demo: bool = False,
                  name: Optional[str] = None,
                  use_llm: bool = True,
                  out: Optional[str] = None) -> Optional[dict]:
        """Map params, build the mesh in headless Blender, export FBX. Returns a
        result dict (fbx, tris, rigged, params, name, variant, source, out_dir) or
        None if Blender is missing / the build failed. Used both by run() and by the
        Unity pipeline's model-fulfilment pass."""
        import vehicle_generator as vg
        from src.blender.locator import find_blender

        variant = self._resolve_variant(variant, prompt)
        _, _, _, asset_type = _variant_table()[variant]
        params, source = self._map_params(prompt or "", variant, use_llm)
        params = self._apply_knobs(variant, params, knobs)

        name = _to_pascal(name or _slug(prompt or variant), variant.title())
        out_dir = Path(out) if out else (self.blender_out / "models" / name)
        out_dir.mkdir(parents=True, exist_ok=True)
        coll = "MODEL_" + name
        fbx_path = out_dir / f"{name}.fbx"
        seed_str = str(seed) if seed is not None else name

        if variant in _VG_VARIANTS:
            code = vg.build_detailed_code(
                params, coll=coll, out_dir=str(out_dir), name=name, seed_str=seed_str,
                rig=rig, demo=demo, kind=variant, fbx_path=str(fbx_path),
            )
        else:
            code = _build_base_code_with_fbx(
                variant, params, coll, str(out_dir), str(fbx_path), rig=rig, demo=demo,
            )
        script_path = out_dir / f"{name}.run.py"
        script_path.write_text(code, encoding="utf-8")

        bl = find_blender(self.blender_path)
        if bl is None:
            console.print("  [yellow]Blender not found[/] (set blender.blender_path or BLENDER_PATH)")
            return None

        result_json = out_dir / f"{coll}.result.json"
        if result_json.exists():
            result_json.unlink()
        log_path = out_dir / f"{name}.log"
        cmd = [str(bl.path), "--background", "--factory-startup", "--python", str(script_path)]
        console.print(f"  [dim]Blender headless ({bl.version}): {variant} '{name}'[/]")
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=self.timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            console.print(f"  [red]timeout[/] (>{self.timeout}s) — see {log_path}")
            return None

        result = {}
        if result_json.exists():
            try:
                result = json.loads(result_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result = {}
        if rc != 0 or not result.get("fbx"):
            console.print(f"  [red]headless build failed[/] (rc={rc}) — see {log_path}")
            return None

        result.update(name=name, variant=variant, asset_type=asset_type,
                      source=source, params=params, out_dir=str(out_dir),
                      script=str(script_path))
        return result

    # ── main run (CLI: build + optional overlay) ─────────────────────────────
    def run(self, prompt: Optional[str] = None, *,
            variant: Optional[str] = None,
            knobs: Optional[dict] = None,
            seed: Optional[int] = None,
            rig: bool = True,
            demo: bool = False,
            name: Optional[str] = None,
            unity_project: Optional[str] = None,
            assets_subdir: str = "Assets/BlenderAssets",
            emit: bool = False,
            use_llm: bool = True,
            out: Optional[str] = None,
            # accepted for registry/_run_micro compatibility, unused for geometry:
            theme: Optional[str] = None,
            mashup_with: Optional[str] = None) -> Path:

        import vehicle_generator as vg
        variant = self._resolve_variant(variant, prompt)
        console.print(Panel(
            f"[bold]{self.display_name}[/]\n"
            f"variant: [cyan]{variant}[/]   rig: [cyan]{rig}[/]\n"
            f"{prompt or '(knob-driven, no prompt)'}", style="blue"))

        # emit mode: write the bpy script only (run via live MCP), skip headless.
        if emit:
            params, source = self._map_params(prompt or "", variant, use_llm)
            params = self._apply_knobs(variant, params, knobs)
            name = _to_pascal(name or _slug(prompt or variant), variant.title())
            out_dir = Path(out) if out else (self.blender_out / "models" / name)
            out_dir.mkdir(parents=True, exist_ok=True)
            coll = "MODEL_" + name
            fbx_emit = str(out_dir / f"{name}.fbx")
            if variant in _VG_VARIANTS:
                code = vg.build_detailed_code(
                    params, coll=coll, out_dir=str(out_dir), name=name,
                    seed_str=str(seed) if seed is not None else name,
                    rig=rig, demo=demo, kind=variant, fbx_path=fbx_emit)
            else:
                code = _build_base_code_with_fbx(
                    variant, params, coll, str(out_dir), fbx_emit, rig=rig, demo=demo,
                )
            (out_dir / f"{name}.run.py").write_text(code, encoding="utf-8")
            console.print(f"  [green]emitted[/] script -> {out_dir / (name + '.run.py')} "
                          f"[dim](run via live MCP; headless skipped)[/]")
            return out_dir

        res = self.build_fbx(prompt, variant=variant, knobs=knobs, seed=seed,
                             rig=rig, demo=demo, name=name, use_llm=use_llm, out=out)
        if res is None:
            console.print("[red]Model build failed.[/]")
            raise RuntimeError("model build failed")

        name, fbx_out, out_dir = res["name"], res["fbx"], Path(res["out_dir"])
        console.print(f"  [green]OK[/]   FBX ([cyan]{res.get('tris','?')}[/] tris, "
                      f"source={res['source']}) -> {fbx_out}")
        plan = {"prompt": prompt, "variant": variant, "source": res["source"],
                "params": res["params"], "rig": rig, "name": name, "fbx": fbx_out,
                "tris": res.get("tris"), "rigged": res.get("rigged")}
        if unity_project:
            plan["unity_overlay"] = self._overlay(
                name, res["asset_type"], fbx_out, res, unity_project, assets_subdir)
        self._write_plan(out_dir, name, plan)
        console.print(Panel(
            f"[bold green]Done![/]  [cyan]{name}[/] ({variant})\n"
            f"FBX:    {fbx_out}\n"
            + (f"Unity:  {plan['unity_overlay'].get('target')}\n"
               if plan.get("unity_overlay") else "")
            + f"Output: {out_dir}", style="green"))
        return out_dir

    # ── helpers ──────────────────────────────────────────────────────────────
    def _overlay(self, name, asset_type, fbx_out, result, unity_project, assets_subdir) -> dict:
        from src.blender.manifest import AssetManifest
        from src.blender.unity_overlay import overlay_asset
        manifest = AssetManifest(
            name=name, asset_type=asset_type, style="procedural_lowpoly",
            route="blender", fbx=fbx_out, rigged=bool(result.get("rigged")),
            collider=bool(result.get("collider")), tris=result.get("tris"),
        )
        res = overlay_asset(manifest, Path(unity_project), assets_subdir=assets_subdir)
        # Drop the AssetPostprocessor so the project auto-wires Humanoid rig /
        # LODGroup / MeshCollider on import (idempotent; never clobbers a newer copy).
        self._install_postprocessor(Path(unity_project))
        if res.copied:
            console.print(f"  [green]overlay[/] -> {unity_project}: {', '.join(res.copied)}")
        if res.missing:
            console.print(f"  [yellow]missing[/] {', '.join(res.missing)}")
        return res.model_dump()

    def _install_postprocessor(self, unity_root: Path) -> None:
        """Copy templates/unity/ProcGenImporter.cs into <project>/Assets/Editor/
        if absent. Editor-only; required for the overlay path since the generated
        scene doesn't otherwise consume Assets/BlenderAssets/."""
        src = _REPO_ROOT / "templates" / "unity" / "ProcGenImporter.cs"
        if not src.exists():
            return
        dst = unity_root / "Assets" / "Editor" / "ProcGenImporter.cs"
        if dst.exists():
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            console.print(f"  [green]editor[/] -> {dst.relative_to(unity_root)}")
        except OSError as exc:  # noqa: BLE001 - overlay should never hard-fail
            console.print(f"  [yellow]postprocessor copy failed[/] ({exc})")

    def _write_plan(self, out_dir: Path, name: str, plan: dict) -> None:
        (out_dir / f"{name}.plan.json").write_text(
            json.dumps(plan, indent=2), encoding="utf-8")
