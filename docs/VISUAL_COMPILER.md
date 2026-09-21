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

## Current implementation status

The repository now has the first semantic-authority seam: `unity-create
--concept-only` runs Step 2 and writes an immutable `Concept.txt` plus an
authority manifest. It rejects reference images and stops before schema fill,
asset resolution, or generation. The existing `unity-create
--reference-image` path remains the legacy Step-2 image experiment; it is not
the semantic-authority path.

Phase 1 also has deterministic evidence tooling: completed E2E runs can be
turned into model-fulfilment shopping-list sidecars with
`unity-visual-shopping-list`. Full runs now place visual query generation in
Step 3: the annotator writes a validated `visual_dictionary.json` sidecar and
the downstream resolver consumes it before any asset choice. Those old
image-only E2E outputs remain useful resolver evidence, but they are not
allowed to redefine semantic truth.

The existing `output/concept_loop` capture is retained as historical migration
evidence. It proves the real Step-2 image boundary and retains downstream
image-feedback builds, but its image-only prompt cannot become the semantic
authority for the corrected flow. The strict same-prompt text-vs-image
comparison remains a separate follow-up experiment.

The Phase 1 protocol and command are documented in
[`E2E_REFERENCE_IMAGE_EXPERIMENT.md`](E2E_REFERENCE_IMAGE_EXPERIMENT.md).

### Migration evidence: the E2E-derived model shopping list

The immediate use of the E2E data is not to declare a visual winner. It is to
turn each generated schema into an auditable fulfilment list for the model
resolver. The list is deterministic and read-only:

```text
schema.entities[].model_hint + count + kind
    + schema.meta.entity_model_paths
    + concept / board / schema hashes
    => visual shopping-list entry
```

The current reproduction is retained at
`output/concept_loop/reproduction_20260921/`. It contains the original-image
E2E run and the concept-to-generated-image E2E run, both using the unchanged
`prompts/e2e_image_only.txt`. These runs are the source data for the first
shopping list; they are not a strict text-only versus image-only A/B pair.

The deterministic extractor is now implemented as
`unity-visual-shopping-list`. It produced six entries for the original-image
run and five entries for the generated-image run:

```text
python main.py unity-visual-shopping-list \
  output/concept_loop/reproduction_20260921/01_source_image_e2e/DirtDrifterRacer \
  --board-image output/concept_loop/reproduction_20260921/01_source_image_e2e/input.png \
  --output output/concept_loop/reproduction_20260921/01_source_image_e2e/shopping_list.json
```

The generated sidecars are
`output/concept_loop/reproduction_20260921/01_source_image_e2e/shopping_list.json`
and
`output/concept_loop/reproduction_20260921/02_generated_image_e2e/shopping_list.json`.
They contain deterministic identities, exact provenance hashes, entity demand,
resolver candidates, asset-gap findings, and the current `sketchfab_query`
board. The command is LLM-free and does not modify the project schema.

The query-board views are:

- `output/concept_loop/reproduction_20260921/01_source_image_e2e/sketchfab_query_board.json`
- `output/concept_loop/reproduction_20260921/02_generated_image_e2e/sketchfab_query_board.json`

These are legacy schema exports showing the historical resolver-facing query
shape. They prove the input/output boundary, but they are not the corrected
board-annotator artifacts. New runs use
`src/visual_compiler_dictionary.py` at Step 3, write
`visual_dictionary.json`, and apply only entries whose entity names already
exist in the schema. The legacy `unity_sketchfab_translator.py` remains only
for compatibility with old callers; the Unity pipeline no longer invokes it.

For a corrected full run, use the immutable Concept and optional canonical
board explicitly:

```text
python main.py unity-create \
  --concept-file output/visual_compiler/<run>/Concept.txt \
  --file prompts/<semantic-source>.txt \
  --visual-board output/visual_compiler/<run>/board.png \
  --visual-dictionary-output output/visual_compiler/<run>/visual_dictionary.json
```

The dictionary is generated after schema fill but before Step 4. Supplying an
existing `--visual-dictionary` skips that LLM annotation and verifies its
Concept.txt hash (and board hash when a board is supplied). Neither path edits
Concept.txt or appends entities.

The headless board boundary is:

