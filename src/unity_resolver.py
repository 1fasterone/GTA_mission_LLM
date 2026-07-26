"""
Unity package resolver — Step 4.
Maps plain-English package descriptions -> Unity Package Manager IDs + versions.
Mirrors GTA5 resolver architecture: local lookup first, RAG fallback.
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from .unity_schema import UnitySchema, UnityPackage

console = Console()

_PKG_ID_RE = re.compile(r"^com\.[a-z][a-z0-9._-]+$")

# MCP-for-Unity editor bridge (CoplayDev/unity-mcp). Installed by git URL so it
# carries no semver — it lives in its own editor assembly, so it loads and
# starts even when the generated gameplay scripts fail to compile, which is
# exactly what lets an agent read the first-open compile errors and fix them.
MCP_BRIDGE_PKG_ID = "com.coplaydev.unity-mcp"
MCP_BRIDGE_GIT_URL = "https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#main"

# Standard built-in engine modules. Unity 6 trims built-in modules from a
# freshly-written manifest unless they are listed explicitly — so a generated
# project with a lean manifest is missing the engine surface that both ordinary
# gameplay code (AudioSource, Rigidbody, …) and the injected MCP bridge rely on,
# producing a wall of `CS1069 … type has been forwarded to assembly
# UnityEngine.XxxModule`. A 3D project masks this by pulling many modules
# transitively through its packages (e.g. navigation -> physics), but a lean 2D
# project pulls almost none. The robust fix is to always ship the same module
# set Unity's own default project template writes.
#
# This is the cross-version-stable subset (present from 2021.3 through 6000.x);
# version-specific entries like `unityanalytics` (removed in Unity 6) and
# `accessibility` (added in Unity 6) are deliberately excluded. All ship at
# "1.0.0". Keyed as a dict so callers get {id: version} directly.
BUILTIN_MODULES = {f"com.unity.modules.{m}": "1.0.0" for m in (
    "ai", "androidjni", "animation", "assetbundle", "audio", "cloth",
    "director", "imageconversion", "imgui", "jsonserialize", "particlesystem",
    "physics", "physics2d", "screencapture", "terrain", "terrainphysics",
    "tilemap", "ui", "uielements", "umbra", "unitywebrequest",
    "unitywebrequestassetbundle", "unitywebrequestaudio",
    "unitywebrequesttexture", "unitywebrequestwww", "vehicles", "video",
    "vr", "wind", "xr",
)}


def _load_package_db(lookups_dir: str) -> dict:
    path = Path(lookups_dir) / "unity" / "packages.json"
    try:
        db = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    # Pre-sort synonym entries once so _local_resolve's substring scan is O(n)
    # without re-sorting on every call.
    db["_sorted_synonyms"] = sorted(db.get("synonyms", {}).items())
    return db


def _local_resolve(description: str, db: dict) -> Optional[str]:
    synonyms        = db.get("synonyms", {})
    sorted_synonyms = db.get("_sorted_synonyms", sorted(synonyms.items()))
    desc = description.lower().strip()

    if desc in synonyms:
        return synonyms[desc]

    for key, pkg_id in sorted_synonyms:
        if key in desc or desc in key:
            return pkg_id

    desc_words = set(desc.split())
    scored: list[tuple[int, str, str]] = []
    for key, pkg_id in sorted_synonyms:
        overlap = len(desc_words & set(key.split()))
        if overlap >= 1:
            scored.append((overlap, key, pkg_id))

    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))
    return scored[0][2]


def _get_version(pkg_id: str, unity_version: str, db: dict) -> Optional[str]:
    versions = db.get("versions", {})
    pkg_versions = versions.get(pkg_id, {})
    if unity_version in pkg_versions:
        return pkg_versions[unity_version]
    # Sort descending so the most-specific/newest version wins when multiple
    # keys share the same major prefix (e.g. "2022.3" beats "2022.1").
    for ver_key, ver_val in sorted(pkg_versions.items(), reverse=True):
        if unity_version.startswith(ver_key.split(".")[0]):
            return ver_val
    return None


# ── Atmosphere ⇄ Unity-version reconciliation ──────────────────────────────────
# The vendored URP-Atmosphere render feature (templates/unity/vendor/URPAtmosphere)
# is written against URP 17's RenderGraph API (RecordRenderGraph / RenderGraphModule
# / UniversalRenderer.CreateRenderGraphTexture). Those symbols do not exist in the
# URP 14 that Unity 2022.3 pins, so an atmosphere project pinned to 2022.3 ships
# render code that cannot compile. The schema-fill LLM picks unity_version from the
# brief's *vibe* (a grounded look → 2022.3) — a soft signal — whereas the atmosphere
# gate keys on concrete concept content, so when they conflict the version yields.
# See memory note unity-65-compat (PR#7): the feature is deliberately RenderGraph-only.

_URP_PKG_ID = "com.unity.render-pipelines.universal"

# URP major that introduced the RenderGraph render-pass API (URP 17 / Unity 6).
_URP_RENDERGRAPH_MAJOR = 17

# Lowest Unity version whose URP ships RenderGraph — the floor an atmosphere
# project is raised to. Present in _VERSION_MAP (unity_generator) and packages.json.
_ATMOSPHERE_MIN_UNITY = "6000.0"


def _urp_has_rendergraph(unity_version: str, db: dict) -> bool:
    """True when the URP version this Unity pins ships the RenderGraph API.

    Derived from the same packages.json map that pins the manifest, so the
    compatibility decision and the actual URP pin can never disagree. Falls back
    to the Unity major (6000+ ⇒ URP 17+) when no URP pin is mapped, which keeps a
    newer-than-mapped release from being needlessly downgraded.
    """
    urp = _get_version(_URP_PKG_ID, unity_version, db)
    if urp:
        try:
            return int(urp.split(".")[0]) >= _URP_RENDERGRAPH_MAJOR
        except (ValueError, IndexError):
            pass
    try:
        return int(unity_version.split(".")[0]) >= 6000
    except (ValueError, IndexError):
        return False


class UnityResolver:
    def __init__(self, lookups_dir: str = "lookups",
                 rag: Optional[Any] = None,
                 use_rag: bool = True):
        self.db = _load_package_db(lookups_dir)
        self.lookups_dir = lookups_dir
        self.rag = rag
        self.use_rag = use_rag and rag is not None

    def _rag_resolve(self, description: str, unity_version: str) -> Optional[str]:
        """RAG attempt — returns a validated package id or None."""
        assert self.rag is not None, "_rag_resolve requires use_rag (implies rag is set)"
        try:
            result = self.rag.resolve_package(description, unity_version)
        except Exception as exc:
            console.print(f"  [yellow]WARN[/] Unity RAG failed for '{description}' "
                          f"({type(exc).__name__}) — trying local fallback")
            return None
        if not result or not _PKG_ID_RE.match(result):
            return None
        # Accept any well-formed com.* id; prefer ones present in the version db.
        if result in self.db.get("versions", {}):
            return result
        # Still return the formally-valid id; _get_version will return None and
        # the manifest writer will fall back to the "1.0.0" default with a warn.
        return result

    def resolve_package(self, description: str, unity_version: str) -> tuple[Optional[str], Optional[str]]:
        """
        Returns (package_id, version) or (None, None) if unresolved.
        Order: explicit valid ID → RAG (when enabled) → local lookup.
        """
        # Already a valid package ID?
        desc = description.strip()
        if _PKG_ID_RE.match(desc):
            version = _get_version(desc, unity_version, self.db)
            return desc, version

        pkg_id: Optional[str] = None
        if self.use_rag:
            pkg_id = self._rag_resolve(desc, unity_version)
            if pkg_id:
                console.print(f"  [cyan]RAG[/]  '{desc}' -> {pkg_id}")

        if not pkg_id:
            pkg_id = _local_resolve(description, self.db)

        if not pkg_id:
            console.print(f"  [yellow]WARN[/] No package match for '{description}' — skipped")
            return None, None

        version = _get_version(pkg_id, unity_version, self.db)
        return pkg_id, version

    def resolve_schema(self, schema: UnitySchema) -> UnitySchema:
        """Fill package_id + version for all packages in the schema.

        Two passes:
          1. Packages with no package_id → full resolve (RAG/local + version).
          2. Packages with package_id but no version → version-only lookup.
             (The LLM frequently outputs `package_id` without a version; we
              must NOT let those slip through to build_manifest_deps' broken
              `1.0.0` fallback.)
        """
        # Reconcile the Unity version with feature requirements BEFORE any
        # package version is pinned, so every dependency (URP included) resolves
        # against the final version rather than the LLM's original pick.
        self._reconcile_atmosphere_version(schema)

        for pkg in schema.packages:
            if not pkg.package_id:
                pkg_id, version = self.resolve_package(pkg.description, schema.unity_version)
                pkg.package_id = pkg_id
                pkg.version    = version
                if pkg_id:
                    console.print(
                        f"  [green]OK[/]   '{pkg.description}' -> {pkg_id} {version or '(no version)'}"
                    )
            elif not pkg.version:
                version = _get_version(pkg.package_id, schema.unity_version, self.db)
                pkg.version = version
                if version:
                    console.print(
                        f"  [green]VER[/]  '{pkg.package_id}' -> {version}"
                    )
        self.expand_behaviors(schema)
        return schema

    def _reconcile_atmosphere_version(self, schema: UnitySchema) -> None:
        """Raise unity_version to a RenderGraph-capable release when the vendored
        URP-Atmosphere feature will ship (in-place, idempotent).

        The atmosphere render pass uses URP 17's RenderGraph API, which the URP
        14 that Unity 2022.3 pins does not have — so a 2022.3 atmosphere project
        cannot compile its own feature. The version pick is soft and the
        atmosphere gate is concrete, so we bump the version to _ATMOSPHERE_MIN_UNITY.
        No-op when the project already resolves to URP ≥17, or when the atmosphere
        feature is not gated on (see wants_atmosphere).
        """
        # Local import: wants_atmosphere lives in the generator, which is a much
        # heavier module — keep it out of the resolver's import surface.
        from .unity_generator import wants_atmosphere

        if not wants_atmosphere(schema):
            return
        if _urp_has_rendergraph(schema.unity_version, self.db):
            return

        old = schema.unity_version
        schema.unity_version = _ATMOSPHERE_MIN_UNITY

        # Baked package pins from the OLD Unity (URP 14.0.11, Cinemachine 2.9.7,
        # … for 2022.3) are wrong for the new engine. Clear the versions of
        # db-known packages so the second pass below re-pins them against the
        # bumped version. Only clears pins we can re-derive — an unknown-to-db
        # baked pin is left as-is rather than dropped. This is also what makes
        # unity-validate replay consistent: byte-identical replay deliberately
        # keeps baked pins, but a version we just moved out from under is exactly
        # the case where the baked pin must NOT survive (see unity-65-compat).
        versions_db = self.db.get("versions", {})
        repinned = 0
        for pkg in schema.packages:
            if pkg.version and pkg.package_id in versions_db:
                pkg.version = None
                repinned += 1

        console.print(
            f"  [magenta]VER[/]  atmosphere feature needs URP RenderGraph (URP "
            f"{_URP_RENDERGRAPH_MAJOR}+) — bumped Unity {old} -> {schema.unity_version}"
            + (f" (re-pinning {repinned} package(s))" if repinned else "")
        )

    # ── Atom behavior expansion ───────────────────────────────────────────────

    @staticmethod
    def expand_behaviors(schema: UnitySchema) -> None:
        """Expand entity behavior prose into AtomInvocation sequences (in-place).

        For each EntitySpec whose `behavior` string matches a known keyword:
          - fills `entity.atoms` with the corresponding atom sequence
          - rewrites `entity.behavior` to the generated C# class name so
            EntitySpawner can `AddComponent(behaviorType)` at runtime

        Entities with already-populated `atoms` (e.g. set by the LLM directly)
        are left untouched.  Entities with unknown behaviors keep their behavior
        string so a hand-authored script can still be attached.
        """
        import re
        from .atoms.behavior_map import expand_behavior

        def _pascal(s: str) -> str:
            parts = re.sub(r"[^a-zA-Z0-9]+", " ", s or "").split()
            return "".join(p.capitalize() for p in parts) or "Entity"

        for e in schema.entities:
            if e.atoms:
                # LLM already filled atoms — derive class name and move on
                if e.behavior and not e.behavior.endswith("Behavior"):
                    e.behavior = _pascal(e.name) + "Behavior"
                continue
            if not e.behavior:
                continue
            expanded = expand_behavior(e.behavior)
            if expanded:
                e.atoms    = expanded
                e.behavior = _pascal(e.name) + "Behavior"
                console.print(
                    f"  [cyan]ATM[/]  '{e.name}' behavior -> "
                    f"{len(expanded)} atoms ({e.behavior})"
                )

        # Expand custom script descriptions → atoms so the generator can
        # emit a coroutine body even when the LLM left extra_* fields empty.
        for s in schema.scripts:
            if s.type != "custom" or s.atoms:
                continue
            if not s.description:
                continue
            expanded = expand_behavior(s.description)
            if expanded:
                s.atoms = expanded
                console.print(
                    f"  [cyan]ATM[/]  '{s.name}' description -> "
                    f"{len(expanded)} atoms"
                )

    def inject_required_packages(self, schema: UnitySchema) -> UnitySchema:
        """
        Add packages required by the chosen render pipeline, game type, and feature flags.
        Deduplicates — never adds a package that's already present.
        """
        db             = self.db
        unity_version  = schema.unity_version
        existing_ids   = {p.package_id for p in schema.packages if p.package_id}

        def _add_pkg(pkg_id: str, reason: str) -> None:
            if pkg_id in existing_ids:
                return
            version = _get_version(pkg_id, unity_version, db)
            schema.packages.append(UnityPackage(
                description = pkg_id,
                package_id  = pkg_id,
                version     = version,
            ))
            existing_ids.add(pkg_id)
            console.print(f"  [magenta]INJ[/]  {pkg_id} {version or ''} ({reason})")

        # Render pipeline required packages
        for pkg_id in db.get("pipeline_packages", {}).get(schema.render_pipeline, []):
            _add_pkg(pkg_id, f"{schema.render_pipeline} pipeline")

        # Game type suggested packages
        for pkg_id in db.get("game_type_packages", {}).get(schema.game_type, []):
            _add_pkg(pkg_id, f"{schema.game_type} game type")

        # Feature flag packages
        for feature in schema.features:
            for pkg_id in db.get("feature_packages", {}).get(feature, []):
                _add_pkg(pkg_id, f"feature:{feature}")

        # Always-include packages
        for pkg_id in db.get("always_include", []):
            _add_pkg(pkg_id, "always required")

        return schema

    def build_manifest_deps(
        self, schema: UnitySchema, inject_mcp_bridge: bool = False
    ) -> dict[str, str]:
        """
        Return {package_id: version} dict for manifest.json, sorted.

        Three-tier fallback per package:
          1. Use schema-declared version if present.
          2. Re-resolve via `_get_version` against this schema's unity_version.
          3. **Skip the package entirely** with a loud warning — a missing
             package is recoverable (the tester can add it manually), but a
             bogus `1.0.0` breaks the whole manifest at import time because
             Unity rejects the whole file when one dep is unresolvable.

        When ``inject_mcp_bridge`` is set, the MCP-for-Unity editor bridge is
        added as a git-URL dependency (no version resolution — the value *is*
        the URL). This is a dev-only tool; gate it off for distribution builds.
        """
        deps: dict[str, str] = {}
        for pkg in schema.packages:
            if not pkg.package_id:
                continue
            version = pkg.version
            if not version:
                version = _get_version(pkg.package_id, schema.unity_version, self.db)
            if version:
                deps[pkg.package_id] = version
                continue
            console.print(
                f"  [yellow]WARN[/] No version known for '{pkg.package_id}' "
                f"on Unity {schema.unity_version} — SKIPPING (add it manually "
                "via Window → Package Manager if you need it)"
            )
        # Always ship the standard built-in engine modules so the project has a
        # complete engine surface under Unity 6 (see BUILTIN_MODULES). Without
        # this, lean manifests fail to compile with CS1069 "type forwarded to
        # UnityEngine.XxxModule" for both gameplay and the MCP bridge.
        added_modules = 0
        for mod_id, mod_ver in BUILTIN_MODULES.items():
            if mod_id not in deps:
                deps[mod_id] = mod_ver
                added_modules += 1
        if added_modules:
            console.print(f"  [magenta]INJ[/]  {added_modules} built-in engine modules")

        if inject_mcp_bridge:
            deps.setdefault(MCP_BRIDGE_PKG_ID, MCP_BRIDGE_GIT_URL)
            console.print(
                f"  [magenta]INJ[/]  {MCP_BRIDGE_PKG_ID} (MCP editor bridge, "
                "git URL — agent can fix first-open errors)"
            )
        return dict(sorted(deps.items()))
