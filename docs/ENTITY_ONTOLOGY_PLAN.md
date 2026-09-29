# Entity ontology plan — Level 1 through Level 8

This plan expands the existing identity-archetype layer into a bounded,
deterministic ontology. It is deliberately staged: each level must produce a
useful artifact and pass an acceptance gate before the next level adds more
meaning.

## Non-negotiable boundaries

- The Pipeline LLM may write the concept and fill the schema in Steps 2–3.
  Identity resolution, validation, variant selection, assembly, asset lookup,
  and backend lowering remain deterministic Steps 4–7.
- Identity semantics are separate from mission archetypes, spatial archetypes,
  gameplay actions, and concrete asset filenames.
- A Scene Compiler binding or locked visual binding outranks derived ontology
  data. Ontology data may explain or constrain a derived catalog choice; it
  may not replace an authored model or transform.
- The first seven levels use lookup data and a resolved sidecar recorded in
  `schema.meta`. `EntitySpec` is not expanded until a repeated consumer proves
  that a real schema field is required. Any promoted field must update the
  schema, prompt, fixer, templates, and tests in one slice.
- No ontology entry invents a new atom. Affordances can only map to atoms,
  micros, prefab roles, or backend capabilities that already exist and have
  been validated.
- Unknown, conflicting, or under-specified meaning is recorded as a warning
  or abstention. It is never silently converted into a different identity.

## Repository review: what already exists

The repository is beyond a literal Level 1 implementation:

| Existing capability | Current location | Status |
| --- | --- | --- |
| Identity families and noun aliases | `lookups/unity/identity_archetypes.json` | Landed; currently 26 families in this checkout |
| Noun → identity resolution | `src/unity_identity_archetypes.py` | Deterministic; model hint/name, context, then kind fallback |
| Catalog-query enrichment | `src/unity_entity_asset_resolver.py` | Explicit identities and contextual fallbacks enrich queries; generic fallbacks remain metadata-only |
| Provenance | `schema.meta["identity_archetypes"]` | Records rule, matched terms, and fallback use |
| Confidence vocabulary | `confidence_tiers` in the lookup JSON | Defined, but not yet returned or enforced by the resolver |
| Attribute/role/variant payloads | — | Missing; the JSON `identity` flags are currently not loaded into `IdentityArchetype` |
| Visual → noun → identity join | visual reasoning + identity resolver | The stages exist separately; an ontology-level, hash-checked join is missing |
| Assembly plan | — | Missing |

Therefore the next implementation slice is Level 2, not another noun-map
expansion. The current resolver and its authority order are the substrate to
protect while the ontology payload grows.

## Level 1 — Basic archetype matching and contract hardening

### Goal

Freeze the meaning of an identity family and make the existing noun/context/
kind behavior auditable.

### Work

1. Keep a versioned identity registry with unique family IDs, family type,
   kind hints, noun aliases, query tokens, and explicit fallback rules.
2. Preserve the precedence already implemented:
   `model_hint`/entity name noun → contextual fallback → explicit kind
   fallback → no identity signal.
3. Validate duplicate aliases, unknown targets, empty families, and malformed
   fallback rules at lookup load time.
4. Keep generic kind fallbacks out of catalog query enrichment. A generic
   `npc_human` result is provenance, not permission to select a themed asset.
5. Add a negative matrix: prop versus human, medieval versus modern, vehicle
   versus creature, and authored model versus derived identity.

### Output and gate

`IdentityResolution` remains deterministic and records the rule, matched
terms, fallback status, and lookup version. Repeated resolution is byte-stable;
missing lookup data returns `no_identity_signal`; all negative pairs either
reject or use the declared neutral path.

## Level 2 — Attribute expansion

### Goal

Turn a family into a semantic identity bundle without changing the schema.

### Data shape

Add controlled, lookup-authored attributes to each identity family:

```json
{
  "attributes": {
    "era": ["medieval"],
    "profession": ["farmer"],
    "social_class": ["commoner", "low"],
    "equipment": ["hand_tool"],
    "visual_traits": ["ragged", "earth_toned"],
    "behavioral_hints": ["timid", "non_hostile"]
  }
}
```

The keys are a closed registry, not arbitrary schema fields. Normalize values
to stable IDs and reject unknown attribute namespaces or malformed values.

### Work

- Load the attribute bundle into `IdentityArchetype`.
- Add the resolved bundle to the identity sidecar with source `archetype`.
- Keep attributes descriptive. Do not overwrite `EntitySpec.behavior`,
  equipment fields, authored models, or tags merely because an identity has a
  hint.
- Add inheritance only after a base family is explicit; do not infer a graph
  from overlapping words.

### Acceptance

`peasant` resolves to the commoner family and its declared bundle; `knight`
does not inherit peasant attributes; a missing or malformed bundle produces a
loud lookup error; an attribute cannot qualify an unrelated asset by itself.

