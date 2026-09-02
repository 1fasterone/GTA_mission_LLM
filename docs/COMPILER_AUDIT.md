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

**Still open:** this is `json_object` mode only — it guarantees syntactically valid JSON,
not schema-shaped JSON. Full `json_schema`-constrained decoding (actually encoding
`UnitySchema`'s ~30 top-level fields / ~40 nested models / untyped `meta: dict` /
`Literal`-heavy shape) remains future work — the schema is large enough, and untyped enough
in places, that it needs its own design pass rather than a quick-win treatment. The
`structured_output` flag also stays untested against a live model in this session — flip it
on and run a real `unity-create` before trusting it for production use.

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

#### Gap D — Reachability Without a Path · ⬜ open
**Verdict:** HEURISTIC

Step 7's "is the content reachable" check is axis-aligned bounding-box containment, not a
graph walk:
```python
if not (xmin <= p[0] <= xmax and zmin <= p[2] <= zmax):
    stranded.append(e.name)
```
Content inside the box but behind a wall reads as reachable. The kinematic variant is closer
but still one-dimensional — sorts hand-hold Y-coordinates, walks bottom-up, breaks at the
first gap past a fixed reach constant, not a BFS/A*/NavMesh query over the level graph.
Given Step 7 grades a schema before real geometry exists, bounding-box was a defensible v1 —
it just hasn't had a v2 yet.

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
| 2 | Feed Step 3 the raw brief | Gap B | ●○○ | ●●● | — (additive) | ✓ | ✅ shipped |
| 3 | Extend rescue layers to scenes/audio/narrative/title | Gap B | ●●○ | ●●● | — (mirrors existing pattern) | △ | ⬜ open |
| 4 | Automated render + perceptual diff | Gap C | ●●○ | ●●● | — (additive to manifold tests) | △ | ⬜ open |
| 5 | Unity PlayMode automated E2E smoke | Gap C | ●●● | ●●● | manual checklist → CI gate | △ | ⬜ open |
| 6 | Grid/graph BFS reachability | Gap D | ●●○ | ●●○ | AABB + 1-D reach-gap walk | △ | ⬜ open |
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
