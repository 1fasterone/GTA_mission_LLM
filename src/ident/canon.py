"""
Canonical IR + content-addressed hashing — the shared identity substrate.

Promoted from src/vstack/canon.py (the V1 I-CAS substrate of the vstack
reference kernel) so every backend — Unity, Blender, GTA5 — derives identity
through one construction instead of four digests with four canonicalizations
(docs/SCENE_GRAPH_COMPILER.md, Phase 0).

Every artifact is identified by a content hash computed over a canonical,
FLOAT-FREE serialization of its quantized fields plus the hashes of its
dependency closure (a Merkle composition). No wall-clock, no RNG, no paths, no
host identifiers ever enter a hash — so hashes are byte-reproducible across
runs, processes, and machines. That reproducibility is the whole point: it is
what makes replay-equivalence and skip-work sound.

Determinism guarantees enforced here:
  * floats are REJECTED inside hashed content (quantize with `q()` first);
  * dict keys are sorted; sets are sorted; list order is preserved as authored;
  * serialization is compact, sorted-key JSON (UTF-8) — stable across runs;
  * every digest is domain-separated by a REGISTERED tag (src/ident/tags.py);
  * file bytes are hashed exactly as found — never normalized first.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

from src.ident.tags import (
    ALL_TAGS, ASSET_BIN, UnregisteredTagError, require_file_tag, require_payload_tag,
)

# ── Fixed quantization steps (real units → integer counts) ────────────────────
Q_POS = 1000        # positions: 1e-3 units  (i.e. integer "milliunits")
Q_ROT = 1_000_000   # quaternion components: 1e-6
Q_TIME = 1000       # seconds: 1e-3

DIGEST_SIZE = 16    # 16 bytes → 32 hex chars (deterministic reference hash)

_FILE_CHUNK = 1 << 20   # fhash() streams files in 1 MiB reads


def q(value, step: int = Q_POS) -> int:
    """Quantize a real value to an integer count of `step`, round-half-to-even.

    Uses Decimal(str(value)) so the result is independent of binary-float repr —
    fully deterministic and float-free (the returned int carries no fp error).

        q(0.05)        -> 50         q(5.0)   -> 5000
        q(9.81)        -> 9810       q(1.5)   -> 1500
    """
    d = (Decimal(str(value)) * step).to_integral_value(rounding=ROUND_HALF_EVEN)
    return int(d)


def _canonical(obj):
    """Normalise to a JSON-able, deterministic, FLOAT-FREE structure."""
    if isinstance(obj, bool):
        return obj                       # bool before int (bool is an int subclass)
    if isinstance(obj, float):
        raise TypeError(
            "raw float in hashed content — quantize with canon.q() first; "
            "floats are nondeterministic across platforms"
        )
    if obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, HashRef):
        return str(obj)                  # a dependency's identity (Merkle edge)
    if isinstance(obj, dict):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_canonical(v) for v in obj)
    raise TypeError(f"unhashable-in-canon type: {type(obj)!r}")


def canon_bytes(obj) -> bytes:
    """Deterministic canonical byte encoding of `obj`."""
    return json.dumps(
        _canonical(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(tag: str, size: int) -> "hashlib.blake2b":
    h = hashlib.blake2b(digest_size=size)
    h.update(tag.encode("utf-8"))
    h.update(b"\x00")
    return h


def chash(tag: str, payload, size: int = DIGEST_SIZE) -> str:
    """Domain-separated content hash:  H(tag, canonical(payload)).

    `tag` is a domain separator (e.g. "node") so structurally-identical
    payloads in different roles never collide. It must be a registered PAYLOAD
    tag (src/ident/tags.py) — an unregistered one raises rather than silently
    opening a new domain.
    """
    h = _digest(require_payload_tag(tag), size)
    h.update(canon_bytes(payload))
    return h.hexdigest()


def fhash(path: str | os.PathLike[str], tag: str = ASSET_BIN,
          size: int = DIGEST_SIZE) -> str:
    """Domain-separated hash of a file's EXACT bytes:  H(tag, bytes).

    The bytes are hashed as found — never decoded, re-encoded, line-ending
    normalized, or otherwise "cleaned" first. Rewriting FBX bytes to make them
    look stable was the root cause of every past Unity ImportFBX failure; an
    identity that required a rewrite would reintroduce it. Two copies of one
    file at different paths share an fhash; one flipped byte does not.
    """
    h = _digest(require_file_tag(tag), size)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_FILE_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


_HEX_RE = re.compile(r"(?:[0-9a-f]{2})+")


@dataclass(frozen=True, order=True)
class HashRef:
    """A typed identity: which domain a digest belongs to, plus the digest.

    Carrying the tag alongside the hex is what lets a payload hold another
    artifact's identity as a Merkle edge (`chash(NODE, {"parent": ref})`)
    without the two domains ever being confused. `str(ref)` is the canonical
    "tag:hex" form; `ref.short` is an 8-char display form for logs only —
    never compare or key on it.
    """
    tag: str
    hex: str

    def __post_init__(self) -> None:
        if self.tag not in ALL_TAGS:
            raise UnregisteredTagError(
                f"[ident.strict] HashRef with unregistered tag {self.tag!r} — "
                "register it in src/ident/tags.py")
        if not _HEX_RE.fullmatch(self.hex):
            raise ValueError(
                f"[ident.strict] HashRef.hex must be lowercase hex bytes, got {self.hex!r}")

    @classmethod
    def of(cls, tag: str, payload, size: int = DIGEST_SIZE) -> "HashRef":
        return cls(tag, chash(tag, payload, size))

    @classmethod
    def of_file(cls, path: str | os.PathLike[str], tag: str = ASSET_BIN,
                size: int = DIGEST_SIZE) -> "HashRef":
        return cls(tag, fhash(path, tag, size))

    @property
    def short(self) -> str:
        return self.hex[:8]

    def __str__(self) -> str:
        return f"{self.tag}:{self.hex}"