### Current implementation slice

The current Level 2 slice implements this contract in
`src/unity_identity_archetypes.py` and
`lookups/unity/identity_archetypes.json`:

- reusable `peasant`, `medieval_worker`, `knight`, and `modern_worker` bundles;
- a separate `modern_human_worker` family so a factory worker is not forced
  into the medieval worker family;
- fixed category/field validation and canonical attribute IDs;
- lookup-authored visual cue rules for ragged clothing, dirt/wear, and tools;
- `schema.meta["identity_archetypes"]` output containing attributes,
  per-field confidence, evidence, and conflicts.

Prefab and AI wiring is intentionally not part of this slice. Until the
prefab, animation, atom, and backend capability registries exist, attributes
remain descriptive compiler metadata and cannot silently activate behavior.

## Level 3 — Roles and affordances

### Goal

Describe what an entity is suited to do without silently enabling behavior.

### Work

Add two controlled lists to the identity registry:

- `roles`: semantic participation such as `worker`, `guard`, `civilian`, or
  `combatant`.
- `affordances`: capabilities such as `work`, `carry`, `gather`, `flee`,
  `fight`, `patrol`, or `talk`.

Resolve them into the sidecar, then pass them through a deterministic adapter
that can select only existing atoms, micros, animation IDs, or backend
capabilities. An affordance is not an order: a peasant may afford `flee`
without the compiler adding a flee action to the scene.

### Acceptance

The sidecar explains why a role was selected, authored behavior remains the
stronger explicit instruction, unsupported affordances are named and ignored
or abstained, and no new atom type or schema field appears as a side effect.

## Level 4 — Contextual variants

### Goal

Allow one identity family to change its presentation and capabilities in an
explicit context while retaining a stable base identity.

### Data shape

```json
{
  "variants": {
    "battle": {
      "affordances": ["flee"],
      "equipment": ["none"]
    },
    "village": {
      "affordances": ["work"],
      "equipment": ["hand_tool"]
    },
    "cutscene": {
      "affordances": ["talk"],
      "emotional_state": ["fearful"]
    }
  }
}
```

### Work

- Define a closed context vocabulary and deterministic context evidence from
  existing schema text, genre, objectives, and backend mode.
- Apply `base attributes → selected variant → explicit schema/authored data`.
- Keep the base identity immutable. Record `selected_variant`, matched context
  terms, and every overridden key in provenance.
- If contexts tie or no variant is declared, keep the base bundle and issue a
  warning; do not guess a variant.

### Acceptance

The same `peasant` can resolve as a worker in a village and a fleeing civilian
in battle, but the noun still resolves to the same identity family. Repeated
inputs produce identical variant selection and no context can override an
authored model binding.

## Level 5 — Cross-modal grounding: image → noun → entity

### Goal

Use visual evidence to corroborate and locate an entity without letting an
image invent gameplay meaning.

### Work

1. Keep image processing in the existing visual-reasoning stages. The image
   annotator may emit closed-vocabulary visual observations, literal noun
   labels, normalized boxes, and evidence confidence; it does not emit a new
   gameplay schema or an asset binding.
2. Build a deterministic join:
   `image label → schema entity name/model_hint → noun map → identity family`.
3. Store source image hash, label, box, allowed visual tokens, matched entity,
   identity result, and rejected alternatives in a versioned sidecar.
4. Treat clothing, posture, and environment as corroborating evidence. They
   may raise or lower semantic confidence only when a controlled vocabulary
   says how; they cannot turn a neutral `crowd` into a knight on visual style
   alone.
5. Require lineage and hash checks before the join. Schema/archetype meaning
   remains authoritative when the image surface disagrees.

### Acceptance

An image containing a labeled peasant joins to the declared peasant entity and
retains its visual evidence. An armor observation without an explicit knight
noun does not create a knight. Missing, extra, or duplicate labels produce a
loud rejection with the exact join failure.

## Level 6 — Compositional entity assembly

### Goal

Compile a semantic identity into a backend-neutral, declarative entity plan.

### Data shape

Use registry-authored assembly profiles rather than free-form LLM code:

```json
{
  "assembly_profiles": {
    "medieval_peasant_worker": {
      "base": "medieval_human_commoner",
      "modifiers": ["ragged_clothing", "earth_tone_palette"],
      "equipment": ["hoe"],
      "affordances": ["work", "carry"],
      "environment_constraints": ["grounded", "village_context"],
      "animation_set": "work_cycle",
      "prefab_role": "civilian_worker",
      "asset_policy": "identity_compatible_human"
    }
  }
}
```

### Work

- Validate every reference against existing asset, prefab, animation, atom,
  and backend capability registries.
