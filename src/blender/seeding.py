"""
Deterministic seed derivation for the Blender pipeline.

One master seed fans out into independent, reproducible per-job / per-stage
sub-seeds via an md5 hash — the same pattern unity_generator._make_uuid uses for
deterministic GUIDs. Independent stages (recipe geometry vs diffusion texture)
get uncorrelated seeds so changing one doesn't perturb the other, yet the whole
run is byte-stable for a given master seed.

    master 1234
      └─ effective_seed(job)          = derive_seed(1234, job.name)
           ├─ derive_seed(eff, "recipe")     → procedural geometry RNG
           └─ derive_seed(eff, "diffusion")  → diffusion sampler seed

`seed=None` anywhere means "not pinned" — callers fall back to DEFAULT_MASTER so
even un-seeded jobs are reproducible from their name (determinism is the default;
pass a random master to vary).
"""

from __future__ import annotations

import hashlib

DEFAULT_MASTER = 0


def derive_seed(master: int, *parts) -> int:
    """Stable 32-bit sub-seed from a master seed + string path components."""
    key = f"{int(master)}:" + "/".join(str(p) for p in parts)
    h = hashlib.md5(key.encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def effective_seed(job) -> int:
    """The integer seed a job actually uses: its explicit `seed`, else one
    derived deterministically from its name under DEFAULT_MASTER."""
    s = getattr(job, "seed", None)
    if s is not None:
        return int(s)
    return derive_seed(DEFAULT_MASTER, getattr(job, "name", "asset"))


def recipe_seed(job) -> int:
    return derive_seed(effective_seed(job), "recipe")


def diffusion_seed(job) -> int:
    return derive_seed(effective_seed(job), "diffusion")
