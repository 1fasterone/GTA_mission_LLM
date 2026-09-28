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

## Current substrate

The registry currently covers 27 identity families across human, creature,
vehicle, and object domains. The extra `object_generic` family is a safe
terminal identity for props and pickups; it is not used to turn arbitrary
scenery into a character or vehicle.

Level 2 now adds lookup-authored attribute bundles for medieval peasants,
medieval workers, knights, and modern factory workers. The bundles are
resolved into `schema.meta["identity_archetypes"]` with per-field confidence;
they do not add fields to `EntitySpec` or trigger prefab/AI behavior yet.

## Resolver contract

`resolve_identity()` applies these deterministic tiers:

1. A distinctive noun in `model_hint` or the entity name, with longer phrases
   and model hints winning ties.
2. A context fallback for a shallow human, vehicle, or creature noun. Examples:
   `Worker` in a medieval forge becomes `medieval_human_worker`; `Unit` of
   kind `vehicle` in open water becomes `vehicle_boat`.
3. An explicit family fallback from `kind`: generic human → `npc_human`,
   vehicle → `vehicle_car`, creature → `creature_generic`.

Every result carries `rule`, `matched_terms`, and `used_fallback`, plus the
resolved attribute bundle, selected context variant, confidence tier, visual
attribute evidence, and conflicts. An entity's marketplace `sketchfab_query`
is intentionally excluded: search context is not identity evidence.

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

The full staged ontology roadmap is in
[`docs/ENTITY_ONTOLOGY_PLAN.md`](ENTITY_ONTOLOGY_PLAN.md). In short, the next
slice is attribute bundles and an auditable sidecar, followed by roles and
affordances, contextual variants, image-to-noun grounding, declarative
assembly profiles, confidence gates, and finally a bounded ontology registry.

The existing identity layer remains the substrate: authored Scene Compiler
bindings and locked visual bindings outrank derived identity, generic kind
fallbacks remain metadata-only for catalog search, and no new `EntitySpec`
field is added until a repeated consumer proves that promotion is necessary.
