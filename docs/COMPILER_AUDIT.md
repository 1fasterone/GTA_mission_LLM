# Compiler Audit

A release-state read of the pipeline: what a live pass over `src/`, `tests/`, `docs/`,
`config.yaml` and `requirements.txt` found weakest, and which real-world techniques close
each gap. Companion to `CLAUDE.md`'s architecture — read that first for the Two Roles /
Pipeline Contract this doc assumes throughout.

Method: a static pass (not a live pipeline run), cross-checked across three independent
research passes, with the five lowest-difficulty fixes below actually implemented and
tested in the same session this doc was written. Treat file:line references as of
2026-09-01 and expect drift as the code moves — this is a snapshot, not a contract.

**Status key:** ✅ fixed and tested · 🟡 partially addressed (see notes) · ⬜ open, not
attempted.

---

## What's Already Solid

Worth knowing before the gaps below, so they read as precise rather than sweeping:

- **Real semantic search, already.** RAG corpus retrieval runs true embedding ANN search —
  ChromaDB HNSW over cosine distance, fastembed's ONNX build of `bge-small-en-v1.5` — with
  per-kind calibrated acceptance thresholds, not a keyword fallback.
  (`rag/query.py:109-136`)
- **A real spline, not waypoints.** Racetrack generation runs a genuine centripetal
  Catmull-Rom spline through arc-length resampling to signed curvature and banking — the
  textbook technique, correctly implemented. (`src/unity_trackgen.py`)
- **Hallucination-safe by construction.** Every LLM-authored value routes to one of three
  tiers — grounded, free vocabulary, or load-bearing-and-rejected — so an invented
  identifier can cost a word, never a compile error. (`docs/LLM_ENRICHMENT_STRATEGY.md §2-4`)
- **No bare primitives, and it's checked.** All seven shipped mesh families are edge-loop
  sculpted or ring-lofted, each gated by a headless-Blender build that asserts a manifold
  mesh before it ships. (`docs/PROCEDURAL_BASES.md`)
- **A compiler that compiles the output.** `unity_cli.py verify` launches the real Unity
  Editor in batchmode and greps the authoritative compile log — not a structural stand-in
  for "does it build."

---

## Where It's Weakest

Tier 1 hides or produces wrong output. Tier 2 is a working pattern the codebase already
trusts elsewhere, just not applied here. Tier 3 is polish the code already names as
unfinished in its own comments.

### TIER 1 — Structural

#### Gap A — Requested, Not Enforced · 🟡 partially addressed
**Verdict:** PROMPT-AND-REPAIR → now optionally CONSTRAINED (json_object mode, opt-in)

Step 3 asked the model for JSON in prose and repaired whatever came back: `_build_payload`
(`src/lm_studio_client.py`) sent `model`/`messages`/`temperature`/`max_tokens`/
`reasoning_effort`/`seed` — never `response_format`. What came back was pulled apart by a
string-aware brace scanner and three sequential repair passes (`extract_json`,
`_sanitize_json_scalars`, `_repair_truncated_json`, `_drop_surplus_closers`). This wasn't
hypothetical — a prior incident ("JSON truncated and could not be repaired") traced straight
to this architecture.

**Fixed (partially):** `LMStudioClient.chat()` now accepts a per-call `json_mode: bool`
parameter that adds `response_format: {"type": "json_object"}` — requesting the server
enforce valid JSON *syntax* at generation time. Gated behind a new config flag,
`lm_studio.structured_output` (default `false`), read only at the Step 3 schema-fill call
site (`src/unity_pipeline.py`) — never the concept-generation call, which expects prose.
If the server/model rejects the parameter, `chat()` retries once without it, transparently,
inside the client itself — not as an extra `SCHEMA_FILL_ATTEMPTS` iteration, since the
pipeline's retry loop only catches `(ValueError, ValidationError)`, not the `RuntimeError`
an HTTP rejection raises; treating it as a normal retry-loop attempt would have crashed the
whole run uncaught on any model/server that rejects the parameter.

