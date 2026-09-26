# Identity archetypes — semantic glue for shallow nouns

## Why this is a separate layer

The existing gameplay and spatial archetypes answer different questions:

| Layer | Question | Example |
| --- | --- | --- |
| Mission archetype | What does the player do? | `race`, `heist` |
| Spatial archetype | How is the world laid out? | `ArenaPlane`, `BoundedVolume` |
| Identity archetype | What kind of thing is this? | `medieval_human_knight`, `vehicle_boat` |

An identity archetype is a semantic family, not a filename or a single noun.
`knight`, `paladin`, and `templar` can share one identity; a shallow entity
called `Unit` can inherit an identity from its kind and scene context.

The canonical vocabulary lives in
`lookups/unity/identity_archetypes.json`. The deterministic resolver is
`src/unity_identity_archetypes.py`. It is post-schema compiler logic: it makes
no LLM calls and adds no field to `EntitySpec`.

## First slice

The initial catalog deliberately covers the common domains requested by the
compiler:

`medieval_human_commoner`, `medieval_human_knight`,
`medieval_human_worker`, `npc_human`, `fantasy_human`, `sci-fi_human`,
`soldier_infantry`, `civilian_modern`, `vehicle_boat`, `vehicle_car`,
`creature_generic`, and `object_generic`.

The extra `object_generic` family is a safe terminal identity for props and
pickups; it is not used to turn arbitrary scenery into a character or vehicle.

## Resolver contract

`resolve_identity()` applies these deterministic tiers:

1. A distinctive noun in `model_hint` or the entity name, with longer phrases
   and model hints winning ties.
2. A context fallback for a shallow human, vehicle, or creature noun. Examples:
   `Worker` in a medieval forge becomes `medieval_human_worker`; `Unit` of
   kind `vehicle` in open water becomes `vehicle_boat`.
3. An explicit family fallback from `kind`: generic human → `npc_human`,
   vehicle → `vehicle_car`, creature → `creature_generic`.

Every result carries `rule`, `matched_terms`, and `used_fallback`. An entity's
marketplace `sketchfab_query` is intentionally excluded: search context is not
identity evidence.

## Integration boundary

Unity asset resolution computes identity before catalog tiers. Identity query
tokens from explicit nouns and contextual fallbacks enrich deterministic
keyword scoring. Generic kind fallbacks remain metadata-only so `npc_human`
cannot turn a neutral `crowd` into a themed catalog character. All results are
recorded in `schema.meta["identity_archetypes"]` for auditability. The
authority order is unchanged:

`Scene Compiler authored model → locked visual binding → UI/native content →
default/catalog resolution → procedural/fallback policy`.

Identity can improve a derived catalog choice, but it cannot override an
authored transform/model or invent a new schema kind.

## Deepening plan

1. **Substrate (landed here):** freeze the 10–20 family vocabulary, noun map,
   fallback precedence, provenance, and deterministic tests.
2. **Catalog identity (next):** derive and freeze an identity family for each
   catalog asset at catalog-build time. Add compatibility gates so a medieval
   human query cannot be satisfied by a modern vehicle or a creature merely
   because marketplace tags overlap.
3. **Backend adapters:** expose the same identity result to GTA ped/vehicle
   resolution without changing either backend schema. Keep backend model names
   as adapters, not identity definitions.
4. **Acceptance matrix:** test exact nouns, shallow nouns with context, generic
   kind fallbacks, negative cross-family pairs, missing lookup data, and
   byte-identical repeated resolution. Require an explicit warning or abstain
   for an unknown family instead of a silent prop guess.
5. **Promotion rule:** when an identity family needs backend behavior rather
   than asset semantics, promote that behavior into a real schema field and
   update prompts, fixers, and generators together. Do not grow the identity
   catalog into a second schema.