```text
python main.py unity-visual-board \
  output/visual_compiler/<run>/Concept.txt \
  --output output/visual_compiler/<run>/board.png \
  --checkpoint sd15ModelLab_realV001_1651730.safetensors \
  --seed 978075547277410 --steps 20 --cfg 8 \
  --sampler dpmpp_2m_sde_gpu --scheduler karras
```

This submits the explicit checkpoint, CLIP positive/negative text, and
KSampler graph to ComfyUI's local `/prompt` API, polls `/history`, retrieves
the PNG through `/view`, records the workflow manifest, and calls `/free`
before LM Studio is used again. The GUI is not part of the production path.

For the Phase 1 stop boundary—Concept + board + dictionary + catalog/Sketchfab
model resolution/download, with no Unity project generation—use:

```text
python main.py unity-create \
  --concept-file output/visual_compiler/<run>/Concept.txt \
  --visual-board output/visual_compiler/<run>/board.png \
  --visual-dictionary-output output/visual_compiler/<run>/visual_dictionary.json \
  --visual-assets-only --seed 42 --no-think
```

That command writes `schema.json` and `visual_assets.json` beside the
dictionary, consumes the same deterministic resolver used by the generator,
and stops before Unity files, model copies, C# validation, or package-cache
work. Existing files are never overwritten.

The first list is already visible in the captured schemas. For example:

| Run | Entity | Count | Visual target (`model_hint`) | Candidate emitted by resolver | Fulfilment signal |
| --- | --- | ---: | --- | --- | --- |
| original image | `RaceCarPlayer` | 1 | off-road race car chassis | `Models/Sketchfab/Vehicle/cartoon-sports-car-04302e4fd46b434abeb96475e241d00f` | candidate; visual review |
| original image | `RaceCarsRivals` | 5 | rival race car model | `Models/Sketchfab/Vehicle/car-x-31-8254e29f12b84ad9b79991a1adb9cb0b` | candidate; visual review |
| original image | `WoodenBarriers` | 15 | wooden barrier structure | `Models/Sketchfab/Environment/low-poly-house-with-wooden-balcony-and-tent-17cd260e9b7c4c60b833fd5deb5a914f` | candidate; visual review |
| original image | `GarageBackground` | 1 | garage interior backdrop | `Models/Sketchfab/Environment/simple-cave-props-stalagmite-variants-a8ca228ba7f2402e9557caa925b2e90f` | asset-gap warning |
| generated image | `PlayerVehicle` | 1 | offroad racing truck | `Models/Sketchfab/Vehicle/stylized-fighter-plane-081b0c9749a94b2baa769d6e2c14daba` | asset-gap warning |
| generated image | `DirtTrack` | 1 | dirt road surface | `Models/Sketchfab/Track/ultimate-low-poly-track-builder-pack-a9bdf62cc78c483d9963bad14b701e62` | candidate; visual review |
| generated image | `MountainRocks` | 15 | large mountain boulder | `Models/Sketchfab/Prop/4k-photorealistic-beach-boulder-17d7471a920e4931be5cad7c9c5f3233` | candidate; visual review |
| generated image | `WoodenBridges` | 3 | wooden suspension bridge | `Models/Sketchfab/Building/wooden-ethnic-square-wall-mirror-d72a45ba83574b97b66fb3ff61ea9c9f` | candidate; visual review |
| generated image | `BackgroundMountains` | 1 | distant mountain range | `Models/Sketchfab/Environment/simple-cave-props-stalagmite-variants-a8ca228ba7f2402e9557caa925b2e90f` | asset-gap warning |

This list is a fulfilment queue, not permission to silently accept the
candidate. `candidate` means the deterministic resolver produced a path;
`asset-gap warning` means the existing fidelity evaluator found vocabulary
evidence that the selected model is unrelated; `visual review` means the
candidate still needs comparison against the board image. An unresolved
entity is an explicit `unfulfilled` entry, never a default prop.

The sidecar must retain both the requested target and the selected candidate.
That makes the next deterministic step clear: search the frozen catalog for a
better kind-safe candidate, record the score and rejection reasons, and only
then update the compiled project. The shopping list must not rewrite the
schema or invent entities from visible board motifs. The second run being
`NOT PLAYABLE` is retained as a validation result; it does not erase the
model-fulfilment evidence produced before the gate.

## Compiler pipeline

