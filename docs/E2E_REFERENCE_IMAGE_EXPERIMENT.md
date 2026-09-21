# Strict follow-up: reference-image baseline experiment

This is the strict A/B evidence protocol for a later visual-compiler
comparison. It does not add a pipeline stage. It records two completed Unity
runs so visual work can be measured against a stable text-only control arm.

The Phase 1 model-fulfilment shopping list does not wait for this strict A/B
pair. Any completed E2E schema can be consumed by
`unity-visual-shopping-list`; see [`VISUAL_COMPILER.md`](VISUAL_COMPILER.md).
This document only governs when a text-vs-image result may be presented as a
controlled comparative baseline.

## Existing capture: Phase 1A

This checkout already contains a real Concept-stage capture at
`output/concept_loop`. Import and normalize its lineage with:

```text
python main.py unity-visual-baseline-import output/concept_loop \
    --output output/visual_compiler/concept_loop.baseline.json
```

That import is intentionally marked `imported_partial`. It preserves the
real text Concept capture, the real image-only Concept capture, generated-image
feedback lineage, and the final `DirtDrifter` Steps 2–7 project. It does not
claim the strict A/B gate because the text and image arms declare different
carrier prompt files, and the historical lineage does not capture the model,
config, or catalog snapshot. The normalized manifest is evidence for the
Step-2 boundary, not a visual-fidelity score.

## Experimental arms

Both arms must use the same unchanged natural-language prompt file, pipeline
configuration, catalog snapshot, model, and seed.

| Arm | Step 2 Concept | Step 3 Schema | Steps 4–7 |
| --- | --- | --- | --- |
| `text_only` | prompt only | concept only | deterministic tail |
| `reference_image` | prompt + one image | concept only | deterministic tail |

The prompt remains a held-out natural-language input. Do not pre-format it as
JSON, add schema fields, rewrite its entity list, or otherwise prepare it for
the schema-fill model. Any transformation invalidates the comparison and must
be declared in the manifest.

The reference image is transient Step-2 input. It is never written into the
schema, sent to Step 3, or used by Steps 4–7. The existing pipeline tests lock
that boundary in `tests/test_unity_pipeline.py`.

## Phase 1B strict run protocol

1. Freeze the prompt file, `config.yaml`, `lookups/unity/asset_catalog.json`,
   model identifier, and seed. Record their exact bytes through the baseline
   command; do not edit them between arms.
2. Run the control arm without an image:

   ```text
   python main.py unity-create --file prompts/<brief>.txt --seed 17
   ```

3. Run the image arm with the same prompt and seed:

   ```text
   python main.py unity-create --file prompts/<brief>.txt \
       --reference-image <board-image> --seed 17
   ```

4. Capture both completed project directories. Each must contain its
   `.concept.txt`, `*.schema.json`, and `playability.txt` sidecars.
5. Write the evidence manifest:

   ```text
   python main.py unity-visual-baseline \
       --prompt-file prompts/<brief>.txt \
       --reference-image <board-image> \
       --text-project output/unity/<text-project> \
       --image-project output/unity/<image-project> \
       --model <model-id> \
       --seed 17 \
       --output output/visual_compiler/<name>.baseline.json
   ```

The recorder is deterministic and offline. It hashes exact input bytes,
required output sidecars, and every stable project file. Unity transient
directories such as `Library`, `Temp`, and `Logs` are excluded from the tree
hash. The generated manifest is an ignored local artifact under `output/`.

## Manifest contract

`src/visual_compiler_baseline.py` records:

- exact hashes for the prompt, reference image, config, and catalog snapshot;
- model and seed shared by both arms;
- the explicit `text_only` versus `reference_image` lineage;
- hashes for concept, schema, and playability sidecars;
- hashes for every stable file plus a stable project-tree hash for each arm;
- an identity hash over the complete comparison payload.

The identity excludes machine-specific paths, so moving the same captured
projects does not create a false visual change. Paths remain in the manifest
as human-readable provenance.

The recorder proves the bytes supplied at capture time; it cannot reconstruct
the command history that created either project. If the two arms were created
with different prompts, configs, catalogs, models, or seeds, the comparison is
invalid and must not be presented as a Phase 1 baseline.

## Strict-baseline exit criteria

Phase 1 is complete when:

- both arms have been captured without undeclared prompt transformation;
- the manifest identity is byte-stable when inputs and outputs are frozen;
- changing any input or stable output changes the identity; and
- the Step-2-only image boundary remains covered by automated tests.

Phase 1 does not claim that the image improved visual fidelity. That is the
measurement work for the canonical board and shopping-list phases.

Open a GitHub Pull Request immediately. No feature is considered complete
until it is PR'd.