**Fixed (follow-up, same day):** two more pieces closed most of the remaining gap.

1. `LMStudioClient.chat()` now also reads `finish_reason` off the API response and stashes it
   as `self.last_finish_reason` — "length" is the SERVER's own confirmation the reply was cut
   off at `max_tokens`, not an inference from a downstream `JSONDecodeError`. `extract_json`'s
   truncation error now says "CONFIRMED" and names the real cause when this fired, instead of
   always guessing. Zero call-site changes needed anywhere (`src/lm_studio_client.py`).
2. Real `json_schema`-constrained decoding shipped as a second, separate opt-in:
   `src/unity_schema_jsonschema.py` derives the full `response_format` payload from
   `UnitySchema.model_json_schema()` (confirmed live: 37 top-level properties, 46 `$defs`,
   `$ref`-based, ~25.9KB serialized; `meta`'s untyped `dict` already renders as an open object —
   `{"type":"object","additionalProperties":true}` — with no post-processing needed). Sent with
   `"strict": false` deliberately: the actual runtime is LM Studio's local llama.cpp grammar
   converter, not OpenAI's strict-mode validator, and it accepts the looser shape. New config key
   `lm_studio.structured_output_mode: off | json_object | json_schema` (tri-state successor to
   the bool above; absent falls back to the bool unchanged — additive, non-breaking). Wired at
   the same Step 3 call site, same one-retry-then-fall-back-to-prose contract as `json_object`.

**Live-verified against the loaded model, corrected** (`qwythos-9b-claude-mythos-5-1m-mtp`, LM
Studio, 2026-09-01/02) — two rounds, because the first round was wrong in a way worth recording.

*Round 1* used a short, deliberately-minimal probe prompt ("Fill the schema minimally") at
300-1200 `max_tokens`, not the real Step 3 system prompt. It showed the server accepting the
full 46-`$def` schema and the output validating against `UnitySchema` with no repair-pass
involvement, and reported a ~1.8-3.5x latency cost on a short completion. That result is real but
was measured on the wrong axis — it never exercised the real system prompt's request for rich
content, so it couldn't have caught what round 2 found. (A `unity-create` run attempting to
re-measure at the real 50k-token budget also landed in a shared repo checkout that a concurrent
session had switched to a different branch mid-flight — the run silently executed old code with
none of this gap's fixes. Re-run from an isolated `git worktree` pinned to this branch's commit,
confirmed by inspecting the checked-out source before trusting the result.)

*Round 2*, in that isolated worktree, ran the actual `unity-create` CLI against the real Step 3
prompt (`UNITY_SCHEMA_FILL_SYSTEM`, full multimodal block, full brief) at `max_tokens: 50000`,
`json_schema` vs. no-`response_format`, 3 `json_schema` trials + 2 baseline trials:

