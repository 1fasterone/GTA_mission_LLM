"""
Export determinism — strip the wall-clock timestamps Blender's exporters embed,
so re-exporting identical geometry yields a byte-identical file (no spurious git
diff / Unity re-import when nothing actually changed).

Measured against real Blender 5.1 output, the *only* non-deterministic fields are
timestamps; everything else (creator/version strings, library build dates) is
stable per toolchain:

  * Alembic (.abc, Ogawa) — `_ai_DateWritten=Mon Jun 15 15:43:16 2026`
        a C `asctime()` string, ALWAYS 24 chars.
  * FBX (.fbx, binary)    — `CreationTime` `YYYY-MM-DD HH:MM:SS:mmm`, 23 chars.
        (Blender already writes epoch here, so FBX is usually deterministic
        already; we canonicalize it defensively.)

All edits are **length-preserving** in-place byte replacements — critical for
Ogawa/FBX, whose internal offset tables would break if the file length changed.
"""

from __future__ import annotations

import re
from pathlib import Path

# C asctime() — "Www Mmm dd hh:mm:ss yyyy", exactly 24 bytes. Day is space-padded.
_ASCTIME_RE = re.compile(
    rb"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    rb"[ 0-3]\d \d\d:\d\d:\d\d \d{4}"
)
# Jan 1 1970 was a Thursday; asctime space-pads the single-digit day. 24 bytes.
CANONICAL_ASCTIME = b"Thu Jan  1 00:00:00 1970"

# FBX human-readable creation time — "YYYY-MM-DD HH:MM:SS:mmm", exactly 23 bytes.
_FBX_TS_RE = re.compile(rb"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3}")
# Blender's exporter writes this CONSTANT dummy (io_scene_fbx/export_fbx_bin.py:
# "CreationTime is replaced by dummy value") with matching constant _FILE_ID /
# _FOOT_ID blobs — so Blender FBX output is already byte-deterministic.
BLENDER_DUMMY_FBX_TS = b"1970-01-01 10:00:00:000"
# Kept for backward compat with older callers/tests; no longer written anywhere.
CANONICAL_FBX_TS = BLENDER_DUMMY_FBX_TS

assert len(CANONICAL_ASCTIME) == 24
assert len(CANONICAL_FBX_TS) == 23


def _sub_same_length(data: bytes, pattern: re.Pattern, repl: bytes) -> tuple[bytes, int]:
    """Replace every match with `repl`, but ONLY when the match is exactly the
    same byte length (so file offsets never shift). Returns (new_data, count)."""
    count = 0

    def _f(m: "re.Match[bytes]") -> bytes:
        nonlocal count
        g = m.group(0)
        if g == repl:
            return g                      # already canonical → not a change (idempotent)
        if len(g) == len(repl):
            count += 1
            return repl
        return g                          # unexpected length → leave untouched (safety)

    return pattern.sub(_f, data), count


def normalize_alembic_bytes(data: bytes) -> tuple[bytes, int]:
    return _sub_same_length(data, _ASCTIME_RE, CANONICAL_ASCTIME)


def normalize_fbx_bytes(data: bytes) -> tuple[bytes, int]:
    """FBX: NO-OP by design — verified 2026-07-09 against Unity 6000.1.

    Binary FBX cross-validates CreationTime against the FileId and the 16-byte
    footer code; Autodesk's FBX SDK (Unity's importer) REJECTS the whole file
    ("ImportFBX Errors: Couldn't import file") when they disagree. Blender's
    exporter writes a CONSTANT dummy CreationTime (1970-01-01 10:00:00:000)
    with matching constant FileId/footer blobs, so its FBX output is already
    byte-deterministic — and any timestamp rewrite here can only corrupt it.
    (Blender itself ignores the footer, which is why round-trips masked this.)
    """
    return data, 0


def normalize_alembic(path: str | Path) -> int:
    """Canonicalize the Alembic write-date in place. Returns fields rewritten."""
    p = Path(path)
    new, n = normalize_alembic_bytes(p.read_bytes())
    if n:
        p.write_bytes(new)
    return n


def normalize_fbx(path: str | Path) -> int:
    """Canonicalize the FBX creation-time in place. Returns fields rewritten."""
    p = Path(path)
    new, n = normalize_fbx_bytes(p.read_bytes())
    if n:
        p.write_bytes(new)
    return n


def normalize_export(path: str | Path) -> tuple[str | None, int]:
    """Dispatch by extension. Returns (kind, fields_rewritten)."""
    s = str(path).lower()
    if not Path(path).is_file():
        return None, 0
    if s.endswith(".abc"):
        return "alembic", normalize_alembic(path)
    if s.endswith(".fbx"):
        return "fbx", normalize_fbx(path)
    return None, 0
