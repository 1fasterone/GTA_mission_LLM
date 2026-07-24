"""
Offline-first Unity package cache — fill once, prime before every first open.

The problem this closes
-----------------------
A generated project's ``Packages/manifest.json`` names ~9 registry packages (URP,
shadergraph, cinemachine, inputsystem, …). On first open Unity resolves each one
against ``packages.unity.com``, downloads a tarball, and extracts it into its
*global* package cache. That is a multi-minute network stall on every fresh
project, repeated for every project, even though the bytes are identical each
time.

Unity already has the right mechanism — the global cache is shared across every
project on the machine, so a package present there is never re-downloaded. It
just needs to be *populated ahead of time*. That is all this module does:

  1. **fill**  — resolve the dependency closure, download each tarball from the
     registry once, and store it under a repo-local cache directory.
  2. **prime** — copy the repo cache into Unity's global cache in the two layouts
     UPM reads, so the next project to open finds every package already there.

Two layouts, both required
--------------------------
Verified against a live Unity install (``%LOCALAPPDATA%\\Unity\\cache``)::

    npm/packages.unity.com/<pkg>/<ver>/package.tgz   # the downloaded tarball
    npm/packages.unity.com/<pkg>/<ver>/package/      # its extracted contents
    npm/packages.unity.com/<pkg>/<ver>/signature     # {"status":"Valid","reason":""}
    packages/packages.unity.com/<pkg>@<ver>/         # extracted, this is what UPM resolves

The ``packages/`` entry is what a project actually resolves against; the ``npm/``
entry is the download-layer cache behind it. Priming both means UPM finds a hit
at whichever layer it consults first.

The closure is not optional
---------------------------
``manifest.json`` lists only direct dependencies. Caching just those still leaves
Unity fetching the rest of the graph — e.g. ``com.unity.cinemachine@3.1.7``
depends on ``com.unity.splines@2.0.0``, which no generated manifest mentions. So
``resolve_closure`` walks the whole graph before anything is downloaded.

``com.unity.modules.*`` are deliberately excluded: those are built-in engine
modules that ship inside the editor and are never fetched from a registry.
Non-registry dependencies (the ``git+``/``file:`` MCP bridge) are skipped for the
same reason — there is no registry tarball to cache.

Philosophy
----------
Best-effort, never blocks a build. Every network and filesystem failure degrades
to "this package stays uncached" and the project still generates — Unity just
downloads that one on open, exactly as it does today. Priming NEVER deletes or
overwrites an existing global-cache entry: a populated ``<pkg>@<ver>`` belongs to
the user's editor, not to us, and a half-written one is worse than none.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

_log = logging.getLogger(__name__)

# Unity's public package registry. Metadata lives here; the tarballs it points at
# are served from download.packages.unity.com (a different host — always follow
# the `dist.tarball` URL from the metadata rather than rebuilding it by hand).
REGISTRY_URL = "https://packages.unity.com"
# Per-registry subdirectory name inside the global cache.
REGISTRY_DIR = "packages.unity.com"

# Shipped inside the editor — never resolved against a registry, so caching them
# is both impossible and pointless.
BUILTIN_PREFIX = "com.unity.modules."

# Written next to a primed tarball. Unity records the result of its signature
# check here; "Valid" is what a normal registry download produces.
_SIGNATURE_BODY = '{\n  "status": "Valid",\n  "reason": ""\n}'

_NETWORK_TIMEOUT = 60


class PackageCacheError(RuntimeError):
    """Raised only by explicitly-invoked CLI paths; the pipeline hook swallows."""


@dataclass(frozen=True)
class PackagePin:
    """One resolved registry package: an exact version plus how to fetch it."""

    name: str
    version: str
    tarball: str
    shasum: str = ""
    dependencies: dict[str, str] = field(default_factory=dict)
    depth: int = 0

    @property
    def slug(self) -> str:
        """`<pkg>@<ver>` — the directory name UPM uses in the packages/ layout."""
        return f"{self.name}@{self.version}"


@dataclass
class CacheReport:
    """What a fill/prime pass actually did — printed by the CLI, logged by the pipeline."""

    resolved: int = 0
    downloaded: list[str] = field(default_factory=list)
    already_cached: list[str] = field(default_factory=list)
    primed: list[str] = field(default_factory=list)
    already_global: list[str] = field(default_factory=list)
    # Pinned versions the registry does not publish. Normal, not an error: the
    # SRP and UI packages (URP, shadergraph, ugui, ui) ship inside the editor
    # under Data/Resources/PackageManager/BuiltInPackages and are resolved from
    # there — packages.unity.com only carries their pre-9.0 history. Nothing to
    # cache, because nothing is ever downloaded.
    not_on_registry: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{self.resolved} package(s) in closure"]
        if self.downloaded:
            parts.append(f"{len(self.downloaded)} downloaded")
        if self.already_cached:
            parts.append(f"{len(self.already_cached)} already in local cache")
        if self.primed:
            parts.append(f"{len(self.primed)} primed into the global cache")
        if self.already_global:
            parts.append(f"{len(self.already_global)} already in the global cache")
        if self.not_on_registry:
            parts.append(f"{len(self.not_on_registry)} editor-bundled (not on registry)")
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        return ", ".join(parts)


# ── Locating Unity's global cache ─────────────────────────────────────────────

def unity_cache_root() -> Path:
    """Unity's global package-cache root.

    ``UPM_CACHE_ROOT`` wins when set — that is Unity's own override, so honoring
    it keeps us pointed wherever the user already redirected the editor.

    The Windows path is verified against a live install. The macOS/Linux
    defaults follow Unity's documented locations but are NOT verified here; set
    ``UPM_CACHE_ROOT`` (or config ``package_cache.global_root``) if they are
    wrong on your machine.
    """
    override = os.environ.get("UPM_CACHE_ROOT")
    if override:
        return Path(override)

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Unity" / "cache"
        return Path.home() / "AppData" / "Local" / "Unity" / "cache"
    if system == "Darwin":
        return Path.home() / "Library" / "Unity" / "cache"
    return Path.home() / ".config" / "unity3d" / "cache"


def global_package_dir(pin: PackagePin, root: Optional[Path] = None) -> Path:
    """Where UPM looks for an extracted package."""
    return (root or unity_cache_root()) / "packages" / REGISTRY_DIR / pin.slug


def global_npm_dir(pin: PackagePin, root: Optional[Path] = None) -> Path:
    """Where UPM keeps the downloaded tarball behind that extracted copy."""
    return (root or unity_cache_root()) / "npm" / REGISTRY_DIR / pin.name / pin.version


def is_globally_cached(pin: PackagePin, root: Optional[Path] = None) -> bool:
    """True when Unity would already resolve this package without a download.

    Checks for the package manifest rather than just the directory: an empty or
    half-extracted folder is not a usable cache entry, and treating it as one
    would let a broken package silently survive a re-prime.
    """
    return (global_package_dir(pin, root) / "package.json").is_file()


# ── Registry ──────────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = _NETWORK_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "gta-mission-llm-pkgcache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        return resp.read()


def fetch_metadata(package: str, meta_dir: Optional[Path] = None) -> dict:
    """Registry metadata for one package, memoized on disk.

    The document lists every published version, so it is large and slow to
    fetch; caching it means a re-fill only pays for packages it has not seen.
    """
    if meta_dir is not None:
        meta_dir.mkdir(parents=True, exist_ok=True)
        cached = meta_dir / f"{package}.json"
        if cached.is_file():
            try:
                return json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _log.debug("[pkgcache] stale metadata for %s — refetching", package)

    raw = _http_get(f"{REGISTRY_URL}/{package}")
    doc = json.loads(raw.decode("utf-8"))
    if meta_dir is not None:
        try:
            (meta_dir / f"{package}.json").write_bytes(raw)
        except OSError:
            pass
    return doc


def _is_registry_dep(name: str, version: str) -> bool:
    """Registry packages only — built-in modules and git/file deps have no tarball."""
    if name.startswith(BUILTIN_PREFIX):
        return False
    lowered = version.lower()
    return not lowered.startswith(("http", "git", "file:", "ssh"))


def resolve_closure(
    dependencies: dict[str, str],
    meta_dir: Optional[Path] = None,
    report: Optional[CacheReport] = None,
) -> dict[str, PackagePin]:
    """Walk the dependency graph breadth-first into a flat {name: pin} map.

    Direct manifest dependencies are only the surface: caching those alone still
    leaves Unity fetching everything they pull in. A package already seen at a
    shallower depth is not revisited — first pin wins, which keeps the result
    stable regardless of iteration order.
    """
    resolved: dict[str, PackagePin] = {}
    # Every (name, version) we have already tried. `resolved` alone is not
    # enough: a package that fails to resolve never lands there, so a second
    # parent depending on it would re-fetch and re-report it.
    attempted: set[tuple[str, str]] = set()
    queue: list[tuple[str, str, int]] = [
        (n, v, 0) for n, v in sorted(dependencies.items()) if _is_registry_dep(n, v)
    ]

    while queue:
        name, version, depth = queue.pop(0)
        if name in resolved or (name, version) in attempted:
            continue
        attempted.add((name, version))
        try:
            meta = fetch_metadata(name, meta_dir)
            entry = meta.get("versions", {}).get(version)
            if entry is None:
                # Not a failure. The editor bundles the SRP/UI packages, so a
                # Unity 6 project pins URP 17.x while the registry's history
                # stops at 8.3.1 — resolved from the editor install, never
                # downloaded, nothing for this cache to do.
                _log.info("[pkgcache] %s@%s not on the registry — editor-bundled "
                          "or an unpublished pin; skipping", name, version)
                if report is not None:
                    report.not_on_registry.append(f"{name}@{version}")
                continue
            dist = entry.get("dist", {})
            tarball = dist.get("tarball")
            if not tarball:
                raise PackageCacheError(f"{name}@{version} has no tarball URL")
            deps = {k: v for k, v in (entry.get("dependencies") or {}).items()}
            pin = PackagePin(name=name, version=version, tarball=tarball,
                             shasum=dist.get("shasum", ""), dependencies=deps,
                             depth=depth)
        except (urllib.error.URLError, PackageCacheError, json.JSONDecodeError,
                OSError, TimeoutError) as exc:
            _log.warning("[pkgcache] cannot resolve %s@%s: %s", name, version, exc)
            if report is not None:
                report.failed[f"{name}@{version}"] = str(exc)
            continue

        resolved[name] = pin
        for dep_name, dep_ver in sorted(pin.dependencies.items()):
            if dep_name not in resolved and _is_registry_dep(dep_name, dep_ver):
                queue.append((dep_name, dep_ver, depth + 1))

    if report is not None:
        report.resolved = len(resolved)
    return resolved


# ── Local cache ───────────────────────────────────────────────────────────────

def local_entry_dir(cache_dir: Path, pin: PackagePin) -> Path:
    """Repo-local home for one package version.

    Mirrors UPM's npm layout (``<pkg>/<ver>/``) so priming is a plain copy rather
    than a restructure.
    """
    return cache_dir / REGISTRY_DIR / pin.name / pin.version


def _safe_extract(tgz: Path, dest: Path) -> None:
    """Extract a registry tarball, refusing entries that escape `dest`.

    A tarball is remote input: an entry named ``../../evil`` would otherwise
    write outside the cache. Python 3.12 makes ``filter="data"`` the default;
    3.11 supports it opt-in, and the manual walk covers anything older.
    """
    with tarfile.open(tgz, "r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")  # type: ignore[call-arg]
            return
        except TypeError:
            pass
        root = dest.resolve()
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(root)):
                raise PackageCacheError(
                    f"refusing tar entry outside cache: {member.name}")
        tf.extractall(dest)  # noqa: S202 (members validated above)


def download(pin: PackagePin, cache_dir: Path, report: Optional[CacheReport] = None) -> bool:
    """Fetch one package into the local cache. Returns True when bytes were written.

    A complete entry is left alone, so re-running ``fill`` is cheap and additive.
    The download lands in a temp directory and is moved into place only once it
    is fully extracted — an interrupted run leaves no half-entry that a later
    prime would mistake for a good one.
    """
    entry = local_entry_dir(cache_dir, pin)
    if (entry / "package" / "package.json").is_file():
        if report is not None:
            report.already_cached.append(pin.slug)
        return False

    try:
        blob = _http_get(pin.tarball)
        if pin.shasum:
            actual = hashlib.sha1(blob).hexdigest()  # noqa: S324 (registry's own digest)
            if actual != pin.shasum:
                raise PackageCacheError(
                    f"checksum mismatch for {pin.slug}: {actual} != {pin.shasum}")

        entry.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(entry.parent)) as tmp:
            staging = Path(tmp)
            tgz = staging / "package.tgz"
            tgz.write_bytes(blob)
            _safe_extract(tgz, staging)
            if not (staging / "package" / "package.json").is_file():
                raise PackageCacheError(f"{pin.slug} tarball has no package/package.json")
            (staging / "signature").write_text(_SIGNATURE_BODY, encoding="utf-8")
            if entry.exists():
                shutil.rmtree(entry, ignore_errors=True)
            shutil.move(str(staging), str(entry))
    except (urllib.error.URLError, PackageCacheError, OSError, tarfile.TarError,
            TimeoutError) as exc:
        _log.warning("[pkgcache] download failed for %s: %s", pin.slug, exc)
        if report is not None:
            report.failed[pin.slug] = str(exc)
        return False

    if report is not None:
        report.downloaded.append(pin.slug)
    return True


def fill(
    dependencies: dict[str, str],
    cache_dir: Path,
    report: Optional[CacheReport] = None,
) -> dict[str, PackagePin]:
    """Resolve the closure and download everything missing from the local cache."""
    report = report if report is not None else CacheReport()
    cache_dir.mkdir(parents=True, exist_ok=True)
    pins = resolve_closure(dependencies, meta_dir=cache_dir / "_metadata", report=report)
    for pin in sorted(pins.values(), key=lambda p: p.slug):
        download(pin, cache_dir, report)
    return pins


# ── Priming Unity's global cache ──────────────────────────────────────────────

def prime(
    pins: Iterable[PackagePin],
    cache_dir: Path,
    global_root: Optional[Path] = None,
    report: Optional[CacheReport] = None,
) -> CacheReport:
    """Copy local cache entries into Unity's global cache.

    Skips anything the editor already has — an existing ``<pkg>@<ver>`` is the
    user's own downloaded copy and overwriting it risks replacing a good package
    with a worse one for no gain. Each destination is staged beside its target
    and moved into place, so a crash mid-copy cannot leave UPM reading a
    partially-written package.
    """
    report = report if report is not None else CacheReport()
    root = global_root or unity_cache_root()

    for pin in sorted(pins, key=lambda p: p.slug):
        entry = local_entry_dir(cache_dir, pin)
        source = entry / "package"
        if not (source / "package.json").is_file():
            continue  # never filled (or fill failed) — nothing to prime
        if is_globally_cached(pin, root):
            report.already_global.append(pin.slug)
            continue

        try:
            pkg_dest = global_package_dir(pin, root)
            pkg_dest.parent.mkdir(parents=True, exist_ok=True)
            _stage_copytree(source, pkg_dest)

            npm_dest = global_npm_dir(pin, root)
            if not (npm_dest / "package" / "package.json").is_file():
                npm_dest.parent.mkdir(parents=True, exist_ok=True)
                _stage_copytree(entry, npm_dest)
        except OSError as exc:
            _log.warning("[pkgcache] prime failed for %s: %s", pin.slug, exc)
            report.failed[pin.slug] = str(exc)
            continue

        report.primed.append(pin.slug)

    return report


def _stage_copytree(source: Path, dest: Path) -> None:
    """Copy `source` to `dest` via a sibling temp dir, then move it into place."""
    if dest.exists():
        return
    with tempfile.TemporaryDirectory(dir=str(dest.parent)) as tmp:
        staged = Path(tmp) / dest.name
        shutil.copytree(source, staged)
        if dest.exists():  # lost a race with another run — theirs is fine
            return
        shutil.move(str(staged), str(dest))


def ensure_cached(
    dependencies: dict[str, str],
    cache_dir: Path,
    global_root: Optional[Path] = None,
    download_missing: bool = True,
) -> CacheReport:
    """Fill (optionally) then prime — the one call the pipeline makes.

    With ``download_missing=False`` nothing is fetched; whatever is already in
    the local cache is still primed, which is the useful behaviour offline.
    """
    report = CacheReport()
    cache_dir = Path(cache_dir)
    if download_missing:
        pins = fill(dependencies, cache_dir, report)
    else:
        pins = resolve_closure(dependencies, meta_dir=cache_dir / "_metadata",
                               report=report)
    prime(pins.values(), cache_dir, global_root, report)
    return report


# ── packages-lock.json ────────────────────────────────────────────────────────

def build_lock(pins: dict[str, PackagePin], dependencies: dict[str, str]) -> dict:
    """Render a ``Packages/packages-lock.json`` for a resolved closure.

    A warm cache alone does not make first open offline: without a lock file
    Unity still contacts the registry to resolve the graph, and only *then*
    discovers the packages are already local. The lock file is what lets it skip
    resolution — the cache is what makes the skip safe.

    Built-in ``com.unity.modules.*`` are emitted as ``source: "builtin"`` with no
    version pin, matching what Unity writes for them.
    """
    locked: dict[str, dict] = {}
    for name, pin in sorted(pins.items()):
        locked[name] = {
            "version": pin.version,
            "depth": pin.depth,
            "source": "registry",
            "dependencies": dict(sorted(pin.dependencies.items())),
            "url": REGISTRY_URL,
        }
    for name, version in sorted(dependencies.items()):
        if name.startswith(BUILTIN_PREFIX):
            locked[name] = {"version": version, "depth": 0,
                            "source": "builtin", "dependencies": {}}
    return {"dependencies": dict(sorted(locked.items()))}