- Merge in a fixed order: base family, variant, explicit modifiers, authored
  locks, then backend policy.
- Emit a `ResolvedEntityPlan` sidecar with canonical ordering and provenance.
- Let Unity and GTA adapters lower the same plan into their existing model,
  prefab, ped, vehicle, atom, and animation mechanisms.
- Reject cycles, missing references, incompatible equipment, and ambiguous
  assembly profiles. Do not synthesize a substitute profile.

### Acceptance

The peasant-worker example resolves to the same canonical plan on every run,
can be lowered by both backends where capabilities exist, and reports any
unsupported GTA/Unity part without discarding the valid identity result.

## Level 7 — Semantic confidence scoring

### Goal

Make identity quality explicit and use it to gate downstream effects rather
than treating every match as equally certain.

### Work

Use the existing lookup tiers as named deterministic evidence classes:

| Evidence | Initial score | Meaning |
| --- | ---: | --- |
| direct noun map | 1.00 | Exact controlled noun/phrase |
| synonym match | 0.90 | Declared alias or equivalent phrase |
| morphology match | 0.75 | Declared suffix or lexical rule |
| context match | 0.65 | Shallow noun disambiguated by context |
| semantic cluster | 0.55 | Controlled corroboration, never a sole asset identity |
| generic fallback | 0.35 | Kind-level terminal identity |

Scores are match confidence, not a claim that a model is visually correct.
Deduplicate evidence sources so repeating the same word cannot inflate the
score. Record:

```json
{
  "confidence": 0.92,
  "confidence_tier": "direct",
  "evidence": ["name:peasant", "archetype:medieval_human_commoner"],
  "fallbacks": ["medieval_human_worker", "generic_human_kind"],
  "asset_query_allowed": true
}
```

### Gates

- `>= 0.85`: strong identity; attributes and compatible catalog query allowed.
- `0.60–0.84`: usable identity; variant selection may proceed, but automatic
  behavior or assembly requires corroboration.
- `0.40–0.59`: metadata-only candidate; do not steer asset selection.
- `< 0.40`: abstain unless the caller explicitly requested a neutral kind
  fallback; record the warning and remedy.

Alternatives are ranked by `(-score, family_id)` and only used when they pass
the same family/kind compatibility checks. A fallback prevents a crash only
when it is declared and compatible; it is never a silent prop substitution.

### Acceptance

Tests cover confidence boundaries, ties, corroboration, missing lookup data,
generic fallback query suppression, stable alternative ordering, and the full
image-to-entity lineage. Resolver output and warnings are byte-stable.

## Level 8 — Full bounded ontology

### Goal

Provide a knowledge-graph-like registry while keeping compilation finite,
fast, reproducible, and auditable.

### Scope

Represent versioned nodes and typed relations for:

- identity families and inheritance;
- attributes and modifiers;
- roles and affordances;
- context variants;
- visual evidence mappings;
- equipment, animation, prefab, asset, atom, and backend capability refs;
- compatibility, exclusion, requirement, and composition relations;
- confidence and provenance for every derived edge.

### Work

- Keep the source registry in reviewable JSON/YAML; do not introduce a graph
  database as a prerequisite.
- Build a deterministic closure pass with cycle detection, orphan detection,
  conflict reports, canonical sorting, and a content hash.
- Cache only by registry hash plus canonical entity input.
- Keep backend adapters thin and explicit. A backend lacking a capability
  reports the missing edge rather than weakening the ontology.
- Add corpus-level acceptance: Unity and GTA fixtures, visual runs, authored
  scenes, neutral fallbacks, and reproducibility checks.

### Exit criteria

Level 8 is complete only when the ontology can compile a representative set of
entities end to end, every derived decision has provenance, unresolved meaning
abstains loudly, repeated inputs are byte-identical, and the final Unity/GTA
artifacts pass their applicable structural and runtime gates.

## Delivery order and next step

Deliver this as small, reviewable slices:

1. Level 1 contract audit and negative matrix.
2. Level 2 attributes plus sidecar serialization.
3. Levels 3–4 roles, affordances, and variants.
4. Level 5 visual grounding and lineage checks.
5. Level 6 declarative assembly profiles and backend adapters.
6. Level 7 confidence gates and fallback reporting.
7. Level 8 registry closure and corpus acceptance.

The immediate next slice is **Level 2**. Read and review the current lookup
JSON, resolver, resolver tests, and identity documentation; then add only the
attribute bundle and sidecar contract. Rewrite or reject any proposed field
that would make the Pipeline LLM responsible for Steps 4–7, change authored
asset authority, or invent an unregistered atom/backend capability.

For every slice, run the focused tests, `py_compile`, `git diff --check`, and
the relevant Unity/GTA structural gate. Open a GitHub Pull Request immediately
after the slice is validated; no feature is complete until it is PR'd.
