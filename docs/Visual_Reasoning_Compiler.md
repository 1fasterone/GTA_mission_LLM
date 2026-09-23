# Visual Reasoning Compiler

## Status

This document defines the image-led compiler mode. Slice 1 is implemented:
`vizconcept.txt` to one canonical ComfyUI board and a complete provenance
manifest. Slice 2 now adds the image-only Qwen boundary: one visual inventory
JSON, one image-derived `Concept.txt`, and a provenance manifest linking both
outputs to the board hash. Live smoke runs remain environment-dependent and
must be reported separately from the checked-in contract and mocked tests.
Slice 3 now closes those artifacts deterministically through an explicit
Concept entity-reference block and a hash-checked closed image IR.

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

1. a visual inventory / dictionary containing discrete asset-worthy objects,
   model hints, and Sketchfab queries;
2. `Concept.txt`, describing a game whose mechanics and entities are built from
   the objects visible in the image.

The image must dictate the game direction. The inventory is strict JSON with
visible elements, exact-label query references, legible visible text, and
uncertainties. `Concept.txt` is plain prose and may make modest mechanics
inferences, but it may not introduce unsupported named entities or pipeline
context. Raw model responses are retained beside both canonical outputs for
auditability.

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

### Step 4 — Resolve and download assets

The deterministic resolver consumes the validated Concept and visual
dictionary. It performs catalog/Sketchfab search, semantic-kind checks, visual
checks, license checks, download verification, and explicit fallback or
rejection. Model filenames and hashes belong in asset metadata, never in
`Concept.txt`.

### Step 5 — Compile the game

The existing Unity schema/compiler consumes the image-derived Concept and the
resolved asset map. The generated project wiring receives the downloaded model
paths through the normal asset-binding contract.

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

- Each Qwen request contains exactly one `data:image/png` image and a fixed
  checked-in instruction. No source prompt text, board-manifest text, Concept,
  schema, asset catalog, or gameplay brief is sent.
- The inventory call uses JSON mode, fixed seed `731947`, temperature `0.2`,
  and reasoning disabled. It must return the five-field inventory payload.
- The Concept call uses fixed seed `731948`, temperature `0.55`, and reasoning
  disabled. It must return plain prose of at least 120 characters.
- Calls are sequential to keep the VRAM contract explicit.

#### Output and provenance contract

`visual_inventory.json` is the canonical
`visual-reasoning-compiler/visual-inventory/v1` artifact. Its
`sketchfab_queries[].label` values must refer exactly to
`visible_elements[].label` values. `Concept.txt` contains no model IDs,
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
was not sent to Qwen. It does not copy the source prompt out of the Slice-1
manifest.

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
