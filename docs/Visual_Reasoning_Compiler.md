# Visual Reasoning Compiler

## Status

This document defines the image-led compiler mode. Slice 1 is implemented
locally: `vizconcept.txt` to one canonical ComfyUI board and a complete
provenance manifest. Its live smoke run remains pending a user-authored
`vizconcept.txt`. Later slices remain deliberately open until that board has
been produced and inspected.

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

Send only `board.png` plus fixed production instructions to Qwen. Produce two
separate artifacts:

1. a visual inventory / dictionary containing discrete asset-worthy objects,
   model hints, and Sketchfab queries;
2. `Concept.txt`, describing a game whose mechanics and entities are built from
   the objects visible in the image.

The original `vizconcept.txt` is not sent to Qwen. The image must dictate the
game direction. The exact output contract will be revised after Slice 1 shows
what information the board actually carries.

### Step 3 — Reconcile image reasoning

Check that the visual inventory and `Concept.txt` describe the same visible
world. Flag or reject:

- Concept entities with no visual support;
- inventory objects omitted from the game concept;
- invented objects not present in the image;
- queries that do not correspond to an asset-worthy object.

This produces the closed image-derived game/asset intermediate representation.

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
