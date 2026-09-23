# Visual Reasoning Compiler

## Status

This document defines the image-led compiler mode. Slice 1 is implemented:
`vizconcept.txt` to one canonical ComfyUI board and a complete provenance
manifest. Slice 2 now adds the image-only Qwen boundary: one visual inventory
JSON, one image-derived `Concept.txt`, and a provenance manifest linking both
outputs to the board hash. Live smoke runs remain environment-dependent and
must be reported separately from the checked-in contract and mocked tests.
Slice 3 now closes those artifacts deterministically through an explicit
Concept entity-reference block and a hash-checked closed image IR. Slice 4
now fulfils that closed request set from frozen catalog data without adding a
network or model call to the deterministic compiler.

The existing semantic-first visual compiler work is not the contract described
here. This mode starts from a visual seed and lets the rendered image direct
both game design and asset fulfilment.

## Working thesis

The proposed compiler treats a rendered game image as an intermediate
representation that can be reasoned over and compiled into an executable game:

```text
visual concept -> rendered board -> visual reasoning -> game IR + asset IR
                -> deterministic fulfilment -> Unity project
```

The image is not merely a reference thumbnail. In this mode it is the creative
source from which the game Concept and the visual shopping list are derived.
The image generator is still not trusted to produce executable truth, and the
LLM is not trusted to select files or silently change the build. Those later
boundaries remain explicit, validated, and recorded.

