"""
Deterministic artifact cache for the Blender Job Manager (Micro-Generator Phase 2).

A BlenderJob is fully determined by its spec: the procedural recipe + seeds turn
the same inputs into byte-identical geometry (proven cross-backend — same seed →
same `geo_hash` on headless subprocess *and* live MCP), and `normalize.py` strips
the only wall-clock bytes from the exports. That determinism is what makes a cache
*safe*: if we've already produced an asset for a given set of inputs, re-running
Blender is wasted work — the output cannot differ.

Two layers, deepest first:

  1. **GeoStore — the substrate (`<cache>/geo/`).** A content-addressed blob store
     keyed on *geometry identity*. Every produced file is stored once by the md5 of
     its bytes (`blobs/<content_hash>`); every result is indexed under its
     `geo_hash` (`index/<geo_hash>.json`), grouping all of a mesh's material/colour
     variants together. This layer knows nothing about jobs or names — it's keyed on
     what the geometry *is*, so it's shared across every job, project, and session.
     It dedups bytes globally and is independently queryable ("which jobs produced
     this mesh?"), the foundation later phases (asset packs, cross-project reuse)
     build on.

  2. **BlenderCache — the skip-work layer on top.** Maps an *input fingerprint*
     (everything that determines the output: geometry-determining job fields, the
     derived seeds, the chosen steps, AND a hash of the recipe/template source so a
     code change busts it) → a `geo_hash` + variant in the substrate. A fingerprint
     hit restores the stored files and skips diffusion + Blender entirely. This is
     the layer the job manager talks to; its API is unchanged.

The fingerprint is the *input* key (predicts "have we built this exact job?"); the
`geo_hash` is the *output* key (the geometry's content identity). The fingerprint
layer indexes *into* the geo substrate rather than keeping its own private copy of
the bytes — so two different jobs that yield the same mesh share storage beneath a
single `geo_hash`, while each still resolves and restores independently above.

Philosophy matches the rest of the package: best-effort, never blocks a job. Any
cache I/O failure degrades to "miss" / "don't store" and the pipeline runs Blender
as usual.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .manifest import AssetManifest
from .rigging  import rig_strategy
from .seeding  import effective_seed, recipe_seed, diffusion_seed

# Bump when the cache layout or fingerprint composition changes in a way that
# should invalidate every existing entry. (v2 = two-layer geo substrate.)
CACHE_VERSION = 2

# Geometry-fingerprint versions. GEO_FP_VERSION bumps the geo-fp scheme itself;
# GEO_TEMPLATE_VERSION bumps when the *geometry-building* portion of the emitted
# bpy script changes (so a cached pre-rig mesh is correctly invalidated). These
# are intentionally separate from the full-result fingerprint so a material/rig
# code change does NOT needlessly bust reusable geometry.
GEO_FP_VERSION = 1
GEO_TEMPLATE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _source_hash() -> str:
    """md5 of the emitted-script template + material source. Folded into every
    fingerprint so editing the recipe template/material busts the whole cache
    (the geometry the script produces would change)."""
    try:
        from . import mcp_backend as mb
        blob = (mb._TEMPLATE + mb._MATERIAL_SRC).encode("utf-8")
        return hashlib.md5(blob).hexdigest()
    except Exception:  # noqa: BLE001 - never let fingerprinting raise
        return "0"


def _recipe_hash(job) -> str:
    """md5 of the specific procedural recipe this job will run — editing that
    recipe busts only jobs that use it."""
    try:
        from .recipes import recipe_for, recipe_source
        name = recipe_for(job.asset_type, job.geometry_nodes)
        return hashlib.md5(recipe_source(name).encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001
        return "0"


class CacheEntry(BaseModel):
    """A resolved fingerprint hit: the manifest as produced + the substrate
    coordinates (geo identity + the stored variant) needed to restore it."""
    fingerprint: str
    geo_key:     str           = ""            # index name in the substrate
    geo_hash:    Optional[str] = None          # the real geometry hash (provenance)
    manifest:    AssetManifest
    variant:     dict          = Field(default_factory=dict)  # blob coordinates


# ── layer 1: the geometry substrate ───────────────────────────────────────────

class GeoStore:
    """Content-addressed substrate, keyed on geometry identity.

    Layout under `root`::

        blobs/<content_hash>      raw artifact bytes, stored once (global dedup)
        index/<geo_hash>.json     { geo_hash, tris, variants: { <fingerprint>: {
                                       manifest, blobs: {fbx,blend,alembic,textures}
                                    } } }

    Each entry in `variants` is one material/colour realisation of the *same* mesh;
    its `blobs` reference shared content-hashed bytes, so identical bytes (a re-run,
    a shared texture) are never duplicated.
    """

    def __init__(self, root):
        self.root  = Path(root)
        self.blobs = self.root / "blobs"
        self.index = self.root / "index"
        self.mesh  = self.root / "mesh"   # pre-rig reusable mesh, keyed by geo-fp

    # blob CAS
    def put_blob(self, path) -> str:
        ch = _file_md5(Path(path))
        dst = self.blobs / ch
        if not dst.exists():
            _copy(Path(path), dst)
        return ch

    def has_blob(self, ch: str) -> bool:
        return bool(ch) and (self.blobs / ch).is_file()

    def blob_path(self, ch: str) -> Path:
        return self.blobs / ch

    # geo index
    def _index_path(self, geo_key: str) -> Path:
        return self.index / f"{geo_key}.json"

    def load_geo(self, geo_key: str) -> Optional[dict]:
        p = self._index_path(geo_key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def put_variant(self, geo_key: str, fingerprint: str, manifest: AssetManifest) -> dict:
        """Ingest `manifest`'s files into the blob store and file them as one
        variant under `geo_key`. Returns the updated geo record."""
        blobs: dict = {"fbx": None, "blend": None, "alembic": None, "textures": []}
        for role in ("fbx", "blend", "alembic"):
            p = getattr(manifest, role, None)
            if p and Path(p).is_file():
                blobs[role] = self.put_blob(p)
        for t in manifest.textures:
            if t and Path(t).is_file():
                blobs["textures"].append({"name": Path(t).name, "blob": self.put_blob(t)})

        rec = self.load_geo(geo_key) or {
            "geo_hash": manifest.geo_hash,
            "geo_key":  geo_key,
            "tris":     manifest.tris,
            "created":  _now(),
            "variants": {},
        }
        rec["variants"][fingerprint] = {
            "created":  _now(),
            "manifest": manifest.model_dump(),
            "blobs":    blobs,
        }
        self._index_path(geo_key).parent.mkdir(parents=True, exist_ok=True)
        self._index_path(geo_key).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return rec

    def get_variant(self, geo_key: str, fingerprint: str) -> Optional[dict]:
        rec = self.load_geo(geo_key)
        if not rec:
            return None
        return rec.get("variants", {}).get(fingerprint)

    def restore_variant(self, variant: dict, out_dir, job) -> AssetManifest:
        """Copy a variant's blobs into `out_dir` (named per `job.output`) and
        return a manifest whose paths point at the restored files."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        m = AssetManifest.model_validate(variant["manifest"])
        blobs = variant.get("blobs", {})

        def _restore(role: str, dst: Path) -> Optional[str]:
            ch = blobs.get(role)
            if self.has_blob(ch):
                _copy(self.blob_path(ch), dst)
                return str(dst)
            return None

        m.fbx     = _restore("fbx",     out_dir / job.output.fbx)     or m.fbx
        m.blend   = _restore("blend",   out_dir / job.output.blend)   or m.blend
        m.alembic = _restore("alembic", out_dir / job.output.alembic) or m.alembic

        tex_dir = out_dir / job.output.textures
        restored: list[str] = []
        for t in blobs.get("textures", []):
            ch, name = t.get("blob"), t.get("name")
            if self.has_blob(ch) and name:
                dst = tex_dir / name
                _copy(self.blob_path(ch), dst)
                restored.append(str(dst))
        m.textures = restored

        if "cache_hit" not in m.operations:
            m.operations = list(m.operations) + ["cache_hit"]
        return m

    # mesh layer — reusable PRE-RIG geometry, keyed by geometry fingerprint
    def _mesh_path(self, geo_fp: str) -> Path:
        return self.mesh / f"{geo_fp}.json"

    def put_mesh(self, geo_fp: str, blend_path, geo_hash: Optional[str],
                 tris: Optional[int]) -> Optional[str]:
        """Store a pre-rig mesh `.blend` under its geometry fingerprint. Returns
        the blob hash, or None when the file doesn't exist."""
        blend_path = Path(blend_path)
        if not blend_path.is_file():
            return None
        ch = self.put_blob(blend_path)
        rec = {"geo_fp": geo_fp, "blend": ch, "geo_hash": geo_hash,
               "tris": tris, "created": _now()}
        self._mesh_path(geo_fp).parent.mkdir(parents=True, exist_ok=True)
        self._mesh_path(geo_fp).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return ch

    def get_mesh(self, geo_fp: str) -> Optional[dict]:
        """Return the stored mesh record for `geo_fp` (with a live `blend_path`
        into the blob store), or None if absent / the blob has gone missing."""
        p = self._mesh_path(geo_fp)
        if not p.is_file():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not self.has_blob(rec.get("blend", "")):
            return None
        rec["blend_path"] = str(self.blob_path(rec["blend"]))
        return rec

    def restore_mesh(self, geo_fp: str, dst) -> Optional[Path]:
        """Copy a cached pre-rig mesh `.blend` to `dst`. Returns the path or None."""
        rec = self.get_mesh(geo_fp)
        if not rec:
            return None
        dst = Path(dst)
        _copy(self.blob_path(rec["blend"]), dst)
        return dst

    def stats(self) -> dict:
        geo_hashes = variants = blob_count = size = meshes = 0
        if self.index.exists():
            for f in self.index.glob("*.json"):
                geo_hashes += 1
                try:
                    variants += len(json.loads(f.read_text(encoding="utf-8")).get("variants", {}))
                except (json.JSONDecodeError, OSError):
                    pass
        if self.mesh.exists():
            meshes = len(list(self.mesh.glob("*.json")))
        if self.blobs.exists():
            for b in self.blobs.iterdir():
                if b.is_file():
                    blob_count += 1
                    size += b.stat().st_size
        return {"geo_hashes": geo_hashes, "variants": variants, "meshes": meshes,
                "blobs": blob_count, "bytes": size}