- The server still **accepts** the schema every time — no rejection, no fallback firing.
- **All 3 `json_schema` trials came back with `entities: []`** — byte-identical section counts
  each time (`0 packages 0 scripts 2 scenes 0 features` at the raw Step-3-fill point). The
  deterministic pipeline stages after Step 3 backfill *some* structure on top (packages, scripts,
  one mission, one character — none of that is LLM content, it's scaffolding), but nothing
  downstream invents entities (correctly, per this repo's no-generic-defaults doctrine), so the
  brief's actual content — the five crystals, the two sentries — never existed. The playability
  gate correctly caught this every time: `"nothing to do — no collectibles, no enemies, no
  missions and no story beats"`, and `unity-create` exited non-zero.
- Both baseline trials (no `response_format`) filled `entities` normally and passed the
  playability gate at 95/100.
- `json_schema` trials also ran *faster* than baseline (43-48s vs. 83-98s total pipeline
  wall-clock) — the opposite of round 1's "slower" finding, because there's far less content to
  generate when the largest, most deeply-nested list in the schema comes back empty.

**Conclusion: do not recommend `json_schema` mode.** This is not a latency tradeoff, it's a
reproducible content-loss regression, specifically on `entities` — the one field the schema's
grammar represents as a list of the largest, most deeply-`$ref`-nested object (`EntitySpec`).
The likely mechanism: llama.cpp's grammar-masked sampling has to choose, at the start of each list
element, between the trivial "close the array" token and the much larger sub-grammar for one more
`EntitySpec`, and at `schema_temperature: 0.2` the model consistently takes the cheap exit.
`json_object` mode (syntax-only, no shape grammar) does not have this failure mode and remains the
safe, tested default. `structured_output_mode: json_schema` stays wired and available (config
default is still `off`, unaffected) as a documented, *not-recommended* experimental setting for
whoever wants to root-cause this — e.g. testing a higher `schema_temperature` under `json_schema`
specifically, or a schema variant that inlines `minItems` on `entities` — rather than removed
outright, since the plumbing itself (payload shape, fallback-on-rejection, `finish_reason`
capture) is correct and worth keeping for that follow-up work.

**Still open:** root-causing and fixing the `entities`-empty failure mode above; the same live
check against OpenRouter/Ollama (`LMStudioClient` serves all three identically, but the
fallback-on-rejection path has only been exercised by a live *acceptance*, never a live
rejection, against any of the three).

#### Gap B — Step 3 Never Sees the Brief · ✅ fixed
**Verdict:** INFORMATION LOSS

The schema-fill prompt interpolated only `{concept}` — the LLM's own prose paraphrase of the
user's brief — never the brief itself. The project's own A/B study measured the cost per
brief section in the production path:

| Brief section | Rescue layer? | Recall in production (LLM arm) |
|---|---|---|
| Entities | yes | 9/9 + 1 invented |
| Scripts | yes | 12/12 + 1 invented |
| Scene names | none | 1/5 kept (4 invented) |
| Audio cue ids | none | 3/10 kept (7 invented) |
| Narrative beats | none | 3/6 |
| Project title | none | 0/1 — fully replaced |

The two rescued rows prove the model wasn't the limiting factor — a deterministic re-parse
of the brief already patches recall back to near-total where it exists. The fix is not
"remove the LLM": a deterministic-only run of the same brief keeps 100% of every name but
composes *zero* mission objectives, because the brief's progression lives in prose the
parser can't interpret. Identity and meaning are different recall problems.

**Fixed:** `brief_block(user_prompt)` (`src/unity_prompts.py`) threads the raw brief into
`UNITY_SCHEMA_FILL_USER` immediately after `{concept}` — reading as a direct correction to
the paraphrase above it — capped at `BRIEF_CAP_CHARS` (4000 chars, logged if truncated) to
stay inside a local model's 8k-16k context window alongside the ~3.5k-token schema prompt
already there. The rescue injectors (`unity_concept_entities.py`, `unity_concept_scripts.py`)
remain a safety net rather than the primary recall mechanism.

**Not done this pass:** extending the rescue-layer pattern itself to scenes/audio/narrative/
title (the project's own §5 ranks this "highest-value" separately from feeding the brief) —
still open, see the solutions table.

#### Gap C — QA Stops at the Compiler · ⬜ open
**Verdict:** STRUCTURAL-ONLY

Automated testing is genuinely strong where it exists: a pytest smoke suite, a playability
evaluator with per-case score floors *locked* in version control, and a real Unity batchmode
compile gate that greps the authoritative editor log. None of it looks at a rendered pixel,
and none of it plays the game. `tests/test_blender_render.py` asserts `parts > 0` and
`nonmanifold_or_invalid == []` — no image is ever produced or compared, despite the
filename; worth flagging so a skim of the test list doesn't over-credit coverage that isn't
there. Whether a generated mission or scene is actually *playable* — the blip appears, every
ped spawns, the objective completes — is a ten-minute human checklist per generation, for
both backends. `com.unity.test-framework` (Unity's own PlayMode automation) already sits in
the vendored package cache, unused for this.

#### Gap D — Reachability Without a Path · ✅ fixed
**Verdict:** HEURISTIC → GRID/GRAPH BFS

Step 7's "is the content reachable" check was axis-aligned bounding-box containment, not a
graph walk:
```python
if not (xmin <= p[0] <= xmax and zmin <= p[2] <= zmax):
    stranded.append(e.name)
```
Content inside the box but behind a wall read as reachable. The kinematic variant was closer
but still one-dimensional — sorted hand-hold Y-coordinates, walked bottom-up, broke at the
first gap past a fixed reach constant, not a BFS/A*/NavMesh query over the level graph, so two
holds sharing a height read as "connected" even sitting on opposite sides of the wall with
nothing joining them.

**Fixed:** `src/unity_reachability.py` (new module) replaces both with a real graph walk, still
schema-only — Step 7 grades a schema before real geometry exists, so this stays a deliberate
approximation, not a NavMesh:
- `flood_fill_reachable()` — a 2-D occupancy grid over `terrain_bounds`, blocked wherever a
  `wall`/`building`/`obstacle`/`rock` instance's footprint lands (approximated from `scale`,
  the same way `src/unity_layout.py._footprint_radius` sizes an unmeasured "single" placement),
  BFS'd out from the player's own spawn cell. `_check_content_reachable` runs this only for
  `ground_plane`/`route_corridor` primitives (a flying/swimming/climbing game's content sits at
  an altitude a ground wall was never meant to block) and only after the pre-existing bounding
  box test, which stays authoritative for "over the void" — a flood fill bounded by
  `terrain_bounds` cannot itself tell a point outside those bounds from one at the edge. The
  player's own spawn gets a small cleared disk regardless of what's blocked nearby, so a
  blocker sitting AT spawn (`_check_spawn_clear`'s finding to make) can't also cage the flood
  fill before it starts; blocking-kind entities are never tested as reachability TARGETS
  themselves (a wall is the barrier, not content needing a path to it — a thick enough wall
  reads every cell several rows deep as "no open neighbour", which is correct for the barrier
  and meaningless as a finding about it).
- `wall_climb_reachable()` — the same idea for the climbing-wall archetype: a real graph over
  the baked hold instances, edges wherever two holds sit within the layout's own reach constant
  in full 3-D (not just Y), BFS'd from a virtual ground node. Replaces `_climb_ceiling`'s
  sorted-Y walk in `_check_objective_kinematics`'s `WALL_SURFACE` branch.

This closes the gap the packer's own non-overlap guarantee (`_pack_groups`) mostly already
prevented for procedurally-packed scenes — nothing legitimately ends up geometrically inside a
solid group's circle there. It matters for content whose positions were baked outside that
packer, most concretely a hand-authored Scene Compiler layout
(`src/unity_sceneinput.py`/`unity_scenebind.py` bakes `instance_positions` straight from a 3-D
editor, walls included, with no circle-packing guarantee at all). New tests:
`tests/test_unity_reachability.py` (the grid/graph algorithms directly, including the
horizontally-displaced-same-height-holds regression the old sorted-Y walk could not see) and
two new cases in `tests/test_unity_playability.py` (content behind a full-span wall vs. on the
spawn side of it).

### TIER 2 — Consistency

#### Gap E — The Embeddings Are Already Installed · ✅ fixed (the audio-dispatch half)
**Verdict:** KEYWORD/SUBSTRING

Model/asset resolution and scene binding match by weighted token overlap and substring
containment — while the exact same codebase runs real embedding search for the RAG corpus,
two files away, on dependencies already in `requirements.txt`. No fuzzy/edit-distance
library exists anywhere in `src/`. The prompt/theme classifier (`unity_archetypes.py`)
already learned this lesson once: an earlier plain-substring check let `"vive"` match inside
`"survive"`, silently flipping a 3D survival prompt to VR — patched with a
`\b`-bounded, `lru_cache`-memoized regex (`_kw_pattern`). The structurally identical audio-cue
dispatcher had the same unguarded shape, unpatched, in **7 places** — not just the one
function initially suspected.

**Fixed:** `src/audio/synth.py` now mirrors `unity_archetypes.py`'s proven fix — a local
`_kw_pattern`/`_any_kw_match` pair (word-boundary regex, `s?` tolerant of simple plurals so
"footstep" still matches "footsteps" without reopening the "campfire"/"fire" collision the
left-boundary anchor blocks) — applied to `keyword_archetype()` and all six descriptor
checks in `archetype_params()`. New regression tests lock in both the collision fix
("campfire" no longer false-matches "fire") and plural tolerance.

**Still open:** reusing `chromadb`/`fastembed` for model/asset resolution
(`unity_entity_asset_resolver.py`) and scene binding (`unity_scenebind.py`) themselves — a
bigger, determinism-sensitive change (the index build must stay offline/deterministic, and
any embedding score should blend with, not replace, the existing deterministic weights) —
not attempted this pass. See the solutions table.

#### Gap F — Built by Hand, Not by the Plan · ⬜ open
**Verdict:** DESIGNED, NOT WIRED

All seven shipped mesh families are sculpted through a hand-written `bmesh` toolkit — good
work, gated by real manifold tests — but the project's own architecture note
(`docs/model-pipeline-architecture.md`) already says this is the wrong tool for organic
shapes:

> "Asking an LLM to procedurally author organic geometry produces primitive soup —
> plausible structure with no authentic surface, because the model reasons about geometry
> blind: no viewport feedback, no depth."

That document already specs the fix — route mechanical parts to parametric CAD
(CADQuery/build123d), route organic parts to a generative-mesh API behind a frozen-artifact
determinism boundary — and marks it `Status: draft` / `Open work`. Neither appears in
`requirements.txt`. One naming trap for whoever picks this up: `job_schema.py`'s
`geometry_nodes` field is a dispatch string, not Blender's actual Geometry Nodes system —
nothing in `src/` touches `bpy.data.node_groups`.

### TIER 3 — Named, Not Done

#### Gap G — Not Actually Poisson-Disk · ✅ fixed
**Verdict:** SELF-DOCUMENTED

Within-group scatter placement used a jittered grid — nearest-N grid cells plus bounded
random jitter — and the function's own documentation already stated it wasn't true
Poisson-disk sampling.

**Fixed:** `_scatter_positions()` (`src/unity_layout.py`) now runs a grid-accelerated
Bridson's algorithm — a real minimum pairwise distance, not an approximation eroded by
jitter — then keeps the `count` samples closest to center (trimming a valid Poisson-disk set
can only preserve or increase the remaining minimum distance, never decrease it, so the
guarantee survives the trim). Same seeding contract as before (`random.Random(f"{name}:
{count}:{spacing:.3f}")`); active-list/grid bookkeeping uses plain lists throughout — never
dict/set iteration order — to keep the determinism tests byte-identical across runs. A
side benefit: using the model's real `footprint` directly as the packing radius (when it
exceeds `eff_spacing`) fixes a latent overlap the old grid could still produce in that case,
not just reproduces it. `tests/fixtures/arenaplane_baseline.json`'s recorded scatter
positions were regenerated to match (only the two scatter-placed entities' coordinates
changed; names, order, `world_spec`, and `terrain_bounds` did not).

#### Gap H — A Spline With No Hills · 🟡 partially addressed
**Verdict:** SELF-DOCUMENTED, plus a consumer-side blocker the original audit missed

The track generator's centripetal Catmull-Rom spline is genuinely well built — elevation
was flat, and the file's own comments already named a Perlin-noise elevation profile as the
planned next step. **This gap was initially rated low-difficulty by this audit; that rating
was wrong.** Tracing the only consumer, `src/unity_racetrack.py`, found the road surface
rides the centerline's `y` directly (fine), but the finish-line marker and the start grid
both hardcode `y=0.0` independent of the centerline — and `src/unity_terraingen.py` (the
only other elevation system in the repo) is a separate, unrelated world-scale heightmap
generator with no linkage to a track centerline. Shipping real elevation as originally
scoped would spawn karts floating over or embedded in an elevated track.

**Fixed (scoped down):** `unity_trackgen.py` now has a tested elevation *primitive* —
deterministic closed-loop value noise (seeded keyframe heights, cosine-interpolated,
amplitude capped as a fraction of track length) threaded through
`_build_centerline`/`generate_centerline`/`generate_track_route` via a new
`elevation_amplitude` parameter. **Default is `0.0`, which reproduces today's exact flat `y`
output byte-for-byte** — every existing caller, including `unity_racetrack.py`, is
unaffected. `unity_racetrack.py` itself was deliberately left untouched.

**Still open:** wiring non-zero elevation into `unity_racetrack.py` requires also fixing the
start-grid/finish-line hardcode and resolving the ground-plane question the original comment
raised — real follow-up work, not attempted this pass.

#### Gap I — A Search Engine That Can't Read Sentences · ⬜ open
**Verdict:** THIRD-PARTY LIMIT

Sketchfab's own search is literal keyword match — verified live that a descriptive style
sentence returns zero results while a two-word label returns dozens, which is why prompts
are capped at four words before they're sent. The mitigations already in place
(author-affinity biasing, a concept-translator that shortens queries) are sound engineering
around a real external constraint, not an oversight — though nothing reranks the candidates
that come back using the embedding stack already installed for the RAG corpus.

---

## The Fixes, Rated

Difficulty/impact: ●○○ low · ●●○ medium · ●●● high. Day-one: ✓ should've shipped from the
start · △ defensible v1, natural v2 · — deliberate later-stage work.

| # | Solution | Fixes | Difficulty | Impact | Replaces | Day one? | Status |
|---|---|---|---|---|---|---|---|
| 1 | `json_object`-mode structured output | Gap A | ●○○ | ●●● | repair pipeline → fallback | ✓ | ✅ shipped (opt-in) |
| 1b | `finish_reason`-confirmed truncation errors | Gap A | ●○○ | ●●○ | inferred-from-JSONDecodeError guess | ✓ | ✅ shipped |
| 1c | `json_schema`-mode structured output (real shape) | Gap A | ●●○ | ●●● | `json_object` (syntax only) | △ | 🟡 shipped off-by-default; live-verified NOT recommended (empties `entities`, see Gap A) |
| 2 | Feed Step 3 the raw brief | Gap B | ●○○ | ●●● | — (additive) | ✓ | ✅ shipped |
| 3 | Extend rescue layers to scenes/audio/narrative/title | Gap B | ●●○ | ●●● | — (mirrors existing pattern) | △ | ⬜ open |
| 4 | Automated render + perceptual diff | Gap C | ●●○ | ●●● | — (additive to manifold tests) | △ | ⬜ open |
| 5 | Unity PlayMode automated E2E smoke | Gap C | ●●● | ●●● | manual checklist → CI gate | △ | ⬜ open |
| 6 | Grid/graph BFS reachability | Gap D | ●●○ | ●●○ | AABB + 1-D reach-gap walk | △ | ✅ shipped |
| 7 | Reuse chromadb+fastembed for model resolution & scene binding | Gap E | ●●○ | ●●○ | weighted-token scorer / substring binder | △ | ⬜ open |
| 8 | Mirror the `\b`-bounded regex guard | Gap E | ●○○ | ●●○ | unguarded `any(w in p for w in words)` | ✓ | ✅ shipped |
| 9 | Parametric CAD for mechanical parts | Gap F | ●●○ | ●●● | mechanical-part share of the bmesh toolkit | ✓ | ⬜ open |
| 10 | Generative-mesh APIs for organic parts | Gap F | ●●● | ●●● | — (sits beside the sculpted toolkit) | — | ⬜ open |
| 11 | Poisson-disk sampling (Bridson's algorithm) | Gap G | ●○○→●●○* | ●●○ | `_scatter_positions()` jittered grid | ✓ | ✅ shipped |
| 12 | Value/Perlin-noise elevation profile | Gap H | ●○○ | ●○○ | — (additive; elevation was flat) | ✓ | 🟡 primitive shipped, not wired |
| 13 | Local embedding rerank of Sketchfab candidates | Gap I | ●●○ | ●○○ | — (layers on existing author-affinity biasing) | — | ⬜ open |

\* Rated low-difficulty in the original pass; implementing a real grid-accelerated Bridson
sampler (~100 lines) turned out to be the second-most-involved fix in this batch, not the
third. Recorded here so the next difficulty estimate on this table starts from the
corrected number, not the optimistic one.

---

## Changelog

- **2026-09-01** — Fixes #1, #2, #8, #11 shipped in full; #12 shipped as a tested,
  default-off primitive (not wired into `unity_racetrack.py` — see Gap H). All five
  verified with new/updated tests; full regression suite run clean except seven pre-existing
  `tests/test_gta_retrieval.py` failures (RAG embedding-distance thresholds, confirmed via
  `git stash` to exist independent of this session's changes — out of scope here).
- **2026-09-01 (follow-up)** — Fix #6 (Gap D, grid/graph BFS reachability) shipped: new
  `src/unity_reachability.py`, wired into `_check_content_reachable` and
  `_check_objective_kinematics`'s `WALL_SURFACE` branch in `src/unity_playability.py`. New
  `tests/test_unity_reachability.py` plus two new cases in `tests/test_unity_playability.py`.
- **2026-09-01 (follow-up 2)** — Fixes #1b and #1c (Gap A, remaining half) shipped:
  `finish_reason` surfaced from the API response onto `LMStudioClient.last_finish_reason`
  (confirmed-vs-guessed truncation errors), and real `json_schema`-constrained decoding
  (`src/unity_schema_jsonschema.py`, config key `lm_studio.structured_output_mode`). Both
  initially live-verified against the loaded model (`qwythos-9b-claude-mythos-5-1m-mtp`) with a
  short probe prompt: accepted, output validates against `UnitySchema` with no repair-pass
  involvement, ~1.8-3.5x latency on a short completion vs. no `response_format`. New tests:
  `TestJsonSchemaMode`/`TestFinishReason` in `tests/test_lm_studio_client.py`,
  `tests/test_unity_schema_jsonschema.py`. Full suite: 4026 passed, same 7 pre-existing
  `tests/test_gta_retrieval.py` failures as the prior pass (unrelated, confirmed pre-existing).
- **2026-09-02 (correction)** — the follow-up's `json_schema` latency finding was measured on
  the wrong prompt (a short minimal-fill probe, not the real Step 3 system prompt) and its
  planned re-measurement at full `max_tokens` first ran in a repo checkout a *concurrent* Claude
  Code session had switched to a different branch mid-flight, silently executing code without
  this gap's fixes. Re-run from an isolated `git worktree` pinned to this branch's commit, against
  the real `unity-create` CLI and the real Step 3 prompt at `max_tokens: 50000`: **3/3 `json_schema`
  trials came back with `entities: []`** (the brief's actual collectibles/enemies), failing the
  playability gate every time, while 2/2 baseline trials filled entities normally and passed at
  95/100. This is a reproducible content-loss regression specific to `entities` — the schema's
  largest, most deeply-`$ref`-nested list — not the latency tradeoff originally reported (the
  `json_schema` trials ran *faster* than baseline, consistent with generating far less content).
  **`json_schema` mode is NOT recommended** — see Gap A for the corrected writeup and the
  suspected mechanism. `structured_output_mode` stays wired (default `off`, unaffected) as a
  documented experimental setting for whoever root-causes this, not removed.
