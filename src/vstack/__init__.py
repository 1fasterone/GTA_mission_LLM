"""
vstack — a minimal, *real* reference implementation of the deterministic
V1 → V5 game-assembly slice described in docs/specs/V1..V5 + E2E_0001.

This is NOT the full V1–V13 compiler (that remains a specification). It is the
smallest runnable kernel that genuinely exercises the identity chain

    rig_hash → deform_hash → entity_hash → scene_hash → mission_hash
             → behavior_hash → physics_hash → worldstate_hash@N

with real content-addressed hashing (blake2b over canonical, float-free,
quantized IR) and a deterministic tick-barrier simulation — so the E2E_0001
"SpawnWalkComplete" test can actually run and produce reproducible results.
"""