# ── layer 2: the skip-work fingerprint cache ──────────────────────────────────

class BlenderCache:
    """Input-fingerprint cache layered on the GeoStore substrate.

    Parameters
    ----------
    cache_dir : path
        Root for both layers (`<cache_dir>/geo/` substrate, `<cache_dir>/fp/`
        fingerprint pointers).
    enabled : bool
        Off → every lookup misses and nothing is stored.
    """

    def __init__(self, cache_dir, enabled: bool = True):
        self.cache_dir = Path(cache_dir)
        self.enabled   = bool(enabled)
        self.geo       = GeoStore(self.cache_dir / "geo")
        self.fp_dir    = self.cache_dir / "fp"
        # Set by the manager. The Blender version is folded into the geometry
        # fingerprint (a `.blend` is version-coupled, so an upgrade must miss
        # cleanly); template_root decides whether a template rig micro resolves or
        # falls back to auto, which changes the result fingerprint.
        self.blender_version: str = "unknown"
        self.template_root: Optional[str] = None

    # ── key ─────────────────────────────────────────────────────────────────

    def fingerprint(self, job, steps: list[str]) -> str:
        """Stable hash of everything that determines `job`'s output.

        Deliberately EXCLUDES `job.name`, `job.meta`, and `job.output` paths:
        those don't change the mesh. The one way a name *does* affect geometry —
        seeding (an un-pinned seed derives from the name) — is captured by folding
        in the resolved `effective_seed`/`recipe_seed` instead.
        """
        tex = job.textures
        blob = {
            "v":              CACHE_VERSION,
            "asset_type":     job.asset_type,
            "style":          job.style,
            "description":    job.description,
            "detail":         job.detail,
            "color":          job.color,
            "geometry_nodes": job.geometry_nodes,
            "soft_body":      job.soft_body,
            "rig":            job.rig,
            "animations":     sorted(job.animations),
            "lods":           job.lods,
            "lod_levels":     job.lod_levels,
            "textures": {
                "resolution":     tex.resolution,
                "bake":           tex.bake,
                "source":         tex.source,
                "material_brief": tex.material_brief,
            },
            "effective_seed": effective_seed(job),
            "recipe_seed":    recipe_seed(job),
            "diffusion_seed": diffusion_seed(job) if job.needs_diffusion() else None,
            "steps":          list(steps),
            "recipe_hash":    _recipe_hash(job),
            "source_hash":    _source_hash(),
            # Which versioned rig micro will run (template-vs-auto changes output).
            "rig_strategy":   rig_strategy(job, self.template_root).id,
        }
        canon = json.dumps(blob, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def geo_fingerprint(self, job) -> str:
        """Fingerprint of the **pre-rig mesh** only — the reusable geometry unit.

        Deliberately NARROWER than `fingerprint`: it folds in only what shapes the
        base mesh (recipe + `recipe_seed` + detail + asset_type/geometry_nodes +
        the geometry-portion template version + the Blender version, since the
        cached unit is a version-coupled `.blend`). It EXCLUDES colour, material,
        textures, diffusion, rig, and animations — those are applied *after* the
        mesh is reused, so two material/colour variants share one cached mesh.
        """
        blob = {
            "gv":             GEO_FP_VERSION,
            "tv":             GEO_TEMPLATE_VERSION,
            "asset_type":     job.asset_type,
            "detail":         job.detail,
            "geometry_nodes": job.geometry_nodes,
            "recipe_seed":    recipe_seed(job),
            "recipe_hash":    _recipe_hash(job),
            "blender":        self.blender_version,
        }
        canon = json.dumps(blob, sort_keys=True, separators=(",", ":"))
        return "geo-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _fp_path(self, fingerprint: str) -> Path:
        return self.fp_dir / f"{fingerprint}.json"

    # ── read ────────────────────────────────────────────────────────────────

    def lookup(self, fingerprint: str) -> Optional[CacheEntry]:
        """Resolve a fingerprint to its stored variant in the substrate, or None
        on a miss / disabled cache / dangling pointer."""
        if not self.enabled:
            return None
        ptr_path = self._fp_path(fingerprint)
        if not ptr_path.is_file():
            return None
        try:
            ptr = json.loads(ptr_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        geo_key = ptr.get("geo_key", "")
        variant = self.geo.get_variant(geo_key, fingerprint)
        if not variant:
            return None
        manifest = AssetManifest.model_validate(variant["manifest"])
        return CacheEntry(fingerprint=fingerprint, geo_key=geo_key,
                          geo_hash=manifest.geo_hash, manifest=manifest, variant=variant)

    def restore(self, entry: CacheEntry, out_dir, job) -> AssetManifest:
        """Restore a hit's files (from the substrate) into `out_dir`."""
        return self.geo.restore_variant(entry.variant, out_dir, job)

    # ── write ───────────────────────────────────────────────────────────────

    def store(self, fingerprint: str, manifest: AssetManifest, out_dir, job) -> Optional[str]:
        """File `manifest` into the substrate under its geo identity and write the
        fingerprint pointer. Returns the geo key, or None if disabled.

        When the manifest carries no `geo_hash` (e.g. a backend that didn't report
        one) we fall back to keying the variant under the fingerprint itself, so the
        skip-work layer still works — it just can't dedup that result across meshes.
        """
        if not self.enabled:
            return None
        geo_key = manifest.geo_hash or f"fp-{fingerprint[:32]}"
        self.geo.put_variant(geo_key, fingerprint, manifest)
        self.fp_dir.mkdir(parents=True, exist_ok=True)
        self._fp_path(fingerprint).write_text(
            json.dumps({"geo_key": geo_key, "geo_hash": manifest.geo_hash}, indent=2),
            encoding="utf-8")
        return geo_key

    # ── maintenance ───────────────────────────────────────────────────────────

    def clear(self) -> int:
        """Delete both layers. Returns the number of fingerprint pointers removed."""
        n = len(list(self.fp_dir.glob("*.json"))) if self.fp_dir.exists() else 0
        shutil.rmtree(self.geo.root, ignore_errors=True)
        shutil.rmtree(self.fp_dir, ignore_errors=True)
        return n

    def stats(self) -> dict:
        """Combined summary across both layers."""
        fps = len(list(self.fp_dir.glob("*.json"))) if self.fp_dir.exists() else 0
        return {"dir": str(self.cache_dir), "fingerprints": fps, **self.geo.stats()}
