"""
BlenderJobManager — the service from the goal:

  receives job specs → queues them → launches Blender headless → monitors output
  → retries failures → returns asset manifests.

Design mirrors the headless Unity `verify` gate (unity_cli._run_compile_check):
locate the external tool, run it in batch mode with a timeout, parse the report
it drops to disk, and degrade gracefully when the tool is missing (here: emit a
STUBBED manifest so the wider pipeline keeps moving instead of hard-failing).

Backend parity: BOTH backends run the same recipe-emitted script from
`mcp_backend.build_job_code` — the live MCP backend returns its `result`, the
subprocess backend runs it headless::

    blender --background --factory-startup --python <name>.run.py

with `write_manifest=True` so the run leaves `<name>.manifest.json` on disk; we
read that back into an AssetManifest. Same recipes, materials, and steps either
way — no divergence between headless and live output.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Optional

from rich.console import Console

from .job_schema  import BlenderJob
from .manifest    import AssetManifest, BatchManifest, JobResult, JobStatus
from .locator     import find_blender, BlenderInstall
from .diffusion   import DiffusionClient, build_texture_request
from .mcp_backend import build_job_code, steps_for
from .cache       import BlenderCache
from .rigging     import rig_strategy

# Geometry steps that a pre-rig mesh reuse replaces (everything else is the
# material/rig/export delta that still runs per job).
_GEO_STEPS = ("generate", "cleanup", "unwrap")

console = Console()


class BlenderJobManager:
    """Queue + headless runner for BlenderJobs.

    Parameters
    ----------
    config : dict
        The `blender` section of config.yaml (optional). Recognised keys:
          blender_path, output_dir, timeout, max_retries, diffusion{...}
    """

    def __init__(self, config: Optional[dict] = None,
                 mcp_executor: Optional[Callable[[str], dict]] = None):
        cfg = config or {}
        self.output_dir   = Path(cfg.get("output_dir", "output/blender"))
        self.timeout      = int(cfg.get("timeout", 900))
        self.max_retries  = int(cfg.get("max_retries", 1))
        # Strip wall-clock timestamps from FBX/Alembic so identical geometry
        # re-exports byte-identical (no spurious git/Unity churn). On by default.
        self.normalize_exports = bool(cfg.get("normalize_exports", True))
        # Deterministic artifact cache: identical job inputs reuse a stored result
        # and skip diffusion + Blender entirely. On by default; safe because the
        # pipeline is reproducible (seed → geo_hash, normalized exports).
        cache_enabled = bool(cfg.get("cache", True))
        cache_dir = cfg.get("cache_dir") or (self.output_dir / "_cache")
        self.cache = BlenderCache(cache_dir, enabled=cache_enabled)
        # Authored rig-template armature library (none shipped yet → templates fall
        # back to deterministic auto micros). Folded into the result fingerprint.
        self.cache.template_root = cfg.get("rig_template_dir") or None
        self._blender_cfg = cfg.get("blender_path")
        self.blender: Optional[BlenderInstall] = find_blender(self._blender_cfg)
        # A pre-rig mesh `.blend` is version-coupled; fold the Blender version into
        # the geometry fingerprint so an editor upgrade misses + rebuilds cleanly.
        self.cache.blender_version = (self.blender.version or "unknown") if self.blender \
            else ("live" if mcp_executor is not None else "none")
        self.diffusion = DiffusionClient(cfg.get("diffusion"))
        self._queue: deque[BlenderJob] = deque()
        # MCP (live-Blender) backend. When set, jobs run inside the connected
        # Blender via this callable: it takes a bpy script string and returns the
        # dict the script assigned to `result`. The host process can't call the
        # MCP tool itself, so the agent/orchestrator injects this.
        self.mcp_executor = mcp_executor
        self.live_steps: Optional[list[str]] = None   # override step subset (live)

    # ── queue API ─────────────────────────────────────────────────────────────

    def submit(self, job: BlenderJob) -> None:
        self._queue.append(job)

    def submit_all(self, jobs: Iterable[BlenderJob]) -> None:
        for j in jobs:
            self.submit(j)

    def submit_spec(self, spec: dict | str | Path) -> BlenderJob:
        """Accept a raw job spec (dict, JSON string, or path to a .json file),
        validate it into a BlenderJob, and enqueue it."""
        if isinstance(spec, (str, Path)) and Path(str(spec)).is_file():
            spec = json.loads(Path(str(spec)).read_text(encoding="utf-8"))
        elif isinstance(spec, str):
            spec = json.loads(spec)
        job = BlenderJob.model_validate(spec).default_output(str(self.output_dir))
        self.submit(job)
        return job

    @property
    def pending(self) -> int:
        return len(self._queue)

    # ── run ───────────────────────────────────────────────────────────────────

    def run_all(self, *, dry_run: bool = False) -> BatchManifest:
        """Drain the queue, running each job (with retries). Returns a
        BatchManifest aggregating every JobResult."""
        results: list[JobResult] = []
        total = len(self._queue)
        if self.mcp_executor is not None:
            console.print("  [green]Live backend[/] — running in the connected Blender via MCP")
        elif self.blender:
            console.print(f"  [green]Blender[/] {self.blender.version or '?'} "
                          f"-> {self.blender.path}")
        else:
            console.print("  [yellow]Blender not found[/] — jobs will be stubbed "
                          "(install Blender or set blender.blender_path)")

        idx = 0
        while self._queue:
            idx += 1
            job = self._queue.popleft()
            console.print(f"  [cyan]({idx}/{total})[/] {job.name}  "
                          f"[dim]{job.asset_type}/{job.style} via {job.meta.get('route', 'blender')}[/]")
            results.append(self._run_one(job, dry_run=dry_run))

        batch = BatchManifest(
            total   = len(results),
            done    = sum(r.status == JobStatus.DONE for r in results),
            failed  = sum(r.status == JobStatus.FAILED for r in results),
            stubbed = sum(r.status == JobStatus.STUBBED for r in results),
            results = results,
        )
        return batch

    # ── single job ────────────────────────────────────────────────────────────

    def _run_one(self, job: BlenderJob, *, dry_run: bool) -> JobResult:
        out_dir = self.output_dir / job.name
        route = job.meta.get("route", "blender")

        # The versioned rig micro chosen for this job (rigging router). When it adds
        # no armature, drop the rig/animate steps so a rig=True non-character (e.g.
        # a crate → RigAutoStaticV1) doesn't get spuriously skeletoned.
        strat = rig_strategy(job, self.cache.template_root)
        steps = list(self.live_steps) if self.live_steps else steps_for(job)
        if self.live_steps is None and not strat.needs_armature:
            steps = [s for s in steps if s not in ("rig", "animate")]

        # Layer 2 (skip-work): identical inputs → reuse the stored artifacts and
        # skip diffusion + Blender entirely. Keyed on the canonical (full) steps so
        # a cold run and a geometry-reused run of the same job share this cache.
        fp: Optional[str] = None
        if self.cache.enabled and not dry_run:
            fp = self.cache.fingerprint(job, steps)
            hit = self.cache.lookup(fp)
            if hit is not None:
                return self._cache_hit(hit, job, out_dir, route)

        # Geometry reuse: if the pre-rig mesh for this geometry already exists,
        # restore it and trim the geometry steps so the backend runs only the
        # material/rig/export delta. (geo-fp excludes colour/material/rig, so
        # variants share one mesh.)
        run_steps, reuse_blend, geo_fp = steps, None, None
        if self.cache.enabled and not dry_run:
            geo_fp = self.cache.geo_fingerprint(job)
            if self.cache.geo.get_mesh(geo_fp) is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                restored = self.cache.geo.restore_mesh(
                    geo_fp, out_dir / f"{job.name}.mesh.blend")
                if restored is not None:
                    reuse_blend = str(restored)
                    run_steps = [s for s in steps if s not in _GEO_STEPS]
                    console.print(f"      [green]geo reuse[/] {job.name} "
                                  f"[dim]{geo_fp[4:16]}[/]")

        # Diffusion pre-pass: if this job's textures need a diffusion model and
        # one is reachable, render the albedo now and point the job at it. If the
        # backend is down, fall back to procedural so Blender still textures it.
        if job.needs_diffusion() and not dry_run:
            self._diffusion_prepass(job, out_dir)

        # Live MCP backend: run inside the connected Blender (preferred when set).
        if self.mcp_executor is not None and not dry_run:
            result = self._run_via_mcp(job, out_dir, route, steps=run_steps,
                                       reuse_blend=reuse_blend, rig_strategy_id=strat.id)
        # No Blender → stub (graceful degradation, same as Unity verify w/o editor)
        elif self.blender is None or dry_run:
            result = self._stub(job, out_dir, route, dry_run=dry_run)
        else:
            result = self._run_subprocess(job, out_dir, route, steps=run_steps,
                                          reuse_blend=reuse_blend, rig_strategy_id=strat.id)

        # Store real geometry (never stubs) so the next identical job is a hit.
        if (fp and result.status == JobStatus.DONE
                and result.manifest is not None and not result.manifest.stub):
            try:
                self.cache.store(fp, result.manifest, out_dir, job)
            except OSError:
                pass
            # Layer 1 (substrate): file the reusable pre-rig mesh under its geo-fp
            # so material/colour variants skip the geometry build. Only produced on
            # a cold build (reuse_blend is None).
            if geo_fp and reuse_blend is None and result.manifest.mesh_blend:
                try:
                    self.cache.geo.put_mesh(geo_fp, result.manifest.mesh_blend,
                                            result.manifest.geo_hash, result.manifest.tris)
                except OSError:
                    pass
        return result

    def _run_subprocess(self, job: BlenderJob, out_dir: Path, route: str, *,
                        steps: Optional[list[str]] = None,
                        reuse_blend: Optional[str] = None,
                        rig_strategy_id: str = "") -> JobResult:
        """Headless Blender backend: run the recipe script with retries."""
        attempts = 0
        last_err = ""
        start = time.time()
        while attempts <= self.max_retries:
            attempts += 1
            rc, log_path, manifest = self._invoke_blender(
                job, out_dir, steps=steps, reuse_blend=reuse_blend,
                rig_strategy_id=rig_strategy_id)
            if rc == 0 and manifest is not None:
                return JobResult(
                    name=job.name, status=JobStatus.DONE, route=route,
                    attempts=attempts, duration_s=round(time.time() - start, 2),
                    manifest=manifest, log_path=str(log_path),
                )
            last_err = f"blender exited {rc}" if rc != 0 else "no manifest produced"
            console.print(f"      [yellow]retry[/] {job.name} ({last_err})")

        return JobResult(
            name=job.name, status=JobStatus.FAILED, route=route,
            attempts=attempts, duration_s=round(time.time() - start, 2),
            error=last_err, log_path=str(out_dir / f"{job.name}.log"),
        )

    def _cache_hit(self, entry, job: BlenderJob, out_dir: Path, route: str) -> JobResult:
        """Restore a cached entry into out_dir and return a DONE result."""
        manifest = self.cache.restore(entry, out_dir, job)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{job.name}.manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"      [green]cache hit[/] {job.name} "
                      f"[dim]{entry.fingerprint[:12]}[/]")
        return JobResult(
            name=job.name, status=JobStatus.DONE, route=route,
            attempts=0, duration_s=0.0, manifest=manifest,
        )

    def build_live_code(self, job: BlenderJob, out_dir: Optional[Path] = None) -> str:
        """Public helper: the bpy script that runs `job` in live Blender.
        Exposed so an orchestrator can run it directly through the MCP tool."""
        out_dir = out_dir or (self.output_dir / job.name)
        return build_job_code(job, str(out_dir), steps=self.live_steps)

    def _run_via_mcp(self, job: BlenderJob, out_dir: Path, route: str, *,
                     steps: Optional[list[str]] = None,
                     reuse_blend: Optional[str] = None,
                     rig_strategy_id: str = "") -> JobResult:
        """Run one job inside the connected Blender via the injected executor."""
        import time
        steps = steps if steps is not None else (self.live_steps or steps_for(job))
        code = build_job_code(job, str(out_dir), steps=steps,
                              reuse_blend=reuse_blend, rig_strategy_id=rig_strategy_id)
        attempts = 0
        last_err = ""
        start = time.time()
        while attempts <= self.max_retries:
            attempts += 1
            try:
                raw = self.mcp_executor(code)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - executor/transport failure
                last_err = f"executor error: {exc}"
                console.print(f"      [yellow]retry[/] {job.name} ({last_err})")
                continue
            data = self._unwrap_mcp_result(raw)
            if data and "error" not in data:
                out_dir.mkdir(parents=True, exist_ok=True)
                manifest = AssetManifest.from_blender_report(data)
                self._normalize_outputs(manifest)
                (out_dir / f"{job.name}.manifest.json").write_text(
                    manifest.model_dump_json(indent=2), encoding="utf-8")
                return JobResult(
                    name=job.name, status=JobStatus.DONE, route=route,
                    attempts=attempts, duration_s=round(time.time() - start, 2),
                    manifest=manifest,
                )
            last_err = (data or {}).get("error", "no result returned from Blender")
            console.print(f"      [yellow]retry[/] {job.name} ({last_err})")

        return JobResult(
            name=job.name, status=JobStatus.FAILED, route=route,
            attempts=attempts, duration_s=round(time.time() - start, 2),
            error=last_err,
        )

    @staticmethod
    def _unwrap_mcp_result(raw) -> dict:
        """The MCP tool returns {"status":"ok","result":{...}}; the executor may
        hand us that envelope or the inner dict directly. Normalise to the inner
        manifest dict."""
        if not isinstance(raw, dict):
            return {}
        if "result" in raw and isinstance(raw["result"], dict):
            return raw["result"]
        return raw

    def _diffusion_prepass(self, job: BlenderJob, out_dir: Path) -> None:
        req = build_texture_request(job)
        job.meta["diffusion_prompt"] = req.prompt
        if not self.diffusion.available():
            job.meta["diffusion"] = "unavailable -> procedural fallback"
            job.textures.source = "procedural"
            return
        albedo = out_dir / job.output.textures / f"{job.name}_albedo.png"
        produced = self.diffusion.generate_albedo(req, albedo)
        if produced:
            job.meta["diffusion_albedo"] = str(produced)
        else:
            job.meta["diffusion"] = "render failed -> procedural fallback"
            job.textures.source = "procedural"

    def _invoke_blender(self, job: BlenderJob, out_dir: Path, *,
                        steps: Optional[list[str]] = None,
                        reuse_blend: Optional[str] = None,
                        rig_strategy_id: str = ""):
        """Run Blender headless on one job via the SAME recipe-emitted script the
        live MCP backend uses (backend parity). Returns (rc, log_path, manifest|None)."""
        out_dir.mkdir(parents=True, exist_ok=True)
        # Identical to the live path, plus a tail that writes the manifest to disk.
        code = build_job_code(job, str(out_dir),
                              steps=(steps if steps is not None else self.live_steps),
                              write_manifest=True, reuse_blend=reuse_blend,
                              rig_strategy_id=rig_strategy_id)
        script_path = out_dir / f"{job.name}.run.py"
        script_path.write_text(code, encoding="utf-8")
        log_path = out_dir / f"{job.name}.log"
        manifest_path = out_dir / f"{job.name}.manifest.json"
        if manifest_path.exists():
            manifest_path.unlink()

        cmd = [
            str(self.blender.path),
            "--background", "--factory-startup",
            "--python", str(script_path),
        ]
        try:
            with open(log_path, "w", encoding="utf-8") as log:
                proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=self.timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            console.print(f"      [yellow]timeout[/] {job.name} exceeded {self.timeout}s")
            return 124, log_path, None
        except OSError as exc:
            log_path.write_text(f"failed to launch blender: {exc}", encoding="utf-8")
            return 127, log_path, None

        manifest = None
        if manifest_path.exists():
            try:
                manifest = AssetManifest.from_blender_report(
                    json.loads(manifest_path.read_text(encoding="utf-8")))
                self._normalize_outputs(manifest)
            except (json.JSONDecodeError, OSError, ValueError):
                manifest = None
        return rc, log_path, manifest

    def _normalize_outputs(self, manifest: AssetManifest) -> None:
        """Strip embedded timestamps from the produced FBX/Alembic so identical
        geometry yields byte-identical files. Best-effort; never fails a job."""
        if not self.normalize_exports or manifest is None:
            return
        from .normalize import normalize_export
        for path in (manifest.fbx, manifest.alembic):
            if not path:
                continue
            try:
                kind, n = normalize_export(path)
                if n:
                    manifest.operations.append(f"normalize_{kind}")
            except OSError:
                pass

    def _stub(self, job: BlenderJob, out_dir: Path, route: str,
              *, dry_run: bool) -> JobResult:
        """Produce a placeholder manifest so callers can proceed without Blender."""
        ops = ["mesh_cleanup", "uv_unwrap"]
        if job.lods:
            ops.append(f"lod_x{job.lod_levels}")
        if job.rig:
            ops.append("rigify_autorig")
        if job.animations:
            ops.append("anim_retarget")
        ops.append("export_fbx")
        alembic = None
        unity_packages: list[str] = []
        if job.soft_body:
            ops.append("export_alembic")
            alembic = str(out_dir / job.output.alembic)
            unity_packages.append("com.unity.formats.alembic")

        from .seeding import effective_seed
        manifest = AssetManifest(
            name=job.name, asset_type=job.asset_type, style=job.style, route=route,
            fbx=str(out_dir / job.output.fbx),
            blend=str(out_dir / job.output.blend),
            alembic=alembic,
            textures=[str(out_dir / job.output.textures / f"{job.name}_albedo.png")],
            lods=[f"{job.name}_LOD{i}" for i in range(job.lod_levels)] if job.lods else [],
            animations=list(job.animations),
            rigged=job.rig,
            seed=effective_seed(job),
            unity_packages=unity_packages,
            operations=ops,
            warnings=["stub: Blender unavailable — no geometry produced"
                      if not dry_run else "dry-run: nothing executed"],
            stub=True,
        )
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{job.name}.manifest.json").write_text(
                manifest.model_dump_json(indent=2), encoding="utf-8")
        return JobResult(
            name=job.name, status=JobStatus.STUBBED, route=route,
            attempts=0, manifest=manifest,
        )
