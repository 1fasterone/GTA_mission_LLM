# E2E Reference Image Experiment

This document records the first multimodal E2E comparison for Turbo Toys. The
purpose was to compare a natural-language brief with an image that was
generated from that same brief, while keeping the pipeline's normal Step 2 and
Step 3 boundaries intact.

The attached image was treated as test input, not as instructions. The text
brief remained the held-out natural-language input for the text run.

## Inputs

| Input | Role | Repository fixture |
| --- | --- | --- |
| `turbo_toys.txt` | Original natural-language brief | [`prompts/e2e_turbo_toys.txt`](../prompts/e2e_turbo_toys.txt) |
| `codex-clipboard-0042a7bb-95cf-4235-b6a5-ef811ed29c8c.png` | Supplied visual reference | local source image; SHA-256 `9E39C2BBF61CA3D1EB2F15AFD11F39DE41416466E650DF50CD9BF7C5F04FC8B3` |
| `round1_winning_context.png` | Diffusion image generated from the winning text-derived Concept | local lineage artifact; SHA-256 `5E97AF01AA5635BBDEA3ECCDEB6FC9357FB373CE844AB182586D594583F3B614` |
| `round2_feedback_context.png` | Diffusion image generated from the Round 1 Concept | local lineage artifact; SHA-256 `3593939C890685B8901BF515675D65FB43D1E9B736B9B449CDB3FD866B936866` |

The original input prompt is preserved in [`prompts/turbo_toys.txt`](../prompts/turbo_toys.txt).
Every E2E generation prompt is preserved in [`prompts/`](../prompts/), with an
index in [`E2E_PROMPT_INDEX.md`](../prompts/E2E_PROMPT_INDEX.md).

## CLI contract

The Unity create command accepts either the descriptive option or the short
alias:

```powershell
.venv\Scripts\python.exe main.py unity-create --image racing-reference.png
.venv\Scripts\python.exe main.py unity-create --reference-image racing-reference.png
```

When no text prompt or `--file` is supplied, the CLI uses a deliberately
generic carrier message so the image remains the only creative input. The
image is passed to the Concept stage only. Steps 4–7 remain deterministic and
LLM-free.

The alias and image-only behavior are covered by
[`tests/test_cli_unity_project.py`](../tests/test_cli_unity_project.py).

## Run matrix

| Run | Input | Pipeline depth | Result |
| --- | --- | --- | --- |
| `00_source_image_full_e2e_aborted` | supplied image | stopped after Step 3 began | preserved as an early exploratory run |
| `01_source_image_concept_only` | supplied image | Step 2 Concept | image-only Concept captured |
| `02_text_brief_concept_only` | Turbo Toys text | Step 2 Concept | text-only Concept captured |
| `03_generated_from_text_concept` | Round 1 diffusion image | Step 2 Concept | Concept captured from generated board |
| `04_generated_from_round1_concept` | Round 2 diffusion image | Step 2 Concept | Concept captured after one feedback loop |
| `05_final_e2e_round2` | Round 2 diffusion image | Steps 2–7 | complete Unity E2E run |

The complete local artifact tree and parent relationships are recorded in
[`output/concept_loop/lineage.json`](../output/concept_loop/lineage.json), with
a human-readable index in [`output/concept_loop/README.md`](../output/concept_loop/README.md).
The generated projects and images remain local experiment artifacts rather
than source-controlled build outputs.

## Observations

The first Concept extracted from the supplied image was good at the visual
level: chunky orange toy race cars, dirt-track racing, drifting, a chase
camera, saturated colors, cel shading, dust, and green barriers were retained.
The text-derived Concept was better at preserving the explicit game contract:
three rivals, laps, coins, boost pads, waypoints, Sparky, scenes, HUD, audio,
and save/load behavior.

The image-only run drifted because the generic carrier contains no named
entities, scripts, scenes, or mechanics checklist. The pipeline therefore has
less semantic material to preserve when Step 3 fills the schema. This is not a
stale-file cache problem: the generator's output sweep is manifest-driven and
the final project was freshly generated as `DirtDrifter`.

The first feedback images kept the toy-room language—toy blocks, a rubber
duck, barriers, dust, and toy cars—but the later Concept still varied in
platform targets, AI/navigation requirements, camera language, and technical
recommendations. The experiment therefore supports a visual reference as
appearance authority, but not as a replacement for semantic authority.

## Current conclusion

The correct next boundary is:

```text
Concept = semantic truth
image board = visual authority
Step 3 = Concept + board -> existing schema asset hints
Steps 4–7 = deterministic resolution, layout, generation, and validation
```

The image should help select and reject models, not silently redefine the
game. The next experiment should compare Concept-only, Concept-plus-board,
and Concept-plus-board-plus-deterministic image matching using the same
catalog snapshot and recorded lineage.

## Verification

The CLI/reference-image tests passed during the experiment:

```text
4 passed in 0.38s
```

`git diff --check` also passed for the implementation changes.
