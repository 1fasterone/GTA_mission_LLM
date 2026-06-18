"""
Canonical IR + content-addressed hashing — the V1 substrate (I-CAS).

Every artifact in the V1–V5 reference slice is identified by a content hash
computed over a canonical, FLOAT-FREE serialization of its quantized fields plus
the hashes of its dependency closure (a Merkle composition). No wall-clock, no
RNG, no paths, no host identifiers ever enter a hash — so hashes are byte-
reproducible across runs, processes, and machines. That reproducibility is the
whole point: it is what makes replay-equivalence (V11) and skip-work (V1) sound.

Determinism guarantees enforced here:
  * floats are REJECTED inside hashed content (quantize with `q()` first);
  * dict keys are sorted; sets are sorted; list order is preserved as authored;
  * serialization is compact, sorted-key JSON (UTF-8) — stable across runs.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_EVEN

# ── Fixed quantization steps (real units → integer counts) ────────────────────
Q_POS = 1000        # positions: 1e-3 units  (i.e. integer "milliunits")
Q_ROT = 1_000_000   # quaternion components: 1e-6
Q_TIME = 1000       # seconds: 1e-3

DIGEST_SIZE = 16    # 16 bytes → 32 hex chars (deterministic reference hash)


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


def chash(tag: str, payload, size: int = DIGEST_SIZE) -> str:
    """Domain-separated content hash:  H(tag, canonical(payload)).

    `tag` is a domain separator (e.g. "v3.entity") so structurally-identical
    payloads in different roles never collide.
    """
    h = hashlib.blake2b(digest_size=size)
    h.update(tag.encode("utf-8"))
    h.update(b"\x00")
    h.update(canon_bytes(payload))
    return h.hexdigest()
