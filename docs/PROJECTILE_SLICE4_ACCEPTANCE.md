# Projectile Slice 4 Acceptance

Slice 4 adds a deterministic firearm presentation adapter on top of the Slice
3 projectile prefab and visual contract. `ProjectileController` remains the
only authority for movement, swept impact, damage, owner filtering, and pool
return.

## Compiler contract

- `fast_round` carries compiler-owned `firearm_presentation` metadata.
- The one supported release event is `OnFirearmRelease`.
- Animator trigger resolution uses the fixed priority `Fire`, `Attack`, then
  `Shoot` and accepts trigger parameters only.
- An Animator is compatible only when it is on the controller GameObject, has
  one supported trigger, and one of its clips declares the exact release event.
- `thrown_object` does not receive the firearm adapter.

## Runtime contract

An accepted shot first borrows a projectile. Only then does it update fire-rate
state, decrement ammunition once, and queue one release. A compatible Animator
sets its resolved trigger and releases from the named event. Missing or invalid
animation data uses the generated presentation immediately; a bounded timeout
also prevents a compatible-but-stalled Animator from consuming ammunition
without launching.

The generated fallback provides:

- deterministic muzzle socket search (`Muzzle`, `ProjectileMuzzle`,
  `MuzzleFlash`, `BarrelEnd`) plus `GeneratedProjectileMuzzle`;
- a visible generated firearm primitive when no view model is present;
- visual-only recoil on the view model;
- a generated orange muzzle-flash particle system.

## Acceptance command

```powershell
pwsh -File tools/run_projectile_slice4_acceptance.ps1
```

The runner creates a clean Unity 6000.0.66f2 URP project under
`I:\UnityAcceptance`, emits the controller from canonical Python/templates,
checks compilation, then runs two Play Mode tests:

1. one sustained fire input produces at least five visible launches, matching
   ammunition decrements, and at least five target impacts through the
   generated fallback;
2. a compatible Animator releases exactly once from `OnFirearmRelease`, while
   a duplicate event is a no-op.

Required proof markers are `SLICE4_AUTOMATIC_PASS` and
`SLICE4_ANIM_EVENT_PASS`.
