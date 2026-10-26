# Projectile Slice 3: Prefab and Visual Specification

## 1. Scope and authority

1. Slice 3 is additive to the existing `projectile_3d` lowering path.
2. `ProjectileController` remains the sole authority for swept collision,
   impact, damage, owner filtering, lifetime expiry, and return-to-pool.
3. `ProjectilePool` remains the sole authority for allocation, checkout,
   return, parking, and pool-growth policy.
4. `Controller` remains the sole authority for input, muzzle selection, launch
   direction, ammunition, and pool selection.
5. The Pipeline LLM does not select projectile presentation. The compiler
   derives presentation from the closed `fast_round`, `gravity_round`, and
   `thrown_object` profiles and stores it under the existing
   `schema.meta["physical_projectile_contract"]` record.
6. No generated Unity file is a source of truth. Every runtime change in this
   slice is emitted from compiler source or a Jinja template.

## 2. Closed presentation records

1. Each closed motion profile maps to exactly one compiler-owned presentation
   record:

   | Motion profile | Resolver name | Resolver hint | Primitive fallback | Uniform visual scale |
   |---|---|---|---|---|
   | `fast_round` | `ProjectileVisualFastRound` | `bullet projectile round` | `ProjectileFallback_FastRound_Capsule` | `0.08` |
   | `gravity_round` | `ProjectileVisualGravityRound` | `arrow bolt projectile` | `ProjectileFallback_GravityRound_Capsule` | `0.12` |
   | `thrown_object` | `ProjectileVisualThrownObject` | `rough throwing rock stone` | `ProjectileFallback_ThrownObject_Cube` | `0.22` |

2. The presentation record contains only compiler metadata: resolver identity,
   model hint, resolved Resources path, fallback name and primitive, uniform
   scale, fallback colour, visual orientation, and root physics radius.
3. The compiler rejects an unknown motion or visual profile. It never maps an
   unknown value to the nearest known profile.
4. The resolver identity is not appended to `schema.entities`. It is a
   compiler-only request, so it cannot become an extra world spawn or create a
   false `scene_specs[].entities` coverage obligation.

## 3. Visual resolution path

1. Step 5 selects the closed projectile profile from the untouched brief and
   stamps the corresponding presentation record into the existing physical
   projectile metadata.
2. Step 6 constructs one transient `EntitySpec` from that record and passes it
   through `resolve_entity_assets`, using the same default-model, authored,
   catalog, Sketchfab-freeze, deterministic sampling, model-copy, texture-copy,
   `.meta`, and `Resources` path machinery used for normal entities.
3. The transient request disables the resolver's generic prop fallback. A real
   semantic match yields a Resources path; a miss yields an empty path so the
   named projectile fallback remains authoritative.
4. The resolved Resources path is stamped back into the presentation record.
   It is also retained in the generated model-binding sidecar for provenance,
   but the transient resolver identity never enters the scene entity list.
5. After project generation and before micro emission, the controller
   subsystem refreshes its `meta.projectile_presentation` option from the
   resolved contract. This is the same post-resolution refresh seam already
   used for model-aware cover points.
6. A replay with the same schema, frozen catalogs, asset bytes, compiler
   version, and configured seed must render identical presentation metadata
   and generated source.

## 4. Generated prefab structure and physics constraint

1. `Controller.BuildProjectile3DPrefab` creates an empty root named
   `Projectile3DPrefab_<weapon-index>` with local position zero, local rotation
   identity, and local scale `Vector3.one`.
2. The root receives exactly the existing Rigidbody, one primitive
   `SphereCollider`, and `ProjectileController`. The collider radius comes from
   the closed presentation record.
3. A child named `ProjectileVisualRoot` contains either:
   - an instantiated resolved model loaded by Resources path; or
   - the profile's named primitive fallback.
4. Every visual child collider, Rigidbody, CharacterController, or Joint is
   disabled immediately and removed. Visual mesh topology never supplies the
   projectile collision shape.
5. The visual root receives only a uniform `Vector3.one * scale` transform.
   Profile-specific facing uses rotation, never non-uniform body scaling.
6. A missing resolved asset is supported by the named fallback. An unknown
   fallback primitive, invalid non-positive scale/radius, or unknown closed
   profile is a loud compiler/runtime abstention, not a silent sphere
   substitution.

## 5. Pool checkout reset checklist

`ProjectilePool.Borrow` activates the instance and calls
`ProjectileController.PrepareForCheckout` before the controller configures or
launches it. That call must:

