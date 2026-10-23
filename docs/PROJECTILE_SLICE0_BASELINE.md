# Projectile Slice 0 Baseline

## Status

Slice 0 is the locked runtime baseline. Slice 1 now layers deterministic
`fast_round` and `thrown_object` profiles on that foundation without replacing
`ProjectileController`, `ProjectilePool`, or their generated collision and
reuse behavior.

The roadmap remains in
`docs/PROJECTILE_PHYSICS_AND_ANIMATION_ROADMAP.md`. Slice 2 is the next
development step after the combined Slice 0 and Slice 1 delivery reaches
`main`.

## Compiler invariants

- Bare shooting remains hitscan.
- Only explicit supported physical-projectile language selects
  `projectile_3d`.
- Explicit hitscan language wins over visual bullet language.
- The 3D bridge does not forge a Rigidbody2D contract.
- Slice 1 profiles use existing typed weapon fields and compiler-owned `meta`;
  they add no post-freeze LLM call or invented top-level schema field.
- `ProjectileController` and `ProjectilePool` remain generated companion
  types owned by the controller micro.
- The physical launch begins outside the owner's collider bounds.
- Pool checkout rebinds owner-collider ignores.
- Pool return restores ignored collision pairs, velocity, angular velocity,
  gravity, kinematic state, impact/release guards, particles, and lifetime.
- Runtime collision retains both `Continuous Speculative` and
  `Physics.SphereCastNonAlloc`; collision callbacks and swept hits converge on
  the same idempotent impact path.
- Damage, release, and pool return occur at most once per checkout.

## Accepted Unity evidence

The baseline uses the live Unity acceptance already merged by PR #264:

- Unity 6000.0.66f2 compiled the generated project with zero C# errors.
- Play Mode frames advanced.
- A pooled projectile moved at 60 units/second, or 1.2 units per fixed step.
- It did not collide with its owner.
- It reduced the target from 100 health to 75 exactly once.
- It returned to the pool after impact.

This is inherited baseline evidence, not a new Unity run. Slice 1 changes only
the deterministic profile inputs and retains the accepted runtime templates.
Any later slice that changes templates or runtime behavior must regenerate a
project and repeat the full live gate rather than citing this record as fresh
proof.

## Automated lock

`tests/test_unity_projectile_contract.py` verifies both layers:

1. Slice 0 renders the generated controller, projectile, and pool sources and
   asserts the runtime invariants above.
2. Slice 1 selects closed bullet and thrown-rock profiles and proves both
   lower through the same `projectile_3d` runtime.

The existing focused generator and compose tests remain part of the gate.

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests/test_unity_projectile_contract.py `
  tests/test_micro_generators.py `
  tests/test_unity_compose.py -k projectile

.venv\Scripts\python.exe -m ruff check `
  src/unity_projectile_contract.py `
  tests/test_unity_projectile_contract.py
```

Focused tests do not replace Unity-wide compilation and advancing Play Mode
acceptance after a runtime change.
