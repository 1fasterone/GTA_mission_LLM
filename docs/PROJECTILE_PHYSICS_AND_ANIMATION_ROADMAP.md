# Projectile Physics and Animation Roadmap

## Purpose

Build proper physical projectile output for fast bullets and thrown objects
without replacing the existing `ProjectileController` and `ProjectilePool`
foundation. The compiler remains deterministic after schema freeze; generated
Unity files are outputs and are never the source of a fix.

## Existing foundation to preserve

- `src/unity_projectile_contract.py` opts explicit 3D physical-projectile
  briefs into the existing `projectile_3d` delivery.
- `src/micro/controller.py` lowers the typed weapon into generated controller,
  projectile, and pool scripts.
- `templates/unity/micro/controller/ProjectileController.cs.j2` owns swept
  collision, impact, damage, owner filtering, and return-to-pool behavior.
- `templates/unity/micro/controller/ProjectilePool.cs.j2` owns allocation,
  checkout, return, and reset boundaries.
- `templates/unity/micro/controller/Controller.cs.j2` owns input, muzzle
  resolution, launch direction, ammunition, and pool selection.
- `src/unity_compose.py` reserves both companion class names so generated and
  Pipeline-LLM-authored scripts cannot create duplicate global definitions.

The runtime contract already uses a dynamic Rigidbody, `Continuous
Speculative`, owner-collider exclusion, and `Physics.SphereCastNonAlloc`.
Those are invariants, not temporary scaffolding.

## Authority boundaries

1. The Pipeline LLM may write the concept and existing schema fields only.
2. `src/unity_projectile_contract.py` may derive a closed compiler-owned
   profile from the original raw brief and record it in schema `meta`.
3. Steps 4-7 remain deterministic and LLM-free.
4. No generated Unity script is hand-edited. Change the compiler source or
   templates, regenerate, and compare the result.
5. Runtime physics may vary by platform, but compiler output, profile
   selection, generated source, and validation results must be reproducible.

## Target profiles

| Profile | Emission | Motion | Presentation |
|---|---|---|---|
| `fast_round` | Muzzle | High speed, normally no gravity, swept first-hit collision | Bullet mesh, tracer, muzzle flash, impact effect |
| `gravity_round` | Muzzle | Gravity, velocity-aligned flight, swept first-hit collision | Arrow, bolt, shell, or rocket presentation |
| `thrown_object` | Hand release | Dynamic Rigidbody, gravity, angular velocity, swept collision | Held rock/object, wind-up, release, tumble, impact |

Profiles are closed compiler vocabulary. They reuse existing schema fields for
speed, gravity, lifetime, damage, range, pool size, and pool growth. Future
presentation data stays in compiler-owned projectile metadata until there is
an approved schema-extension or promotion path.

## Delivery plan

### Slice 0 - Baseline lock

- Preserve hitscan precedence and the loud 2D abstention.
- Preserve pool reset, lifetime return, owner filtering, muzzle clearance,
  `Continuous Speculative`, and `Physics.SphereCastNonAlloc`.
- Record current Unity Play Mode evidence as the regression baseline.

Exit gate: existing focused tests pass and a generated bullet still moves,
hits one target once, ignores its owner, and returns to the pool.

### Slice 1 - Typed motion foundation

- Version the compiler contract.
- Select `fast_round` for explicit bullet/projectile language.
- Select `thrown_object` only when a throw verb and rock/stone object are both
  explicit; a scenic rock must not enable projectile delivery.
- Give each profile deterministic existing-field values and a suitable held
  model hint.
- Carry `motion_profile`, `emission_profile`, and `visual_profile` as
  compiler-owned metadata while continuing to lower through `projectile_3d`.

Exit gate: bullet and thrown-rock briefs compose through the same proven
runtime while producing distinct speed, gravity, cadence, range, and visual
identity inputs.

### Slice 2 - Authoritative swept impact

- Keep Rigidbody movement and collision callbacks.
- Sweep a sphere from the prior physics position through the traveled or
  predicted segment every fixed step.
- Filter owner/self colliders before selecting a hit.
- Choose the nearest valid hit; define a stable tie-break for equal distances.
- Route sweep and callback contacts through one idempotent impact method.
- Apply damage and pool return no more than once.