While prior work explores visual compilers that translate structured visual
graphs into executable operations (e.g.,
[VGCompiler](https://arxiv.org/abs/2609.22327)), no existing system identified
in the current review treats a rendered image as a typed intermediate
representation that drives deterministic asset selection, visual identity
resolution, and full game-project generation.

The specific pipeline — rendered visual seed → visual reasoning → semantic
Concept + asset fulfilment → validated Unity project — appears to be an
original combination not present in current literature. This remains a scoped
research-positioning claim, not a universal proof of nonexistence; the
[arXiv search](https://arxiv.org/search/cs?query=Visual%20Reasoning%20Compiler&searchtype=all&abstracts=show&order=-announced_date_first&size=50)
should be revisited before publication.

## End-to-end plan

### Step 1 — Render the visual seed

Input:

- `vizconcept.txt`, a human-authored visual/game-screenshot prompt
- fixed ComfyUI checkpoint and generation settings

Output:

- `board.png`, the canonical visual intermediate representation
- `board.comfy.json`, the immutable provenance manifest

This step does not call Qwen, create `Concept.txt`, query Sketchfab, download
models, fill a schema, or generate Unity files.

### Step 2 — Reason over the image

Send only `board.png` plus fixed, checked-in production instructions to Qwen.
The original `vizconcept.txt` is deliberately not read by this stage. Produce
two separate artifacts in sequential calls so one vision-capable 9B model can
remain loaded on an 8 GB card:

1. a visual inventory / dictionary containing discrete visible objects and
   noun-only Sketchfab queries;
2. `Concept.txt`, describing what is visible, interpreting the setting and
   mood, and proposing a game whose mechanics and entities are grounded in
   those objects.

The image must dictate the game direction. The inventory is strict JSON with
visible elements, exact-label query references, legible visible text, and
uncertainties. Its authoritative asset fields are short visible noun
phrases: no commentary, inferred roles, gameplay terms, or query enrichment.
The inventory description field is a compatibility mirror of the label, not a
second prose channel.
Scene summary and uncertainty text are audit metadata only and cannot create
asset requests. The validator rejects query terms that are not present in
that object's own label or model hint. `Concept.txt` is the natural-language
interpretation pass and may make modest mechanics inferences, but it may not
introduce unsupported named entities or pipeline context. Raw model responses
are retained beside both canonical outputs for auditability.

### Step 3 — Reconcile image reasoning

Run deterministic reconciliation over the Slice-2 inventory, `Concept.txt`,
and its provenance manifest. Slice 3 does not look at the original prompt and
does not ask Qwen to judge its own output. `Concept.txt` carries an explicit
plain-text `VISIBLE ENTITY REFERENCES` block so the compiler has a bounded,
auditable entity channel instead of pretending that arbitrary prose can be
parsed perfectly.

Reject:

- Concept entity references with no unique inventory match;
- asset-worthy inventory elements omitted from the reference block;
- Sketchfab queries that target UI/effects or have no model hint;
- any input whose hashes do not match the Slice-2 manifest.

Optional UI/effect elements may remain unreferenced, but are recorded as
warnings. Generic prose outside the explicit reference block is not silently
treated as a hidden entity channel. This produces the closed image-derived
game/asset intermediate representation without an LLM call.

### Step 4 — Fulfil assets from frozen inputs

The deterministic fulfilment pass consumes the closed image IR and the
Slice-3 reconciliation manifest. It reads the existing local asset catalog
plus the frozen `lookups/unity/sketchfab_catalog.json` partition, performs
stable semantic-kind scoring, license checks for Sketchfab entries, safe path
checks, and byte hashing. A missing, ambiguous, incompatible, unlicensed, or
unreadable candidate is recorded as an explicit rejection. It never searches
Sketchfab, downloads a model, calls Qwen, or invents a fallback.

Live Sketchfab search/download/Blender conversion remains an explicit
pre-freeze preparation operation in `src/sketchfab_freeze.py`. Its separate
vision adjudicator may select one candidate or refuse all candidates after
looking at the candidate previews. Refusal, unavailable vision, malformed
vision output, and a selected UID that is not in the proposed pool all stop
freezing; none may fall back to keyword order. The frozen entry records the
decision, reason, candidate UIDs, and preview hashes. After that operation
has written a frozen catalog entry, rerun Slice 4. Slice 4 itself remains
offline, deterministic, and unaware of Qwen. Model filenames and hashes belong
in asset metadata, never in `Concept.txt`.

### Step 5 — Compile from the frozen visual IR

Slice 5 is an explicit precomputed-Concept adapter around the existing Unity
pipeline. It must not call the normal Step-2 Concept writer: that would turn
the image-derived Concept into a second, untracked interpretation. The only
Pipeline LLM work in this slice is the existing Step-3 schema fill, using the
validated image-derived `Concept.txt` as its Step-2 result.

After schema fill, deterministic binding maps schema entities to the closed
IR's visual references and the exact Slice-4 selections. The generator then
copies those selected files through the normal Unity asset-binding chain; it
does not score the catalog again or silently substitute another model. An
authored Scene Compiler model remains higher authority. A missing or
ambiguous required visual binding rejects before generation.

### Step 6 — Validate and preserve evidence

Run deterministic schema, asset, code, and playability checks. Preserve a run
manifest linking:

```text
vizconcept hash
board hash
Qwen outputs and hashes
Concept hash
visual dictionary hash
catalog/download hashes
schema hash
Unity validation results
```

## Slice 1 — detailed contract

### Command

```powershell
& .\.venv\Scripts\python.exe main.py unity-vizconcept-board `
  --vizconcept-file 'prompts\vizconcept.txt' `
  --output 'output\visual_reasoning\git_run\board.png' `
  --manifest 'output\visual_reasoning\git_run\board.comfy.json' `
  --checkpoint 'sd15ModelLab_realV001_1651730.safetensors' `
  --seed 978075547277410 `
  --width 768 `
  --height 512 `
  --steps 20 `
  --cfg 8 `
  --sampler dpmpp_2m_sde_gpu `
  --scheduler karras
```

The command is intentionally separate from `unity-visual-board`, which
belongs to the older Concept-first branch of work.

When a strong existing render cannot be reproduced on the available GPU, use
the provenance-preserving import boundary instead of substituting a weaker
regeneration:

```powershell
& .\.venv\Scripts\python.exe main.py unity-vizconcept-import `
  --board-image 'path\to\known-good-board.png' `
  --vizconcept-file 'prompts\vizconcept.txt' `
  --output 'output\visual_reasoning\imported_run\board.png' `
  --manifest 'output\visual_reasoning\imported_run\board.import.json'
```

The import manifest records the exact board hash and optional visual-concept
hash. It explicitly marks the original Comfy workflow as unknown; it never
pretends that an imported image was regenerated by this machine.

### Input rules

- `vizconcept.txt` must exist, be non-empty, and decode as UTF-8.
- The raw source bytes receive a SHA-256 hash before generation.
- Existing board or manifest files are never overwritten.
- The source prompt is recorded in the manifest so the run remains auditable.

### Comfy workflow

The headless graph is:

```text
CheckpointLoaderSimple
    ├── CLIPTextEncode positive
    ├── CLIPTextEncode negative
    └── VAE
EmptyLatentImage -> KSampler -> VAEDecode -> SaveImage
```

The tested defaults are the supplied SD1.5 checkpoint, seed
`978075547277410`, 20 steps, CFG 8, `dpmpp_2m_sde_gpu`, and `karras`. The
landscape `768x512` default keeps the screenshot readable while remaining
appropriate for a 4060-class GPU; dimensions remain explicit CLI inputs.

The positive prompt is a deterministic wrapper around the raw visual concept.
The negative prompt removes generic rendering failures but does not ban text
or UI, because those may be meaningful game objects for visual reasoning.

### Manifest contract

`board.comfy.json` records:

- source path, exact source hash, and source text;
- positive and negative prompts;
- checkpoint, seed, dimensions, steps, CFG, sampler, and scheduler;
- Comfy base URL, client ID, prompt ID, and output reference;
- the complete submitted workflow graph;
- a request fingerprint;
- output path, MIME type, and exact PNG hash;
- explicit authority flags showing that no LLM or Concept was used.

### VRAM boundary

Slice 1 calls ComfyUI only. It does not load Qwen. On success or failure it
attempts `POST /free` so the next stage can load the vision model. LM Studio
unloading remains an orchestration concern until the image-led multi-stage
runner is designed.

### Slice 1 acceptance criteria

The slice is complete when:

- unit tests cover prompt wrapping, graph wiring, hashing, output refusal, and
  manifest contents;
- a mocked API test covers submit, history polling, image retrieval, and
  cleanup;
- a live ComfyUI run produces a valid PNG and manifest with the pinned
  settings;
- repeated requests with the same source/settings produce the same request
  fingerprint;
- no Qwen call, Concept file, Sketchfab request, model download, schema, or
  Unity project is created;
- the generated board is inspected before Step 2 is finalized.

### Slice 2 — detailed contract

#### Command

```powershell
& .\.venv\Scripts\python.exe main.py unity-vizconcept-reason `
  --board-image 'output\visual_reasoning\git_run\board.png' `
  --board-manifest 'output\visual_reasoning\git_run\board.import.json' `
  --inventory-output 'output\visual_reasoning\git_run\visual_inventory.json' `
  --concept-output 'output\visual_reasoning\git_run\Concept.txt' `
  --manifest 'output\visual_reasoning\git_run\reasoning.json'
```

`--board-manifest` is optional, but when supplied its recorded board hash must
match the PNG. The command refuses to overwrite any output, including the raw
response sidecars (`visual_inventory.raw.txt` and `Concept.raw.txt`).

#### Request boundary

- The inventory Qwen request contains exactly one `data:image/png` image and a
  fixed checked-in instruction. No source prompt text, board-manifest text,
  Concept, schema, asset catalog, or gameplay brief is sent.
- The Concept Qwen request contains that same image plus the ordered,
  validator-approved visible noun labels from the inventory. The labels are a
  closed structural alignment list: descriptions, model hints, Sketchfab
  queries, scene summaries, and uncertainties are not forwarded. The image
  remains the source for relationships, composition, setting, mood, and game
  mechanics. Use `--image-only-concept` to run the independent legacy mode.
- The inventory call uses JSON mode, fixed seed `731947`, temperature `0.0`,
  and reasoning disabled. It must return the five-field inventory payload. If
  the first response has format drift, one format-only retry uses seed
  `731950`; the same strict validator still decides acceptance.
- The Concept call uses fixed seed `731948`, temperature `0.55`, and reasoning
  disabled. It must return plain prose of at least 120 characters.
- Calls are sequential to keep the VRAM contract explicit.

#### Output and provenance contract

`visual_inventory.json` is the canonical
`visual-reasoning-compiler/visual-inventory/v2` artifact. Its
`sketchfab_queries[].label` values must refer exactly to
`visible_elements[].label` values. Labels, descriptions, model hints, and
queries are validated as short visible noun phrases; the description mirrors
its label; a query cannot add semantic terms outside its object's own
label/model hint. A physical object's validated query is sufficient to create
an asset request; `model_hint` remains optional. `Concept.txt`
contains no model IDs,
filenames, asset queries, or references to the pipeline itself. It begins with
one deterministic reference block:

```text
VISIBLE ENTITY REFERENCES:
- <short visible entity phrase>
END VISIBLE ENTITY REFERENCES
```

Every concrete entity used by the concept must be listed there. Slice 3
matches those phrases conservatively against inventory labels, model hints,
and descriptions.

`reasoning.json` records the board hash, optional Slice-1 manifest hash, model,
fixed request instructions and settings, raw-response hashes, canonical output
hashes, and explicit authority flags proving that the original `vizconcept.txt`
was not sent to Qwen. In the default guided mode it also records the exact
validated inventory labels sent to the Concept call and verifies them again in
Slice 3. It does not copy the source prompt out of the Slice-1 manifest.

#### Slice 2 acceptance criteria

- mocked tests prove both calls receive only one PNG data URI and no source
  prompt text;
- malformed inventory fields, unknown query labels, empty inventories, and
  unsupported Concept formats abstain loudly;
- canonical outputs, raw response sidecars, and the provenance manifest are
  written only after both calls validate;
- a live run with the imported board is inspected before reconciliation is
  designed;
- no Sketchfab request, asset download, schema fill, or Unity generation occurs.

### Slice 3 — detailed contract

#### Command

```powershell
& .\.venv\Scripts\python.exe main.py unity-vizconcept-reconcile `
  --inventory 'output\visual_reasoning\git_run\visual_inventory.json' `
  --concept 'output\visual_reasoning\git_run\Concept.txt' `
  --reasoning-manifest 'output\visual_reasoning\git_run\reasoning.json' `
  --output 'output\visual_reasoning\git_run\closed_image_ir.json' `
  --manifest 'output\visual_reasoning\git_run\reconciliation.json'
```

Slice 3 is deterministic and makes zero LLM calls. It validates the Slice-2
manifest authority flags and hashes, revalidates the inventory contract,
parses the explicit Concept entity-reference block, and emits a self-contained
`visual-reasoning-compiler/closed-image-ir/v1` artifact. The closed IR carries
the board record, validated inventory, Concept text/prose, entity matches, and
asset requests with their matched Concept references.

Entity matching is intentionally conservative: normalized distinctive tokens
from a reference must match a unique inventory label, model hint, or
description. Ties and weak matches abstain. Every queried or otherwise
model-worthy inventory element must be referenced. UI/effect elements may be
unreferenced, but the closed IR records them as warnings. Slice 3 does not
attempt unrestricted noun-phrase extraction from prose; the explicit block is
the contract that makes the closure claim honest.

#### Slice 3 acceptance criteria

- a valid Slice-2 manifest and matching inventory/Concept hashes are required;
- an unmatched or ambiguous Concept entity reference rejects the run;
- an omitted asset-worthy inventory element rejects the run;
- a query for UI/effect content or an element without a model hint rejects the
  run;
- the output and reconciliation manifest are written only after all checks
  pass, and existing artifacts are never overwritten;
- no source prompt, Sketchfab request, asset download, schema fill, or Unity
  generation occurs.

### Slice 4 — detailed contract

#### Command

```powershell
& .\.venv\Scripts\python.exe main.py unity-vizconcept-fulfill-assets `
  --closed-ir 'output\visual_reasoning\git_run\closed_image_ir.json' `
  --reconciliation-manifest 'output\visual_reasoning\git_run\reconciliation.json' `
  --lookups-dir 'lookups' `
  --output 'output\visual_reasoning\git_run\asset_fulfilment.json' `
  --manifest 'output\visual_reasoning\git_run\asset_fulfilment.manifest.json'
```

The command requires the Slice-3 output hash recorded in its reconciliation
manifest. It emits
`visual-reasoning-compiler/asset-fulfilment/v1` with the closed request
fingerprint, catalog-file hashes, selected relative asset paths, source and
license metadata, file SHA-256/byte counts, and explicit rejections. A
rejected result is written for inspection and exits non-zero; it is not
silently downgraded to a generic prop.

Slice 4 acceptance criteria:

- the Slice-3 IR and reconciliation manifest must both be present and hash
  matched;
- no LLM, network, live Sketchfab search, download, or Blender conversion is
  reachable from the fulfilment command;
- all selected files must remain under their catalog root and be readable;
- frozen Sketchfab entries must carry one of the configured accepted licenses;
- equal best matches reject as ambiguous rather than choosing by accident;
- the output records enough hashes and provenance to hand the resolved asset
  map to the existing Unity asset-binding chain.

### Slice 5 — detailed contract (rewritten before implementation)

The stale version of this step said that `unity-create --file Concept.txt`
was sufficient. It is not: `UnityPipeline.run()` would treat that file as a
new user brief, call the normal Concept stage again, and the generator would
re-score the catalog instead of honouring Slice 4. Slice 5 therefore requires
an explicit precomputed-Concept entry point and a first-class visual asset
binding input.

#### Proposed command boundary

```powershell
& .\.venv\Scripts\python.exe main.py unity-vizconcept-compile `
  --closed-ir 'output\visual_reasoning\git_run\closed_image_ir.json' `
  --asset-fulfilment 'output\visual_reasoning\git_run\asset_fulfilment.json' `
  --asset-manifest 'output\visual_reasoning\git_run\asset_fulfilment.manifest.json' `
  --config 'config.yaml' `
  --output 'output\unity\visual_git_run'
```

The command must:

1. verify the Slice-3 closed IR hash and the Slice-4 fulfilment-manifest
   hash/fingerprint before contacting the schema-fill model;
2. use the Concept text carried by the closed IR as the precomputed Step-2
   concept and make exactly the existing Step-3 schema-fill call (plus its
   existing bounded retry when validation fails);
3. send no board image, original `vizconcept.txt`, raw catalog, asset
   filenames, asset IDs, or hidden gameplay context to that model;
4. deterministically bind schema entities to the closed IR visual references,
   then to the exact fulfilled asset selections; ties, omissions, and
   invented required visual entities abstain before generation;
5. pass those bindings through an explicit resolver/generator parameter. Do
   not smuggle them through `schema.meta`, a fake Scene Compiler layout, or a
   second catalog search; and
6. run the existing deterministic Steps 4-7 tail, write the schema/project,
   and preserve one run manifest linking every input, prompt/output hash,
   schema hash, asset-file hash, and validation result.

#### Authority and binding order

The authority order for a visual entity is:

```text
authored Scene Compiler model
    -> exact Slice-4 visual asset binding
    -> existing deterministic catalog resolver for non-visual engine content
    -> explicit rejection in visual-strict mode
```

The selected asset is identified by catalog source, relative path, source
hash, and license metadata. Entity matching is deterministic and conservative
over the entity name, kind, model hint, and the closed inventory label/model
hint. A category word alone is not enough. UI/effect entities stay on their
existing engine-native paths and are not forced into mesh assets.

#### Slice 5 acceptance criteria

- `unity-create --file` remains unchanged; the precomputed-Concept path is a
  separate command and has its own tests;
- the Slice-2 Concept is preserved byte-for-byte and the normal Concept call is
  provably skipped;
- the schema-fill prompt contains the image-derived Concept but no board,
  source visual prompt, catalog filenames, or asset identifiers;
- every required visual reference has one unique schema-entity binding and
  one exact Slice-4 asset selection, or the run rejects before generation;
- generated files include the selected asset bytes through the existing
  resolver/generator copy path, with no second scoring decision;
- Steps 4-7 remain deterministic and LLM-free after schema fill;
- the run manifest makes the precomputed-Concept transformation explicit,
  including all source and output hashes.

## Design rules

1. Preserve the raw visual seed and every intermediate artifact.
2. Keep image-led and semantic-first modes separate and named.
3. Let Qwen interpret the image, but keep file choice and project wiring
   deterministic.
4. Never insert model filenames into `Concept.txt`.
5. Reject unsupported or contradictory intermediate artifacts loudly.
6. Do not claim exact image reproducibility beyond the recorded request and
   environment fingerprint unless the Comfy backend proves byte equality.

## Research positioning

The term “visual reasoning compiler” has adjacent uses in current literature,
including visual graph reasoning systems that recover an executable graph
representation. This project should therefore describe its novelty precisely:
it proposes a game-production compiler in which a generated screenshot is the
creative intermediate representation, a VLM derives both game intent and asset
intent from that image, and deterministic tooling compiles those intents into
real downloadable assets and an executable Unity project.

The literature position should be revisited before publishing claims or a
paper; the repository should preserve dated searches and links rather than
relying on an absolute “nobody has done this” statement.