```text
Semantic game prompt
  | gameplay truth only
  v
Concept.txt (immutable semantic authority)
  | entities, roles, counts, mechanics, win/lose, required systems
  v
Canonical visual board (visual authority)
  | appearance planning only: silhouette, palette, material, lighting, motifs
  v
Visual Dictionary sidecar (LLM annotator)
  | declared Concept entity -> model_hint, sketchfab_query, visual reference
  v
Deterministic resolver / fulfilment
  | catalog, kind gates, text/image similarity, license and provenance checks
  v
Full E2E compiler
  | Concept + board + dictionary -> schema -> deterministic Steps 4–7
  v
Validated Unity project
```

The Pipeline LLM remains limited to Steps 2–3. In the visual compiler flow,
Step 2 produces the semantic Concept, and the later annotation call may only
map board details onto entities already declared by that Concept. Steps 4–7
remain deterministic and LLM-free. The image never becomes a source of
entities, mechanics, objectives, or win/lose rules.

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

The sidecar contract is intentionally separate from `Concept.txt`:

```json
{
  "concept_hash": "...",
  "board_hash": "...",
  "entries": [
    {
      "entity": "RaceCarPlayer",
      "model_hint": "chunky orange toy off-road racer",
      "sketchfab_query": "low poly toy race car",
      "visual_reference": {"crop": "...", "hash": "..."}
    }
  ]
}
```

The dictionary may annotate only declared Concept entities. It must not append
entities, alter counts, add mechanics, or edit the Concept text. Resolver
results may be attached to the dictionary or written as a second fulfilment
sidecar, but both remain downstream of the immutable Concept hash.

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

1. **Author the semantic game prompt** — describe gameplay truth only:
   entities, roles, counts, mechanics, win/lose conditions, and required
   systems. Avoid visual language except where it changes semantic identity.
2. **Compile to Concept.txt only** — use `unity-create --concept-only`.
   Save the Concept and its hash; reject images and stop before schema fill.
3. **Generate one canonical visual board** — build it from Concept.txt with a
   pinned Stable Diffusion/Comfy configuration, seed, model identifier, and
   exact output hash. The board is a planning artifact, not gameplay truth.
4. **Annotate the board with a vision-capable LLM** — map visual details only
   onto Concept-declared entities and write `visual_dictionary.json` with
   `model_hint`, `sketchfab_query`, and optional visual references.
5. **Resolve models deterministically** — use entity kind, text similarity,
   image similarity, catalog/license constraints, and explicit rejection
   reasons. No LLM makes the final asset choice.
6. **Keep Concept.txt immutable** — link Concept, board, dictionary, and
   resolver outputs by hashes and run IDs. Never write model names into the
   semantic Concept.
7. **Run full E2E** — feed Concept + board + dictionary into Step 3, then let
   deterministic Steps 4–7 produce the project and validation report.

The tested local smoke run is retained under
`output/visual_compiler/headless_test_20260921_final4/`. Its dictionary records
the closed seven-entity set and its assets report records which Sketchfab
models were downloaded or which typed-safe default was retained. The board
itself is a planning artifact; the final SD1.5 board is not expected to show
every entity reliably, so the dictionary remains closed by Concept/schema
authority rather than treating board omissions as semantic deletion.

The prior image-only E2E capture and its shopping-list sidecars remain useful
as migration evidence for Step 5. They do not skip Steps 1–4 of this authority
flow and cannot be used as semantic source material.

### Phase gates

The semantic-authority Phase 1 gate is deliberately narrow:

- the source prompt is natural-language gameplay truth;
- `unity-create --concept-only` receives no reference image or board;
- `Concept.txt` is written once and never overwritten;
- the authority manifest records Concept, prompt, config, model, and seed
  provenance; and
- no schema, dictionary, asset, or Unity project is produced in this step.

The downstream fulfilment sidecar gate is:

- every entry comes from a Concept-derived schema entity;
- an unknown dictionary entity is rejected rather than appended;
- every entry retains `model_hint`, kind, count, and the resolver candidate;
- missing candidates are explicit `unfulfilled` records;
- concept, board image, schema, and validation provenance are hashed;
- resolver or fidelity warnings remain attached to the entry; and
- the sidecar is deterministic for frozen inputs.

The strict text-vs-image manifest remains available as a separate follow-up
gate. It should not block the semantic-authority flow or the later board and
dictionary work.

Legacy E2E manifests may be consumed as resolver evidence, but they may not
rewrite the new Concept authority or treat visible board motifs as gameplay
entities.

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
