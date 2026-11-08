"""Real Unity reference assemblies for SDK Roslyn consumer checks.

R0 framework decision: framework member truth comes from the Unity Editor's
own reference assemblies, never from a copied Python member table.  The
selected-source Roslyn check (`unity_component_check.check_candidate`) binds
authored C# against exactly the paths this module resolves, so an inherited
MonoBehaviour member, a GetComponent overload or a UnityEngine type is judged
by the compiler that will later build the project.

Configuration (config.yaml)::

    component_api:
      unity_editor: C:/Program Files/Unity/Hub/Editor/6000.1.4f1
      package_assemblies: []   # optional: compiled package DLLs or folders

`unity_editor` is the Hub install root (the folder that contains `Editor/`).
Runtime module assemblies come from `Editor/Data/Managed/UnityEngine`
(UnityEngine*.dll only; UnityEditor assemblies are never runtime-visible) and
the .NET Standard 2.1 surface from `Editor/Data/NetStandard/ref/2.1.0`.

Packages (uGUI, TextMeshPro, Input System) compile per project and ship no
assembly inside the Editor.  `package_assemblies` may name compiled package
DLLs, or folders of them, from an already-imported project's
`Library/ScriptAssemblies`.  Without them, a namespace such as `TMPro` stays
unresolved; consumers report that as an unverified scope rather than as an
authoring error (see `unity_authored_check`).

Absence is a loud abstention with an explicit remedy, never a silent fallback
to an SDK-only compilation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .ident import fhash


class ReferenceProfileUnavailable(RuntimeError):
    """No usable Unity reference profile is configured on this machine."""

    def __init__(self, message: str):
        super().__init__(f"[unity-references] {message}")


@dataclass(frozen=True)
class ReferenceProfile:
    """Sorted, de-duplicated reference assembly paths plus their identity."""

    editor: str
    editor_version: str
    paths: tuple[Path, ...]
    package_assemblies: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        """Portable evidence: assembly names and content hashes, no host paths."""
        return {
            "format": "unity-reference-profile/v1",
            "editor_version": self.editor_version,
            "assemblies": [{"name": p.name, "content_hash": fhash(p)} for p in self.paths],
            "package_assemblies": [Path(p).name for p in self.package_assemblies],
        }


_REMEDY = ("set component_api.unity_editor in config.yaml to a Unity Hub install "
           "root such as 'C:/Program Files/Unity/Hub/Editor/6000.1.4f1'")


def _settings(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    section = (config or {}).get("component_api") or {}
    if not isinstance(section, Mapping):
        raise ReferenceProfileUnavailable(
            f"component_api must be a mapping, got {type(section).__name__}; {_REMEDY}")
    return section


def _package_paths(entries: Any) -> list[Path]:
    if entries in (None, ""):
        return []
    if not isinstance(entries, (list, tuple)):
        raise ReferenceProfileUnavailable(
            "component_api.package_assemblies must be a list of DLL files or folders")
    found: list[Path] = []
    for raw in entries:
        path = Path(str(raw)).expanduser()
        if path.is_file() and path.suffix.lower() == ".dll":
            found.append(path)
        elif path.is_dir():
            # Editor-only package assemblies (*.Editor.dll) are never visible
            # to runtime scripts, so admitting them would bind code Unity's
            # player build rejects. Assembly-CSharp*.dll is the imported
            # project's OWN compiled scripts: admitting it would declare every
            # generated type twice (CS0433) and bind against stale source.
            found.extend(p for p in path.glob("*.dll")
                         if ".Editor" not in p.name
                         and not p.name.startswith("Assembly-CSharp"))
        else:
            raise ReferenceProfileUnavailable(
                f"package assembly path {str(path)!r} does not exist")
    return found


def unity_reference_profile(config: Mapping[str, Any] | None) -> ReferenceProfile:
    """Resolve the configured Unity reference profile or abstain loudly."""
    settings = _settings(config)
    editor_raw = str(settings.get("unity_editor") or "").strip()
    if not editor_raw:
        raise ReferenceProfileUnavailable(f"no Unity Editor configured; {_REMEDY}")
    editor = Path(editor_raw).expanduser()
    data = editor / "Editor" / "Data"
    managed = data / "Managed" / "UnityEngine"
    netstandard = data / "NetStandard" / "ref" / "2.1.0" / "netstandard.dll"
    if not managed.is_dir():
        raise ReferenceProfileUnavailable(
            f"{str(managed)!r} is missing — {editor_raw!r} is not a Unity Editor "
            f"install; {_REMEDY}")
    if not netstandard.is_file():
        raise ReferenceProfileUnavailable(
            f"{str(netstandard)!r} is missing from the configured Unity Editor; {_REMEDY}")
    engine = sorted(managed.glob("UnityEngine*.dll"))
    if not engine:
        raise ReferenceProfileUnavailable(
            f"no UnityEngine*.dll under {str(managed)!r}; {_REMEDY}")
    packages = _package_paths(settings.get("package_assemblies"))
    by_name: dict[str, Path] = {}
    for path in [*engine, netstandard, *packages]:
        resolved = path.resolve()
        # One assembly identity per file name: two copies of UnityEngine.UI.dll
        # would be CS0433 (type exists in both) for every uGUI reference.
        previous = by_name.get(resolved.name.lower())
        if previous is not None and previous != resolved:
            raise ReferenceProfileUnavailable(
                f"duplicate reference assembly {resolved.name!r}: "
                f"{str(previous)!r} and {str(resolved)!r}")
        by_name[resolved.name.lower()] = resolved
    paths = tuple(sorted(by_name.values(), key=lambda p: p.name.lower()))
    return ReferenceProfile(editor=str(editor), editor_version=editor.name,
                            paths=paths,
                            package_assemblies=tuple(str(p) for p in packages))
