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

**Root cause, found (2026-09-02, same day as the correction above).** Not a sampling-temperature
effect — a probe harness that reused one fixed concept and swapped only `response_format` (fast
iteration against the live server, bypassing the ~90s full-pipeline round-trip per attempt) showed
every "empty" response had `finish_reason: stop`: the model wasn't cut off, it *chose* to stop.
Inspecting the actual key order in three successful-but-sparse responses showed why: llama.cpp's
grammar for an all-optional object enforces Pydantic's declaration order as a single monotonic
pass, and `entities` sits 20th of 37 properties, inside an unbroken run of 16 consecutive
all-optional "legacy" fields (`company_name` … `entities` … `environment`). The model consistently
jumped that whole run and landed on the multimodal sections its own prompt names explicitly
(`characters`, `missions`, `audio_specs`, …) — a real choice under the grammar's freedom to skip
any optional key, not a truncation artifact.

The fix needed BOTH halves, confirmed by testing each alone: `minItems: 1` on `entities` alone did
nothing — the model kept omitting the KEY entirely, and an absent optional key never reaches its
own `minItems` constraint. Only adding `entities` to the schema's top-level `required` list (so
the key can't be skipped) *together with* `minItems: 1` (so the now-mandatory array can't be
satisfied with `[]`) produced real content — 3/3 direct-probe trials came back with on-brief
entities (a 5-count crystal pickup group, a 2-count sentry enemy group, matching the test brief's
"five crystals"/"two sentries" exactly), and a follow-up 3-trial run of the actual `unity-create`
CLI end-to-end got 2/3 full playability passes at 95/100 (the third hit a real, different,
*unrelated* failure below).

**Fixed:** `src/unity_schema_jsonschema.py`'s `_harden_for_generation()` applies both changes to a
deep-copied schema before it reaches `response_format` — deep-copied because Pydantic does not
document `model_json_schema()`'s return value as safe to mutate in place, and other code in this
repo calls the same method for unrelated purposes; a shared-dict mutation bug here would have been
very hard to trace. Scoped to `entities` only: `scripts` shows the identical skip pattern in the
same probes but was never the cause of a playability-gate failure (deterministic pipeline stages
backfill scripts regardless of what Step 3 wrote), so it stays untouched pending its own live
evidence of actually mattering, rather than a preemptive guess.

**A second, different failure surfaced once entities stopped being empty**, worth naming so it
isn't mistaken for the same bug: one of the three end-to-end `unity-create` trials had the model
fill `entities` with reef/coral/ocean-themed content for a brief about an "overgrown island" that
never mentioned water, and `src/unity_layout_archetype.py`'s strict medium classifier correctly
refused to lay out a `planar`+`water` combination no primitive covers — exactly the
abstain-loudly behavior that guard exists for. This is a content-fidelity question (does
`json_schema` mode's constrained sampling degrade the model's topical adherence, separate from the
emptiness bug just fixed), not a grammar-mechanics bug, and is not fixed by this pass — flagged
below as still open rather than folded into a claim this pass didn't earn.

`json_object` mode remains the safe, unconditionally-recommended default. `json_schema` mode's
specific, diagnosed emptiness regression is fixed and live-verified; it is not yet claimed fully
production-ready given the reef/coral finding above, so it stays opt-in (config default `off`)
pending more end-to-end trials.