Exit gate: a fast bullet cannot tunnel through a thin static wall or dynamic
target at the supported maximum speed.

### Slice 3 - Projectile prefab and visuals

- Replace the runtime sphere as the normal presentation path.
- Resolve projectile visuals through the established entity/model chain.
- Retain a named deterministic primitive fallback when no asset resolves.
- Use primitive or convex physics shapes on a uniformly scaled body; visual
  mesh complexity must not define collision behavior.
- Reset trails, particles, orientation, material state, and child transforms
  on every pool checkout and return.

Exit gate: bullets and rocks are visibly distinct and remain correct after at
least two reuse cycles.

### Slice 4 - Firearm animation adapter

- Resolve a deterministic muzzle socket.
- Add recoil and muzzle-flash presentation without moving impact authority out
  of `ProjectileController`.
- Launch on one named animation release event when a compatible Animator is
  available.
- Provide a deterministic generated presentation fallback when it is not.
- Preserve ammunition and fire-rate state across either presentation path.

Exit gate: one input produces one visible launch, one ammunition decrement,
and one impact opportunity.

### Slice 5 - Thrown-object animation adapter

- Borrow the rock before wind-up with physics and collision disabled.
- Attach it to a resolved throwing-hand socket.
- Play wind-up and follow-through presentation.
- Release on exactly one animation event; at release, detach, restore dynamic
  physics, rebind owner ignores, apply launch velocity and angular velocity,
  and begin the lifetime clock.
- Loudly abstain when a required authored socket or event conflicts; use the
  deterministic fallback only when the absence is supported.

Exit gate: the rock is visibly held before release, follows a parabolic arc,
tumbles, hits once, and returns or settles according to its closed profile.

### Slice 6 - Structural playability

- Add deterministic checks for a compatible 3D controller, emitter, valid
  projectile prefab, targetable content, impact path, and pool policy.
- Require a hand-release path for `thrown_object` and a muzzle path for
  `fast_round`/`gravity_round`.
- Report explicit remedies rather than silently substituting hitscan.

Exit gate: invalid projectile compositions fail before Unity generation with
a specific `[projectile.*]` diagnostic.

### Slice 7 - Raw-prompt Unity acceptance

Run untouched natural-language inputs through `unity-create`:

1. A firearm that fires visible physical bullets at a thin target.
2. A character who visibly throws rocks at a moving target.

For each run, retain the raw input, intermediate provenance, generated schema,
compiler messages, generated Unity project, Unity compile result, and advancing
Play Mode evidence. Confirm the Pipeline LLM authored Steps 2-3 and no engineer
authored or preformatted an intermediate artifact.

Exit gate: both projects compile with zero C# errors and satisfy the live
acceptance matrix below.

## Live acceptance matrix

| Scenario | Required evidence |
|---|---|
| Bullet against thin wall | Visible motion and first-contact hit despite travel exceeding wall thickness in one fixed step |
| Bullet against dynamic target | Exactly one health reduction and one impact response |
| Projectile against owner | No self-hit at spawn or after pool reuse |
| Rock throw | Visible wind-up, object held in hand, one release, parabolic flight, and visible tumble |
| Rock impact | One target impact, expected damage, and declared settle/return response |
| Pool reuse | Velocity, angular velocity, gravity, collision ignores, effects, transforms, and impact flags reset |
| Pool exhaustion | Deterministic fixed-pool rejection or configured grow-by-one behavior |
| Provenance | Original brief and Step 2-3 artifacts retained; no engineer-authored schema presented as pipeline output |

## Test layers

1. Python unit tests for phrase classification, hitscan precedence, 2D
   abstention, profile values, compose lowering, and duplicate ownership.
2. Generated-C# assertions and syntax compilation for every profile.
3. Unity Editor compilation across the entire generated project.
4. Advancing Unity Play Mode probes for movement, impact, owner filtering,
   damage-once, animation release, and pool return/reuse.
5. Raw-prompt end-to-end runs with durable `_run_logs` evidence.

Focused tests do not replace Unity-wide compilation or advancing Play Mode
evidence.
