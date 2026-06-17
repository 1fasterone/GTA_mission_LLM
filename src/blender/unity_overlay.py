"""
Unity asset overlay — copy a batch's produced assets into a Unity project.

Closes the loop: the Blender Job Manager produces FBX / textures / Alembic plus a
BatchManifest; this drops those files into a generated (or existing) Unity
project's `Assets/` folder and merges any required Unity Package Manager
dependencies (e.g. the Alembic package for `.abc` clips) into
`Packages/manifest.json`.

Mirrors the merge-don't-clobber philosophy of unity_cli's overlay: never removes
or downgrades an existing dependency, only adds missing ones.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .manifest import AssetManifest, BatchManifest


# Default UPM versions for packages a Blender asset can require. Conservative,
# widely-available versions; the merge never downgrades an existing pin.
UNITY_PACKAGE_VERSIONS: dict[str, str] = {
    "com.unity.formats.alembic": "2.3.3",
}

DEFAULT_ASSETS_SUBDIR = "Assets/BlenderAssets"


class OverlayResult(BaseModel):
    copied:         list[str] = Field(default_factory=list)   # dst rel paths
    missing:        list[str] = Field(default_factory=list)   # src paths not found
    packages_added: list[str] = Field(default_factory=list)
    target:         str = ""

    @property
    def ok(self) -> bool:
        return bool(self.copied) or bool(self.packages_added)


def _copy(src: Optional[str], dst_dir: Path, res: OverlayResult,
          unity_root: Path, rename: Optional[str] = None) -> None:
    if not src:
        return
    sp = Path(src)
    if not sp.exists():
        res.missing.append(src)
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (rename or sp.name)
    shutil.copy2(sp, dst)
    try:
        res.copied.append(str(dst.relative_to(unity_root)).replace("\\", "/"))
    except ValueError:
        res.copied.append(str(dst))


def overlay_asset(manifest: AssetManifest, unity_root: Path,
                  assets_subdir: str = DEFAULT_ASSETS_SUBDIR,
                  res: Optional[OverlayResult] = None) -> OverlayResult:
    """Copy one asset's files into <unity_root>/<assets_subdir>/<name>/."""
    res = res or OverlayResult(target=str(unity_root))
    dst_dir = unity_root / assets_subdir / manifest.name
    _copy(manifest.fbx, dst_dir, res, unity_root)
    _copy(manifest.alembic, dst_dir, res, unity_root)
    for tex in manifest.textures:
        _copy(tex, dst_dir / "textures", res, unity_root)
    return res


def overlay_batch(batch: BatchManifest, unity_root: Path,
                  assets_subdir: str = DEFAULT_ASSETS_SUBDIR) -> OverlayResult:
    """Copy every produced asset in a batch into the Unity project and merge the
    union of required Unity packages into Packages/manifest.json."""
    unity_root = Path(unity_root)
    res = OverlayResult(target=str(unity_root))
    required: set[str] = set()
    for asset in batch.assets:
        overlay_asset(asset, unity_root, assets_subdir, res)
        required.update(asset.unity_packages)
    if required:
        res.packages_added = merge_packages(unity_root, required)
    return res


def merge_packages(unity_root: Path, packages: set[str]) -> list[str]:
    """Add `packages` to Packages/manifest.json (never downgrade existing).
    Returns the list of package ids actually added."""
    manifest_path = Path(unity_root) / "Packages" / "manifest.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    deps = data.setdefault("dependencies", {})
    added: list[str] = []
    for pkg in sorted(packages):
        if pkg not in deps:
            deps[pkg] = UNITY_PACKAGE_VERSIONS.get(pkg, "1.0.0")
            added.append(pkg)
    if added:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return added


def load_batch(path: str | Path) -> BatchManifest:
    """Load a BatchManifest from a batch_manifest.json on disk."""
    return BatchManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