**Still open:** whether `json_schema` mode's constrained sampling measurably increases off-brief
content vs. `json_object`/free-form (the reef/coral finding is n=1, not yet a pattern); the same
live check against OpenRouter/Ollama (`LMStudioClient` serves all three identically, but the
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

**Fixed (solution #3, follow-up):** the rescue-layer pattern itself now extends to
SCENES/AUDIO/NARRATIVE/TITLE — the project's own §5 had ranked this "highest-value"
separately from feeding the brief. Four new sibling modules
(`src/unity_concept_scenes.py`, `_narrative.py`, `_audio.py`, `_title.py`) mirror
`unity_concept_entities.py` / `unity_concept_scripts.py` exactly: a deterministic regex
parse of `schema.meta['user_prompt']`, a Step-3 checklist, and an append-only
`inject_missing_concept_*` fixer wired into Step 5 (`src/unity_pipeline.py`) and into the
eval replay (`src/unity_eval.py`). The low-level section/list-folding primitives those four
modules and `src/unity_briefparse.py` all need (`_section`, `_scalar`, `_fold_numbered`,
`_to_pascal`, the `_HEADERS` vocabulary, ...) were extracted verbatim into a new
`src/unity_brief_sections.py` rather than tripled — `unity_briefparse.py`'s own
`_read_scenes`/`_read_story_beats`/`_read_audio`/`_read_title` are gone, replaced by calls
into the same four new modules, so the `--no-llm` control arm and the LLM-arm rescue can no
longer drift apart (confirmed byte-identical against `prompts/coldwater_circuit.txt`'s
recorded numbers: title `ColdwaterCircuit`, 5 scenes, 9 entities, 12 scripts, 6 beats, 10
audio cues — see `tests/test_unity_briefparse.py`, new).

TITLE is the one asymmetric case in the family: `schema.project_name` is a scalar, so its
rescue is override-if-wrong rather than append, gated by two confidence tiers
(`src/unity_concept_title.py`) — an explicit `TITLE:` declaration is always trusted; absent
that, the brief must carry ≥2 other recognised section headers before the "first line is the
title" house convention is trusted to override an LLM's own invention. Neither tier met →
abstain, so an unstructured one-line vibe prompt can never be forced into a mechanically-bad
title. New tests: `tests/test_unity_concept_scenes.py`, `_narrative.py`, `_audio.py`,
`_title.py` (53 cases). Full suite: 4134 passed, 1 skipped, same one pre-existing unrelated
`tests/test_outputs_smoke.py` failure (confirmed via `git stash` to predate this change).

#### Gap C — QA Stops at the Compiler · ✅ fixed
**Verdict:** STRUCTURAL-ONLY → now also LIVE (Unity side) and checks a rendered pixel

Automated testing is genuinely strong where it exists: a pytest smoke suite, a playability
evaluator with per-case score floors *locked* in version control, and a real Unity batchmode
compile gate that greps the authoritative editor log. Historically none of it looked at a
rendered pixel, and none of it played the game. `tests/test_blender_render.py` asserts
`parts > 0` and `nonmanifold_or_invalid == []` — no image is ever produced or compared,
despite the filename; still true, still worth flagging so a skim of the test list doesn't
over-credit coverage that isn't there.

**Fixed (the "plays the game" half — solution #5):** `src/unity_playtest.py` +
`templates/unity/editor/PlayModeSmoke.{cs,Tests.asmdef}` drive `com.unity.test-framework`
(Unity's own PlayMode automation — confirmed vendored in the package cache, previously
unused) through a real batchmode `-runTests -testPlatform PlayMode` run: enter Play, load the
generated main scene via the runtime `SceneManager` (never `EditorSceneManager` — sidesteps
`EntitySpawner`'s `[ExecuteAlways]`/`_baked` edit-mode guard entirely), and assert — generically,
with no per-genre or per-class knowledge — zero console errors/exceptions during play, exactly
one active `Player`-tagged object when the schema declares an avatar, and every entity group
spawned the expected instance count. That last check is the one a naive implementation gets
wrong: expected counts are derived from the same signals `EntitySpawner.cs.j2` itself gates
spawning on (`schema.meta["entity_model_paths"]`'s `skip:`/`fx:` routing, `SceneSpec.entities`
scene scoping, the 2D archetypes' `kind` exclusions, tiled path groups' runtime-computed
counts) — not from `entities[i].count` alone, which would false-fail on ordinary,
already-shipped generator output. `com.unity.test-framework`'s version is pinned to `1.1.24`
(what's actually vendored locally) rather than resolved through `unity_resolver`'s version
table, which currently points at `1.4.5`/`1.4.6` — using that would have silently required
network access on every first run. Opt-in: a new `unity_cli.py playtest <project>` command,
plus `e2e-verify --playtest` to run compile + static playability + live playtest in one pass
(skips the Play phase entirely when compile fails). Fast, no-Unity-launch unit tests cover the
expectations-derivation branches and NUnit XML parsing; a `unity_playtest`-marked opt-in test
mirrors `tests/test_unity_compile.py`'s real-Editor pattern.

**Fixed (the "does it look right" half — solution #4):** a fifth `[UnityTest]`,
`CaptureMainCameraScreenshot`, renders one real frame and hands it to
`src/unity_playtest.py`'s `analyze_render_health()` — structural pixel checks in the same
spirit as the manifold checks it sits beside, not a golden-reference diff (there is no fixed
"correct" image for an arbitrary LLM-generated project): a solid-black frame (nothing
rendered) or a frame dominated by Unity's magenta missing-shader color (a material/shader
failed to compile or load) both fail the gate; anything else passes. Deliberately scoped no
further than that — camera framing/composition ("is the shot interesting") is a different,
much fuzzier problem this doesn't attempt, matching the same boundary solution #5 already
drew around "boot-time defects, not aesthetic quality."

Getting a clean capture took five real attempts against a live Cinemachine+URP project,
each a genuine dead end worth recording (full detail in `PlayModeSmoke.cs`'s own comments):
`WaitForEndOfFrame` is never evoked in batchmode; reassigning the scene's own
Cinemachine-driven camera's `targetTexture` (three different ways) triggered a native
`QuaternionToEuler: Input quaternion was not normalized` and rendered 100% magenta regardless
of technique; `Instantiate()`-cloning that camera to strip the interference ran the clone's
`Awake()`/`OnEnable()` synchronously, before any code could disable the offending component
— too late by construction. The fix that actually worked: a genuinely fresh `GameObject` +
`Camera.CopyFrom()` (never cloned from anything Cinemachine touched) plus explicitly
attaching URP's `UniversalAdditionalCameraData` (reflection-only, `CopyFrom()` doesn't carry
it, and runtime `AddComponent<Camera>()` doesn't auto-attach it the way the Editor's camera
UI does).

That still rendered 100% magenta — which turned out not to be a capture-mechanism bug at
all, but a **real, previously-invisible generator bug** the new gate surfaced on its very
first genuinely clean run: `SetupRenderPipeline.cs.j2`'s first-open setup gate checked
`EditorPrefs` *before* the actual project state, and `EditorPrefs` is stored machine-globally,
keyed only by `project_name` — so regenerating a same-named project (an entirely normal
development workflow: tweak a prompt, run `unity-create` again) left every copy after the
first permanently unconfigured. `GraphicsSettings.defaultRenderPipeline` stayed null, every
URP-authored material rendered as Unity's magenta fallback, and nothing logged an error,
because a pipeline mismatch isn't one — invisible to every check in this repo until this one.
**Fixed:** the gate now checks `GraphicsSettings.defaultRenderPipeline` first, unconditionally,
and only consults `EditorPrefs` as a same-project optimization once that real state already
says "configured" — never as a standalone gate that can mask a genuinely-unconfigured project.
Live-verified end to end afterward: `unity_cli.py playtest` on a fresh `SkylineGlider`
generation — **PASS, compile=OK, playtest=OK, 9/9**, screenshot black=0% magenta=0%, a real
sky-toned frame, not a stand-in color.

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

#### Gap E — The Embeddings Are Already Installed · ✅ fixed (both halves)
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

**Fixed (remaining half):** model/asset resolution (`unity_entity_asset_resolver.py`) and
scene binding (`unity_scenebind.py`) now blend cosine similarity into their existing
deterministic keyword scorers — opt-in (`assets.embedding_select` / `scenebind.
embedding_select`, both default `false`), refinement-only, offline/deterministic. Deliberately
**not** `chromadb`, despite the audit's original wording: measured against the real code,
neither call site looks like a ChromaDB workload. The local catalog
(`lookups/unity/asset_catalog.json`) has 2953 Kenney entries — well under where this repo's
own RAG corpus (`rag/query.py`) says an approximate HNSW index starts earning its keep
(~20k). Authored scenes are tiny-N (5-11 entities against 1-4 groups) with text that's novel
every run — no stable corpus to index at all. A precomputed, L2-normalized embedding matrix
plus one vectorized matrix-vector cosine per resolve call (new `src/unity_semantic_match.py`,
cached to gitignored `lookups/unity/asset_catalog_embeddings.{npz,meta.json}`, built via
`unity-catalog --embed`) is simpler, sub-millisecond at this scale, and avoids stacking a
second nondeterminism surface (HNSW's approximate recall) on top of the one this tier already
protects (`_stable_seed`'s seeded-softmax variety rotation). Both tiers reuse
`unity_semantic_embed.text_similarity()` (PR #172) as the embedding primitive — zero new
dependencies.

Two blend points, both structurally outside every existing hard gate:
- **Catalog** (`unity_entity_asset_resolver._final_score`): the embedding term is added only
  inside the existing `if s > 0.0:` guard — i.e. only after the keyword scorer's own strong-hit
  gate already qualified the candidate. A zero-keyword-overlap entry can never be rescued by
  embedding similarity alone, at any weight (locked in by
  `TestEmbeddingRefinement::test_embedding_can_only_refine_never_qualify`, mocking
  similarity=1.0 for a keyword-disqualified aircraft model against a tree-type query and
  confirming it is still never picked).
- **Scene-bind** (`unity_scenebind.bind_score`): the term is added last, after the declared-role
  veto/bonus and all four keyword-overlap additions — the veto stays a hard `return 0.0` before
  the embedding term is ever reached (`test_conflicting_role_veto_survives_embedding_enabled`,
  similarity=1.0, weight=10.0, still exactly `0.0`). Unlike the catalog tier, `bind_score` sums
  terms directly against `MIN_BIND_SCORE` with no pre-gate — the one real risk this design
  carries, closed not by inspection but by a purpose-built calibration script
  (`tools/calibrate_scenebind_embedding.py`) that measures the exact
  `test_kind_alone_is_below_the_threshold` scenario live: a bare `kind="vehicle"` match against
  `group="Vehicle"/"vehicle.fbx"` scores `text_similarity=0.709`, giving a provable safe upper
  bound of `embedding_weight < (MIN_BIND_SCORE - _W_KIND) / 0.709 ≈ 1.41`. The shipped default
  (`0.5`) leaves ample margin (`bind_score` on that exact pair: `1.354`, still under
  `MIN_BIND_SCORE=2.0`) and, measured against a genuine positive control (zero keyword overlap,
  `text_similarity=0.652`), can only contribute `+0.33` at that weight — nowhere near enough to
  manufacture a bind from nothing.

`tools/calibrate_catalog_embedding.py` calibrated the catalog-tier default the same way:
genuinely-correct pairs (Trees/pine-tree, Crate/crate-*) measured `0.76-0.89`; the
cross-domain near-miss `_passes_kind_scope`'s own docstring names (ScoreBoard sharing "board"
with `skateboard.fbx`) measured `0.578` — separated from the good pairs, confirming that
specific bug stays closed by `_passes_kind_scope`'s structural filter rather than a score
threshold. Both `config.example.yaml` blocks cite these measured numbers directly, not
placeholders. New tests: `tests/test_unity_semantic_match.py` (21, the shared text
builders/cache/matrix machinery), plus embedding-refinement classes added to
`tests/test_entity_asset_resolver.py` (6) and `tests/test_unity_scenebind.py` (8), plus new
`tests/test_cli_unity_catalog.py` (4, the `--embed` flag). Every disabled-path test confirms
byte-identical output to today and that the embedder is never even loaded when the config key
is absent/false.

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

#### Gap I — A Search Engine That Can't Read Sentences · 🟡 partially addressed
**Verdict:** THIRD-PARTY LIMIT, corrected write-up

Sketchfab's own search is literal keyword match — verified live that a descriptive style
sentence returns zero results while a two-word label returns dozens, which is why prompts
are capped at four words before they're sent. The mitigations already in place
(author-affinity biasing, a concept-translator that shortens queries) are sound engineering
around a real external constraint, not an oversight.

**Correction to the original write-up:** the claim that "nothing reranks the candidates
that come back using the embedding stack already installed for the RAG corpus" was stale.
`unity_semantic_embed.semantic_filter()` / `semantic_filter_frozen()` (PR #172,
`src/unity_semantic_embed.py`) already do exactly that — a hard cosine-similarity floor that
can reject a candidate outright (`semantic_filter` can empty the pool, unlike the vision-LLM
reorder), AND a best-first reorder by image similarity
(`passed.sort(key=lambda t: (-t[0], -t[1]))`) — and the text axis (`text_similarity()`)
already reuses `rag/embedder.py`'s own BAAI/bge-small-en-v1.5 loader, i.e. genuinely IS "the
embedding stack already installed for the RAG corpus" (the image axis adds a separate CLIP
text/vision pair). Confirmed by reading the function body directly and by
`tests/test_unity_semantic_embed.py::test_semantic_filter_keeps_and_orders_by_image_similarity`,
which asserts order, not just membership. Solutions-table row #13 is updated below to match.

**The real, narrower gap that remained:** `semantic_filter()` / `semantic_filter_frozen()`
only ever scored the first `max_candidates` (hardcoded default: 6) entries of the
keyword-ranked list, while `SketchfabClient.search()` already fetches up to 24 raw
candidates per query at no extra network cost. Positions 7-24 were keyword-ranked but never
semantically reconsidered, no matter how good a conceptual match they were — the literal
mechanism behind this gap's own "search engine that can't read sentences" framing. None of
the four call sites that reach these functions (`cli/unity_assets.py`,
`unity_entity_asset_resolver.py`'s Tier S live-fetch AND its network-free cache-hit
re-check, `unity_sketchfab_collection.py`) ever overrode this default, and there was no
config key to do so. This was live and active for real usage: the shipped default already
had `sketchfab.semantic_select: true` enabled, so every live Sketchfab fetch was already
paying the semantic-scoring network cost for up to 6 candidates while structurally unable to
benefit from it beyond that.

**Fixed:** a new `sketchfab.semantic_pool_size` config key (default `6`, byte-identical to
today's behavior) now threads through to `max_candidates` at all four call sites. Also
fixed, found while touching the cache-hit call site: `unity_entity_asset_resolver.py`'s
`sk_scored` was never sorted by keyword score before being sliced to the pool width there —
"first N" meant "first N in catalog-scan order," not "top N keyword-ranked," unlike every
other call site — now sorted first, matching the rest.

`tools/calibrate_sketchfab_rerank_pool.py` — the first calibration script in this repo
needing the LIVE Sketchfab search API rather than local/synthetic data — first ran against 4
queries (2026-09-10; 1, "realistic paratrooper", returned no licensed results at all), which
showed only one marginal rescue case out of three measurable queries — too small a sample to
draw a real conclusion from, not evidence of a narrow effect. **Widened to 14 queries across
vehicle/weapon/character/structure/nature/urban-prop categories and re-run**: 9 of 13
measurable queries had their best semantic match sitting outside the top 6 — a real,
repeatable pattern, not noise. Margins varied meaningfully: `warehouse building` was
clearest (best match at rank #15, +0.027 over the in-pool best), several others were
meaningful (+0.014 to +0.015 at ranks #14-22: `street lamp`, `skateboard ramp`,
`shipping container`, `drone`), and several were near-noise (+0.001 to +0.009 at ranks
#7-19: `zombie`, `police car`, `pine tree`, `motorcycle`, `traffic cone`). `wooden crate`,
`medieval sword`, and `assault rifle` had their best match already inside the top 6.

**Still open, now a considered tradeoff rather than an evidence gap:** the default stays
`6`. A pool wide enough to catch most of the meaningful-margin rescues would need to reach
into the high teens or twenties, at 2.5x-4x today's per-query network/inference cost —
`unity_sketchfab_collection.py`'s batch path pays that once per brief in a loop. The
evidence now genuinely supports raising it for a project willing to spend the extra cost for
more accurate Sketchfab picks; the default was kept conservative deliberately, not because
the case is weak.

---

## The Fixes, Rated

Difficulty/impact: ●○○ low · ●●○ medium · ●●● high. Day-one: ✓ should've shipped from the
start · △ defensible v1, natural v2 · — deliberate later-stage work.

| # | Solution | Fixes | Difficulty | Impact | Replaces | Day one? | Status |
|---|---|---|---|---|---|---|---|
| 1 | `json_object`-mode structured output | Gap A | ●○○ | ●●● | repair pipeline → fallback | ✓ | ✅ shipped (opt-in) |
| 1b | `finish_reason`-confirmed truncation errors | Gap A | ●○○ | ●●○ | inferred-from-JSONDecodeError guess | ✓ | ✅ shipped |
| 1c | `json_schema`-mode structured output (real shape) | Gap A | ●●○ | ●●● | `json_object` (syntax only) | △ | 🟡 shipped off-by-default; entities-empty regression root-caused + fixed, 2/3 e2e trials pass 95+; still opt-in pending the separate content-fidelity question (see Gap A) |
| 2 | Feed Step 3 the raw brief | Gap B | ●○○ | ●●● | — (additive) | ✓ | ✅ shipped |
| 3 | Extend rescue layers to scenes/audio/narrative/title | Gap B | ●●○ | ●●● | — (mirrors existing pattern) | △ | ✅ shipped |
| 4 | Automated render + perceptual diff | Gap C | ●●○ | ●●● | — (additive to manifold tests) | △ | ✅ shipped (opt-in; structural black/magenta checks, not a golden-reference diff) |
| 5 | Unity PlayMode automated E2E smoke | Gap C | ●●● | ●●● | manual checklist → CI gate | △ | ✅ shipped (opt-in) |
| 6 | Grid/graph BFS reachability | Gap D | ●●○ | ●●○ | AABB + 1-D reach-gap walk | △ | ✅ shipped |
| 7 | Reuse chromadb+fastembed for model resolution & scene binding | Gap E | ●●○ | ●●○ | weighted-token scorer / substring binder | △ | ✅ shipped (opt-in; blend, not chromadb — see Gap E) |
| 8 | Mirror the `\b`-bounded regex guard | Gap E | ●○○ | ●●○ | unguarded `any(w in p for w in words)` | ✓ | ✅ shipped |
| 9 | Parametric CAD for mechanical parts | Gap F | ●●○ | ●●● | mechanical-part share of the bmesh toolkit | ✓ | ⬜ open |
| 10 | Generative-mesh APIs for organic parts | Gap F | ●●● | ●●● | — (sits beside the sculpted toolkit) | — | ⬜ open |
| 11 | Poisson-disk sampling (Bridson's algorithm) | Gap G | ●○○→●●○* | ●●○ | `_scatter_positions()` jittered grid | ✓ | ✅ shipped |
| 12 | Value/Perlin-noise elevation profile | Gap H | ●○○ | ●○○ | — (additive; elevation was flat) | ✓ | 🟡 primitive shipped, not wired |
| 13 | Local embedding rerank of Sketchfab candidates | Gap I | ●●○ | ●○○ | — (layers on existing author-affinity biasing) | — | ✅ shipped (PR #172; table corrected from stale `⬜ open`) — pool-width follow-up shipped opt-in, see Gap I |

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
- **2026-09-02 (root cause + fix)** — found and fixed the `entities: []` regression the
  correction above documented. A fast probe harness (one fixed concept, direct httpx calls
  against the real Step 3 prompt, bypassing the ~90s full-pipeline round-trip per attempt)
  showed every empty response had `finish_reason: stop` — a choice, not a truncation — and
  that `entities` sits 20th of 37 properties inside an unbroken run of 16 consecutive
  all-optional fields that llama.cpp's grammar lets the model skip past entirely, landing
  instead on the multimodal sections its own prompt names explicitly. `minItems: 1` alone did
  nothing (an omitted key never reaches its own `minItems`); adding `entities` to the schema's
  top-level `required` list *together with* `minItems: 1` — so the key can't be skipped and the
  now-mandatory array can't be `[]` — produced real, on-brief entities 3/3 in the probe harness
  and passed the `unity-create` playability gate in 2/3 full end-to-end trials at 95/100 and
  92/100 (the third hit an unrelated, legitimate abstention: the model drifted onto
  reef/coral/ocean vocabulary for a land brief, and `unity_layout_archetype.py` correctly
  refused to lay that out — a content-fidelity question, not this bug, left open). Fixed in
  `src/unity_schema_jsonschema.py`'s new `_harden_for_generation()`, deep-copying
  `UnitySchema.model_json_schema()`'s output before mutating it since Pydantic doesn't
  document that dict as safe to mutate in place and other code calls the same method for
  unrelated purposes. New tests: `test_entities_forced_required_and_nonempty`,
  `test_hardening_does_not_mutate_pydantics_own_schema` in `tests/test_unity_schema_jsonschema.py`.
  `json_schema` mode stays opt-in (config default `off`) pending more end-to-end trials of the
  separate content-fidelity question, not because the emptiness bug is still open.
- **2026-09-02 (Gap C, solution #5)** — Unity PlayMode automated E2E smoke shipped:
  `src/unity_playtest.py` + `templates/unity/editor/PlayModeSmoke.{cs,Tests.asmdef}`, wired as
  `unity_cli.py playtest <project>` and `e2e-verify --playtest`. Five real batchmode round-trips
  against a live-generated project (`SkylineGlider`, `qwythos-9b-claude-mythos-5-1m-mtp`) found
  and fixed three real bugs a design-only pass would have shipped silently: the test asmdef's
  `includePlatforms: ["Editor"]` (copied from `CompileCheck.Editor.asmdef`'s convention, wrong
  here) silently excluded it from Play mode's runtime-capable compile set, so zero tests were
  ever discovered — fixed to `[]`, matching Unity's own PlayMode template; `LogAssert.
  ignoreFailingMessages` set in `[SetUp]`/`[OneTimeSetUp]` ran too late to catch a log fired
  during Unity's own pre-test phase, tainting every check with one generic NUnit message — fixed
  via `[InitializeOnLoadMethod]`, which fires at domain-load time; and a genuine, previously
  invisible production bug in `LightingDirector.cs.j2`'s `HasAtmosphereController()`, which
  called `FindFirstObjectByType` on `AtmosphereBootstrap` — a bare static class, not a
  `UnityEngine.Object` — guaranteeing an engine error + `NullReferenceException` on every
  `Awake()` of every project using the vendored atmosphere feature, invisible to static
  playability checks since nothing had ever pressed Play before. Fixed to the type-existence
  check the surrounding comment always intended. Also found live: `com.unity.test-framework` is
  editor-bundled on modern Unity (6000.x) — forcing a manifest pin dragged in an unneeded
  `com.unity.test-framework.performance` (itself pulled in transitively via `com.unity.
  collections`, unrelated to this feature) — now conditional on
  `test_framework_is_editor_bundled()`; and the Player entity was false-failing
  `EntityGroupSpawned` (it merges into the scene's pre-placed object rather than spawning a
  fresh `Player_0`) — excluded there via the existing `pick_player_entity` helper in favor of the
  more-correct `PlayerTagCardinality` check. Final live run: **PASS — compile=OK, playtest=OK
  (8/8)**. New tests: `tests/test_unity_playtest.py` (21 fast, no-Unity-launch unit tests covering
  every expectations-derivation branch — skip:/fx:/tiled-path/scene-scoped/2D-archetype/
  locomotion="none"/player-exclusion — plus NUnit XML parsing and manifest-merge idempotency;
  one `unity_playtest`-marked opt-in real-Editor test). Full suite: 4076 passed, 1 skipped, same
  one pre-existing unrelated `tests/test_outputs_smoke.py` failure. Solution #4 (render +
  perceptual diff) remains open — see Gap C.
- **2026-09-02 (Gap C, solution #4, follow-up)** — automated render + perceptual diff shipped:
  a fifth `[UnityTest]`, `CaptureMainCameraScreenshot`, plus `analyze_render_health()`
  (`src/unity_playtest.py`) — structural black/magenta detection, not a golden-reference diff
  (no fixed "correct" image exists for arbitrary LLM-generated content). Getting a clean
  capture took five live attempts against `SkylineGlider`'s Cinemachine+URP setup — full
  blow-by-blow in `PlayModeSmoke.cs`'s own comments and in Gap C above — ending in a genuine,
  previously-invisible generator bug: `SetupRenderPipeline.cs.j2` gated first-open pipeline
  setup on a machine-global `EditorPrefs` key before checking the project's own
  `GraphicsSettings.defaultRenderPipeline`, so regenerating a same-named project (an entirely
  normal workflow) left every copy after the first permanently stuck on the built-in render
  pipeline — every URP material rendered as Unity's magenta fallback, with no console error at
  all, invisible to every prior check including this repo's own compile gate. Fixed by
  checking the real project state first, unconditionally. New tests:
  `TestAnalyzeRenderHealth` in `tests/test_unity_playtest.py` (5 fast, no-Unity-launch cases:
  a normal frame, solid black, dominant magenta, a small legitimate magenta patch that must
  NOT trip the dominance threshold, and a missing screenshot file). Live-verified end to end:
  `unity_cli.py playtest` on a freshly regenerated `SkylineGlider` — **PASS, compile=OK,
  playtest=OK, 9/9**, screenshot black=0% magenta=0%, confirmed by eye to be a real sky-toned
  frame. Gap C is now fully closed — both of its named solutions (#4 and #5) shipped.
- **2026-09-02 (Gap B, solution #3)** — the rescue-layer pattern shipped for
  SCENES/AUDIO/NARRATIVE/TITLE, closing the §5-ranked "highest-value" item: four new sibling
  modules (`src/unity_concept_scenes.py`/`_narrative.py`/`_audio.py`/`_title.py`) mirror
  `unity_concept_entities.py`/`unity_concept_scripts.py` exactly — deterministic brief parse,
  Step-3 checklist, append-only Step-5 rescue — wired into both `src/unity_pipeline.py` and
  `src/unity_eval.py`'s replay (and into the eval-parity guard's `_TAIL_CALLS` allowlist in
  `tests/test_unity_playability_eval.py`). `src/unity_briefparse.py`'s own private section
  readers were extracted verbatim into those four modules rather than kept as a second,
  drifting implementation — the shared low-level helpers (`_section`, `_scalar`,
  `_fold_numbered`, `_to_pascal`, the header vocabulary) now live in one place,
  `src/unity_brief_sections.py`, new. Confirmed behavior-preserving against
  `prompts/coldwater_circuit.txt`'s recorded §1.6 numbers (title `ColdwaterCircuit`, 5 scenes,
  9 entities, 12 scripts, 6 beats, 10 audio cues), locked in by a new
  `tests/test_unity_briefparse.py` — the offline `--no-llm` arm had no direct test coverage
  before this pass. TITLE is the one asymmetric rescue in the family (scalar
  override-if-wrong, two confidence tiers — see `src/unity_concept_title.py`'s own docstring)
  rather than a list append. New tests: 53 cases across
  `tests/test_unity_concept_{scenes,narrative,audio,title}.py` plus
  `tests/test_unity_briefparse.py`. Full suite: 4134 passed, 1 skipped, same one pre-existing
  unrelated `tests/test_outputs_smoke.py` failure (confirmed via `git stash` to predate this
  change).
- **2026-09-08 (Gap E, solution #7)** — the remaining half of Gap E shipped: embedding-based
  refinement for catalog matching (`unity_entity_asset_resolver.py`) and scene binding
  (`unity_scenebind.py`), both opt-in and off by default. Deliberately **not** `chromadb`
  (see the write-up above for the scale/corpus-stability analysis) — new
  `src/unity_semantic_match.py` reuses PR #172's `unity_semantic_embed.text_similarity()`
  instead, with a precomputed/cached matrix for the catalog tier
  (`unity-catalog --embed`) and direct pairwise calls for the tiny-N scene-bind tier. Both
  blend points sit structurally outside every existing hard gate (the catalog scorer's
  strong-hit gate, the scene-bind role veto), verified live by two purpose-built calibration
  scripts (`tools/calibrate_catalog_embedding.py`, `tools/calibrate_scenebind_embedding.py`)
  against the real bge-small embedder, not guessed — `config.example.yaml`'s
  `embedding_weight: 0.5` defaults for both `assets:` and the new `scenebind:` block cite the
  measured numbers directly. New tests: `tests/test_unity_semantic_match.py` (21),
  `TestEmbeddingRefinement` added to `tests/test_entity_asset_resolver.py` (6) and
  `tests/test_unity_scenebind.py` (8), new `tests/test_cli_unity_catalog.py` (4) — 39 new
  tests total. Full suite: 4359 passed, 1 skipped, same one pre-existing unrelated
  `tests/test_outputs_smoke.py` failure (a stale gitignored `output/` artifact from a
  concurrent session's GTA5 run, confirmed unrelated — this pass never touches the GTA5
  backend).
- **2026-09-10 (Gap I correction + solution #13)** — corrected Gap I's stale "nothing
  reranks the candidates" claim: `unity_semantic_embed.semantic_filter()` /
  `semantic_filter_frozen()` (PR #172) already gate AND reorder Sketchfab candidates by
  CLIP/bge-small cosine similarity; solutions-table row #13 updated from `⬜ open`. The real,
  narrower gap found instead: `semantic_filter`'s `max_candidates` (default 6) only ever
  scored the first 6 of the up-to-24 keyword-ranked candidates `SketchfabClient.search()`
  already fetches per query. New `sketchfab.semantic_pool_size` config key (default `6`,
  preserves today's exact behavior) now threads through `max_candidates` at all four call
  sites (`cli/unity_assets.py`, `unity_entity_asset_resolver.py`'s Tier S live-fetch AND its
  network-free cache-hit re-check, `unity_sketchfab_collection.py`). Also fixed, found while
  touching the cache-hit call site: `sk_scored` was never sorted by keyword score before
  that slice, so "top N" there meant "first N in catalog scan order," not "top N
  keyword-ranked" — now sorted first, matching every other call site. New
  `tools/calibrate_sketchfab_rerank_pool.py` (first calibration script needing the live
  Sketchfab search API, not local/synthetic data) measured 4 real queries live — 1 returned
  no licensed results; of the 3 measurable, only "traffic cone" showed a rescue case (best
  match at keyword rank #19, margin ~0.01 against a ~0.18-wide spread) — too narrow a signal
  to justify widening the shipped default off one query, so `semantic_pool_size` stayed at
  `6`. New tests: 9 across `tests/test_unity_semantic_embed.py` (2, the `max_candidates`
  slicing contract for both `semantic_filter` and `semantic_filter_frozen`, neither
  previously covered),  `tests/test_cli_sketchfab_fetch.py` (2), `tests/test_entity_asset_
  resolver.py` (3, including the cache-hit ordering regression pin), `tests/test_unity_
  sketchfab_collection.py` (2); 2 existing tests
  (`test_semantic_select_closure_binds_entity_query_and_thresholds`,
  `test_semantic_select_closure_binds_brief_query_and_thresholds`) had their monkeypatched
  `semantic_filter` fakes widened to accept `max_candidates` — their old keyword-only
  signature with no `**kwargs` would otherwise `TypeError` once the real call sites started
  passing it. Full suite: 4493 passed, 1 skipped, same one pre-existing unrelated
  `tests/test_outputs_smoke.py` failure (confirmed unrelated, same stale `output/` artifact
  as the prior entry).
- **2026-09-10 (Gap I, widened calibration)** — the 4-query calibration run above was too
  small to draw a real conclusion from; widened `tools/calibrate_sketchfab_rerank_pool.py`'s
  `QUERIES` to 14, spanning vehicle/weapon/character/structure/nature/urban-prop categories,
  and re-ran live. Result reversed the earlier read: 9 of 13 measurable queries had their
  best semantic match sitting outside the default pool of 6 — a real, repeatable pattern,
  not the noise the first sample suggested. Margins ranged from near-zero (`zombie`, +0.001)
  to genuinely meaningful (`warehouse building`, +0.027 at rank #15). Kept
  `semantic_pool_size` at `6` regardless — a deliberate cost/coverage tradeoff (catching most
  of the meaningful-margin cases needs a pool reaching into the high teens/twenties, at
  2.5x-4x today's per-query cost), not an evidence gap this time. `config.example.yaml`'s
  comment and Gap I's write-up above were updated with the real, widened numbers.