1. Remove and deactivate transient child objects that were not present in the
   baseline prefab snapshot.
2. Restore every baseline child parent, sibling index, active state, local
   position, local rotation, and local scale.
3. Restore every Renderer enabled state, its baseline per-instance materials,
   all material properties, render queue, shader keywords, instancing/GI flags,
   and clear every MaterialPropertyBlock.
4. Stop and clear every ParticleSystem, restore its random-seed mode and seed,
   then replay only systems whose baseline `playOnAwake` state requires it.
5. Clear every TrailRenderer, restore its enabled state, and restore its
   baseline emitting state.
6. Reset Rigidbody linear/angular velocity and transient impact/lifetime state
   through the existing configure path without changing collision authority.
7. Leave the projectile root uniformly scaled. Launch code may then set only
   root world position and orientation.

## 6. Pool return reset checklist

`ProjectilePool.Return` calls `ProjectileController.ResetForPool`, reparents the
instance under the pool, resets root local position/rotation/scale, and disables
it. The controller reset must:

1. Restore all owner collision pairs.
2. Clear owner, impact, release, and expiry state.
3. Zero Rigidbody linear and angular velocity, disable gravity, make the body
   kinematic, and retain `ContinuousSpeculative`.
4. Execute the complete visual reset from Section 5, except particles and
   trails remain stopped, cleared, and non-emitting while parked.
5. Remove any transient child visual objects and restore all baseline child
   transforms and active states.
6. Restore every per-instance material from its immutable baseline and clear
   property blocks.
7. Reset root local position to zero, rotation to identity, and scale to one
   after reparenting.

## 7. Exact template insertion points

1. `src/unity_projectile_contract.py`
   - beside `_PROJECTILE_PROFILES`: define the closed presentation table;
   - inside `physical_projectile_contract_for`: attach the selected
     presentation record;
   - beside `apply_physical_projectile_contract`: expose transient resolver and
     resolved-path stamping helpers.
2. `src/unity_generator.py`
   - inside `_resolve_all_models`, immediately after normal entity resolution:
     resolve the transient projectile visual through the same entity resolver,
     stamp the path into contract metadata, and retain the path for copying and
     provenance.
3. `src/unity_compose.py`
   - in `subsystems_for`, copy presentation metadata into controller `meta`;
   - beside `refresh_cover_points`, add the post-model-resolution controller
     metadata refresh.
4. `src/unity_pipeline.py`
   - immediately after `generator.write` and before `composer.emit`: invoke the
     projectile-presentation refresh.
5. `templates/unity/micro/controller/Controller.cs.j2`
   - physical-projectile field block: emit closed presentation constants;
   - `BuildProjectile3DPrefab`: replace `CreatePrimitive(Sphere)` with the empty
     root, primitive collider, and separate visual-root construction;
   - adjacent helper methods: load the resolved model, build the named fallback,
     and strip visual physics components.
6. `templates/unity/micro/controller/ProjectileController.cs.j2`
   - cached runtime fields: add baseline child, renderer/material, particle, and
     trail snapshots;
   - `Awake`: capture the immutable visual baseline;
   - new `PrepareForCheckout`: execute the checkout reset;
   - `ResetForPool`: execute the parked reset;
   - `OnDestroy`: restore owner collisions and dispose per-instance materials.
7. `templates/unity/micro/controller/ProjectilePool.cs.j2`
   - `Borrow`: normalize root scale and call `PrepareForCheckout`;
   - `Return`: reset root local position, rotation, and scale after reparenting;
   - `EnsurePrefabContract`: enforce a primitive root collider and disable
     visual-child physics components.

## 8. Exit-gate evidence

1. Python contract tests prove profile-to-presentation mapping, resolver-only
   identity, named fallback source emission, and unchanged Slice 0/1/2
   invariants.
2. Generated-C# assertions prove model loading, primitive-only root collision,
   visual-physics stripping, and both checkout/return reset call sites.
3. A generated Unity project compiles with zero C# errors.
4. Advancing Play Mode tests run both `fast_round` and `thrown_object` through at
   least three borrows and returns (initial use plus two reuse cycles).
5. Each cycle proves the two profiles retain distinct names, shapes, colours,
   and scales; no child transform, material property, trail segment, particle,
   transient child, velocity, angular velocity, gravity, or orientation leaks.
6. The same Play Mode run re-proves visible motion, owner filtering, first-hit
   swept impact, exactly one damage application, and exactly one pool return.
7. Focused tests do not replace Unity-wide compilation or advancing Play Mode
   evidence.
