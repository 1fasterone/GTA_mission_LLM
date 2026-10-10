# ProjectileController Remaining Slices

## Current status

- PR #251 delivered the shared `ProjectileController` foundation.  It accepts
  runtime damage/owner state, filters the owner, handles either Unity 2D or 3D
  impact callbacks, sends the existing `TakeDamage(float)` message, and expires
  the projectile.  It deliberately does **not** launch a projectile or replace
  hitscan.
- PR #254 made the compiler survive malformed prompt-authored custom C# by
  rejecting a body that begins with an orphaned `else`.  Its CI is green and it
  is merged.
- `ProjectileTestArena` now reaches a generated project and a passing C# syntax
  gate.  That is compiler-survival evidence, **not** end-to-end projectile
  validation.  The generated `Shoot` path is still hitscan; the test prompt's
  custom projectile scripts are not a deterministic projectile contract.

## Existing seams to preserve

| Concern | Current deterministic seam | Constraint |
| --- | --- | --- |
| Fire, reload, weapon switching, reserve ammo | `Shoot`, `Reload`, and `CycleWeapon` controller atoms; `ControllerSchema.weapons`; `Controller.cs.j2` | Keep hitscan as the default and do not change existing generated games. |
| Damage | `TakeDamage(float)` message used by both the controller and the projectile foundation | Do not add an interface solely for the first physical-projectile slice. |
| Ammo pickup | Inventory `restore_ammo` bridge to `Controller.AddAmmo(int)` | Preserve the existing composed bridge. |
| HUD | Controller ammo properties and the HUD/GameContext wiring | Bind emitted events; do not create a parallel prompt-authored ammo system. |
| Projectile foundation | `ProjectileController.cs.j2` emitted for a shoot-capable controller | Retain owner filtering, 2D/3D callback support, lifetime, and the one-impact guard. |

## Branching and delivery rule

Each row below is one independently reviewable PR.  Merge a green slice to
`main`, refresh `origin/main`, then create the next slice branch from that
commit.  "Projectile logic" is the roadmap, not a long-lived integration
branch: small merged slices avoid mixing contract, gameplay, and acceptance
changes in one unreviewable PR.

Do not delete source assets such as `character-skate-boy.fbx` to fix a bad
selection.  A separate asset-identity change must reject an unrelated model for
an enemy request while leaving the catalog available for a game that actually
asks for it.

## Slice P1 — typed delivery and impact vocabulary

Introduce two deterministic Unity atom definitions:

1. `LaunchProjectile`: starts a physical 2D or 3D projectile with an explicit
   body type, speed, gravity choice, lifetime, owner, and damage.
2. `ResolveProjectileImpact`: receives the hit/surface result and selects one
   of the closed outcomes `despawn`, `ricochet`, or `penetrate` before it emits
   damage, VFX, and audio hooks.

`Shoot` remains the player action.  It selects hitscan by default; a typed
delivery selection is the only route that may invoke `LaunchProjectile`.
Define the atom parameters, registry validation, Unity binding, and tests in
one PR.  Do not add broad free-form schema text or let custom C# choose the
delivery mode.

Acceptance:

- The schema rejects unknown delivery modes and impact outcomes.
- An FPS still produces the current hitscan controller when no physical mode is
  declared.
- A physical declaration deterministically emits the new atom sequence.

## Slice P2 — 3D physical launch and pooling

Promote the minimum typed projectile delivery contract alongside its templates:

- 3D prefab requirements: `Rigidbody`, `Collider`, `ProjectileController`, and
  a deterministic pool owner.
- Fire path: borrow, configure, place at the muzzle, assign forward velocity,
  and return to the pool after lifetime or terminal impact.
- Pool path: reset ownership, impact state, velocity, gravity, and active VFX
  state before reuse; grow only through a named deterministic policy.

Hitscan remains a separate controller branch.  No physical-launch code may be
silently invoked by a generic `Shoot` request.

Acceptance:

- One 3D launch spends exactly one magazine round and configures the shared
  foundation with the firing owner.
- Owner collision never damages the shooter.
- Lifetime and a terminal hit return the instance to the pool exactly once.
- The existing hitscan test/golden output remains unchanged.

## Slice P3 — deterministic impact and surface resolution

Create a typed `SurfaceImpactResolver` composition contract with the closed
surface set `metal`, `wood`, `concrete`, and `flesh`.  Each entry owns impact
VFX/SFX keys, penetration resistance, ricochet probability, and bounded angle
variance.  The resolver, not prompt-authored C#, decides whether
`ResolveProjectileImpact` ricochets, penetrates, or terminates.

Acceptance:

- Fixed seed/input produces the same ricochet direction and outcome.
- Penetration consumes the configured depth budget and applies distance damage
  falloff once per victim.
- `TakeDamage(float)` is delivered once; a ricochet never double-damages the
  same collision.

## Slice P4 — 2D physical adapter

Reuse the P1/P3 lifecycle and resolver semantics with `Rigidbody2D`,
`Collider2D`, and 2D velocity/normal math.  This is an adapter, not a second
projectile design and not a conversion of the 3D launcher.

Acceptance:

- 2D and 3D variants share owner filtering, lifetime, pool reset, damage, and
  closed impact outcomes.
- The 2D prefab contains only 2D physics components; the 3D prefab contains
  only 3D physics components.

## Slice P5 — weapon, ammo, HUD, audio, and VFX integration

Wire already-owned seams instead of generating competing custom systems:

- weapon switch changes the typed physical projectile/pool binding;
- reload, reserve ammo, ammo pickups, and out-of-ammo state use the controller
  and inventory bridge;
- HUD receives current-ammo, reload, out-of-ammo, hitmarker, and damage-number
  events from the deterministic contract;
- shot, impact, ricochet, penetration, pickup, hurt, and death route through
  the audio/VFX compositions.

Acceptance:

- Switching weapons changes the emitted physical-projectile binding without
  leaving null prefab/pool references.
- A pickup restores reserve ammo and updates HUD state through the existing
  bridge.
- Each terminal projectile outcome emits no more than one event per channel.

## Slice P6 — projectile acceptance and held-out arena proof

Add explicit projectile acceptance checks so a generic `PLAYABILITY` score
cannot stand in for projectile validation.  Tests may use labeled static schema
fixtures, but no test fixture is passed off as Pipeline LLM output.

Required evidence:

- deterministic 2D and 3D generation tests covering launch, gravity toggle,
  expiry, pooling, owner filtering, damage, ricochet, and penetration;
- composition/wiring tests covering ammo, reload, weapon switch, HUD, SFX, and
  VFX contracts;
- replay the held-out `ProjectileTestArena` prompt through Steps 2–7 without
  hand-authored schema content;
- Unity Editor compile and Play Mode evidence for the generated project before
  claiming that the arena validates projectile interactions end to end.

## Separate companion fixes

These are visible in the held-out run but are deliberately outside the
projectile slices:

- Generic headings such as `GAME TYPE` must not become the restored project
  title when the prompt names a concrete scene/title such as
  `ProjectileTestArena`.
- Asset identity must refuse an unrelated `character-skate-boy` model for an
  `enemy soldier` request.  Repair the resolver/catalog decision and its test;
  retain the source asset.
