"""
Blender executable discovery — same pattern as unity_cli._find_editor /
_discover_editors: probe PATH + the standard install locations on each OS.

Kept dependency-free so it can run inside CI / tests where Blender is absent
(blender_available() simply returns False and the manager stubs the job).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Standard Blender install roots. On Windows each version lives in its own
# "Blender Foundation/Blender X.Y/blender.exe"; we glob for the newest.
_WIN_ROOTS = [
    Path(r"C:\Program Files\Blender Foundation"),
    Path(r"C:\Program Files (x86)\Blender Foundation"),
]
_MAC_CANDIDATES = [
    Path("/Applications/Blender.app/Contents/MacOS/Blender"),
]
_LINUX_CANDIDATES = [
    Path("/usr/bin/blender"),
    Path("/usr/local/bin/blender"),
    Path("/snap/bin/blender"),
    Path(Path.home() / ".local/bin/blender"),
]


@dataclass
class BlenderInstall:
    path:    Path
    version: Optional[str] = None


def _exe_name() -> str:
    return "blender.exe" if sys.platform == "win32" else "blender"


def _windows_installs() -> list[Path]:
    out: list[Path] = []
    for root in _WIN_ROOTS:
        if not root.is_dir():
            continue
        # "Blender 4.2" → blender.exe ; newest version sorts last.
        for sub in sorted(root.iterdir()):
            exe = sub / "blender.exe"
            if exe.exists():
                out.append(exe)
    return out


def find_blender(explicit: Optional[str] = None) -> Optional[BlenderInstall]:
    """Return the first usable Blender install, or None.

    Resolution order:
      1. explicit path argument (from config / CLI)
      2. $BLENDER_PATH env var
      3. PATH (`shutil.which`)
      4. OS-standard install locations (newest on Windows)
    """
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("BLENDER_PATH")
    if env:
        candidates.append(Path(env))

    which = shutil.which("blender") or shutil.which(_exe_name())
    if which:
        candidates.append(Path(which))

    if sys.platform == "win32":
        candidates.extend(reversed(_windows_installs()))  # newest first
    elif sys.platform == "darwin":
        candidates.extend(_MAC_CANDIDATES)
    else:
        candidates.extend(_LINUX_CANDIDATES)

    for c in candidates:
        if c and c.exists() and c.is_file():
            return BlenderInstall(path=c, version=_probe_version(c))
    return None


def blender_available(explicit: Optional[str] = None) -> bool:
    return find_blender(explicit) is not None


_VER_RE = re.compile(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)


def _probe_version(exe: Path) -> Optional[str]:
    """Run `blender --version` (fast, no GUI) to read the version string."""
    try:
        proc = subprocess.run(
            [str(exe), "--version"],
            capture_output=True, text=True, timeout=20,
        )
        m = _VER_RE.search(proc.stdout or "")
        return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        return None
