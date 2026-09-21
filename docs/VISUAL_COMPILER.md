# Visual Compiler

## Thesis

The project is evolving toward a visual compiler in the strict computer
science sense:

```text
semantic authority -> deterministic transformation -> validated output
```

The compiler does not treat an image generator, an asset store, or an LLM as
the final authority. It binds semantic game intent to visual identity, turns
that binding into deterministic asset choices and layout inputs, and rejects
outputs that do not satisfy the contract.

**Visual compiler:** A deterministic system that binds semantic game intent to
visual identity, produces validated asset and layout choices, and rejects
outputs that violate meaning, appearance, or provenance constraints.

This is an architectural target, not a claim that adjacent tools do not
provide individual pieces of it. Renderers, procedural systems, scene
description systems, image generators, and asset search services each solve a
different part of the problem. The visual compiler is the contract that joins
those parts while preserving meaning and repeatability.

## Compiler pipeline

```text
Concept
  | semantic truth: entities, counts, mechanics, behavior, requirements
  v
Visual board
  | visual authority: shape, palette, material, lighting, silhouette, motifs
  v
Step 3 visual dictionary
  | entity roles -> model hints and deterministic shopping-list targets
  v
Deterministic asset resolver
  | catalog, kind gates, text/image similarity, license and provenance checks
  v
Layout and Unity generation
  | deterministic placement, wiring, code generation, and validation
  v
Validated project
```

The Pipeline LLM remains limited to Steps 2–3. It may interpret the Concept
and visual board and fill the existing schema fields such as `model_hint` and
`sketchfab_query`. Steps 4–7 remain deterministic and LLM-free.

Within the visual-compilation boundary, **the LLM is an annotator, not a
generator. It interprets; it does not invent.** It can annotate the declared
entities with visual targets, but it cannot create gameplay entities, choose
an unvalidated runtime asset, or replace deterministic layout and generation.

## Authority boundaries

**Visual authority = the allowed space of appearance, not the allowed space
of meaning.** A board can constrain what a declared entity should look like;
it cannot decide what the game means or which mechanics exist.

| Source | Owns | Does not own |
| --- | --- | --- |
| Concept | gameplay meaning, entity identity, counts, mechanics, behavior, required systems | exact model appearance |
| Visual board | appearance, silhouettes, palette, materials, lighting, composition, environmental motifs | new gameplay entities or mechanics |
| Schema | role binding between semantic entities and visual targets | live catalog availability |
| Resolver | actual catalog model, score, license, fallback, or abstention | gameplay meaning |
| Layout/compiler | placement, transforms, wiring, generated files, validation | inventing semantic requirements |

When authorities disagree, semantic truth wins over visual suggestion. A board
may show a toy block or rubber duck as an environmental motif, but it must not
silently create a gameplay pickup, NPC, or objective that the Concept does not
declare. If the board contradicts the Concept, the board is wrong. That is a
compiler error in the visual input, not permission to rewrite semantic truth.

## The visual dictionary / model shopping list

The key intermediate artifact is an auditable visual dictionary. Conceptually,
each entry answers:

```text
semantic role: rival_car_01
visual target: chunky orange toy off-road racer
model query: toy off-road race car
catalog kind: vehicle
accepted candidate: catalog asset X
text score: 0.91
image score: 0.84
status: accepted
```

The final implementation must use the repository's existing schema and asset
catalog contracts. A dictionary sidecar can carry provenance and evaluation
details; schema changes are not implied by this document. If a new field is
truly needed during experimentation, it belongs in the schema's `extensions`
namespace until a deliberate promotion is reviewed.

The resolver must abstain loudly when a candidate has the wrong kind, fails a
semantic threshold, violates a license/catalog rule, or cannot be tied to a
declared entity. A visually attractive but semantically wrong model is a
rejection, not a successful fallback.

## Board strategy

### Version 0: one canonical board

The first implementation should compile one canonical visual board per
Concept. This gives us a clean baseline:

```text
same Concept + same board + same catalog snapshot + same config
    => same dictionary + same model choices + same validation report
```

It also makes drift measurable. If a result changes, we can identify whether
the change came from Concept extraction, board generation, catalog contents,
resolution thresholds, or layout.

### Version 1: labeled board sets

The visual compiler should eventually support multiple boards, but every board
must have an explicit purpose and lineage. Useful board roles include:

| Board role | Main effect | Must not change |
| --- | --- | --- |
| `canonical` | baseline appearance and composition | semantic entity set and behavior |
| `style_variant` | art direction, palette, material language | entity identity or gameplay meaning |
| `lighting_variant` | time of day, contrast, shadows, atmosphere | model role or required mechanics |
| `silhouette_variant` | proportions, outline, massing, readability | entity count or semantic kind |

Variants should produce separate dictionary candidates or separate compiled
style manifests. They should not be blended into one ambiguous board and they
should not be unioned into the schema as if every visible object were a
required entity.

The deterministic policy should be:

1. Compile the canonical board first.
2. Compile each labeled variant against the same Concept and catalog snapshot.
3. Score each variant using the dimensions it is allowed to influence.
4. Select one approved variant, or retain multiple named outputs for explicit
   A/B comparison.
5. Never allow a lighting-only variant to replace an entity model merely
   because its pixels are more similar.

This gives us multi-style dictionaries without sacrificing a single semantic
truth. It also lets us test whether a variant improves visual fidelity or only
causes attractive drift.

## Determinism and rejection contract

Every compile must record enough provenance to reproduce or explain the
result:

- Concept text and hash
- board image bytes, prompt, seed, model identifier, and hash
- parent board or Concept run identifier
- schema output and hash
- catalog snapshot/config hash
- resolver scores and thresholds
- accepted candidates and explicit rejections
- layout and validation results

The resolver and compiler must be deterministic for fixed inputs. Missing or
conflicting evidence must be reported as an abstention with a remedy, never
silently converted into an unrelated model or stale output.

The visual compiler is intentionally modular. New board types, similarity
metrics, and catalog sources can be added without changing semantic truth or
weakening the authority boundaries above. Each extension must still publish
its inputs, deterministic policy, scores, provenance, and rejection behavior.

## Build plan

1. **Experiment baseline** — preserve the image-vs-text E2E run, prompt files,
   hashes, and lineage. This is recorded in
   [`E2E_REFERENCE_IMAGE_EXPERIMENT.md`](E2E_REFERENCE_IMAGE_EXPERIMENT.md).
2. **Canonical board generation** — generate one visual board from the winning
   Concept and record complete provenance.
3. **Step 3 bridge** — pass Concept plus canonical board to schema generation;
   fill existing asset-hint fields without allowing the board to invent
   gameplay structure.
4. **Shopping-list sidecar** — expose entity-level visual targets, candidate
   scores, rejections, and lineage without changing the production schema.
5. **Deterministic resolver** — combine entity kind, text hints, board/image
   similarity, catalog metadata, and license rules; reject mismatches loudly.
6. **Multi-board variants** — add labeled style, lighting, and silhouette
   boards only after the single-board baseline is repeatable.
7. **Feedback loop** — optionally render the compiled project, compare the
   result against the Concept and approved board, and retain every iteration
   rather than overwriting it.

## Definition of success

The visual compiler is working when it can take a held-out Concept and a
canonical board and produce:

- the same visual dictionary on repeated runs with frozen inputs;
- model choices that match both semantic kind and visual target;
- explicit rejection reports for bad candidates;
- deterministic layout and generated project artifacts;
- a validation report showing playability and visual-binding outcomes; and
- complete lineage for every board, dictionary, candidate, and compiled run.

That is the eureka moment made testable: not “the AI made a pretty image,” but
“the compiler used meaning to decide what the image meant, used the image to
choose how the declared entities should look, and proved when the result was
valid.”
