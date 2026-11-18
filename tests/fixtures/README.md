# tests/fixtures

Checked-in inputs and locked baselines for the layout tests.

| file | what it is |
| --- | --- |
| `dropzone_skydive.schema.json` | A filled `UnitySchema` for the DropZone skydiving brief (`prompts/dropzone.txt`) — altitude-banded content, `locomotion: "fly"`, a Cessna the player exits, a landing pad on the ground. The VerticalCorridor + exit-platform fixture. |
| `skyline_glider.schema.json` | `locomotion: "fly"` with no descent framing, a 900 m ceiling, floating islands and one terrain ridge. The BoundedVolume **air** fixture — this shape used to hard-fail `[layout.strict]`. |
| `lagoon_survey.schema.json` | `locomotion: "swim"` with no descent framing and no altitude figure anywhere (so it exercises the named-default warning), plus a surface-marked survey boat. The BoundedVolume **water** fixture. |
| `dropzone_arcade_llm.schema.json` | A **real** schema-fill output captured verbatim from a live `unity-create` run. Generic kinds with the meaning in `tag` — the shape every hand-written fixture failed to represent, and the one that broke role detection. |
| `living_wall_llm.schema.json` | A **real** schema-fill output from the living-wall climbing run: `tag='Holdable'` holds, a `structure`/`Environment` cliff, wall-attached hazards. The VerticalSurface fixture. |
| `neon_fugitive_flooded_district_llm.schema.json` | A **real** schema-fill output copied byte-for-byte from the live E2E run of `prompts/neon_fugitive.txt` (`origin/main` e7b11e0) that hard-failed `[layout.strict] ... motion_axis='planar' + medium='water'`. `locomotion: "drive"`, eight scenes, and every water word in ONE of them (`FloodedSlums`, "submerged structures") plus its `SubmergedWalls` entity. The regression subject for scoping medium evidence to the player and the whole play space (`TestMediumEvidenceScope`). |
| `neon_fugitive_platform_kind_llm.schema.json` | A **real** schema-fill output copied byte-for-byte from run 05 of the same E2E batch. `locomotion: "drive"`, and one boss-arena `Platform` group with `kind: "platform"` (a `_CLIMB_KINDS` member) made the whole city a VerticalSurface, which then failed `[spatial.strict] world_bounds`. Captured **after** layout, so it carries that wall's baked positions and its stamped `meta.layout_archetype_decision`; `TestPlayerScopedMotionEvidence._pre_layout` clears both before replaying the decision. |
| `skyline_strike_step35.acceptance.json` | A **real** visual schema capture with persisted Step-3.5 micro inputs and dialogue, plus its frozen Slice-6.3 fusion artifact. `tests/test_frozen_step35_acceptance.py` replays it with no LLM or network, requires every spatial check to pass, reconstructs `Assets/Micros` with `unity-validate --compose`, and enforces strict playability at 95/100. |
| `vinewood_rooftop_infiltration_llm.schema.json` | A **real** GTA5 `MissionSchema` schema-fill output captured verbatim from a live `generate` run against the held-out brief `prompts/vinewood_rooftop_infiltration.txt`. First live capture where the LLM correctly paired a `teleport_points` entry with a `keybinds` entry targeting it (`F6` -> teleport to the rooftop) — the vocabulary gap two earlier live runs against other gauntlet briefs exposed (one drifted into an unrelated race archetype and dropped the mechanic entirely; the other added a `teleport_points` entry but no keybind to fire it), which is what motivated tightening the `keybinds` field's instructions in `src/prompts.py`. See `tests/test_mission_keybinds.py::TestVinewoodRooftopLiveCapture`. |
| `neon_fugitive_run02_failed_llm.schema.json` | A **real** schema copied verbatim from the failed-run dump of a live `unity-create` run of `prompts/neon_fugitive.txt` (verbatim brief, `origin/main` e7b11e0, 2026-11-14, E2E batch run 02). The dump state is `live_at_failure`, after the `Post-Reconciliation` checkpoint. The Pipeline LLM wrote the Step 3 content (entities, scene_specs, missions, characters). Deterministic Steps 4-5 had already run up to that checkpoint. It died in the scene graph: `Hoverbike` and `PickupIdentityFile` were in no scene. No entity carries a player tag; `characters[0]` is the player and its `model_ref` names `Hoverbike`. `tests/test_unity_pipeline.py` (scene membership) replays it. |
| `neon_fugitive_run03_failed_llm.schema.json` | Same capture, batch run 03. The player avatar `PlayerHoverbike` (`kind='vehicle'`, `tag='Player'`) was in no scene. `PlatformRings` and `EnergyGenerators` were in no scene either, and nothing in the schema says where they go: the membership pass must abstain on them by name. Also a layout subject (`TestPlayerScopedMotionEvidence`): the player-attached `VehicleController`'s "drift mechanics" read as free-medium prose and floated this `locomotion: "drive"` city in a BoundedVolume. That is player-scoped text, so it exercises the rule that hover/float prose never overrides an explicit `drive`. Replayed through `_pre_layout`, because the dump was taken after layout. |
| `neon_fugitive_run11_failed_llm.schema.json` | Same capture, batch run 11. `CombatPlatforms` (`kind='platform'`) was in no scene. Its one objective belongs to a mission no scene could be inferred for. The player character's `model_ref` names `Crowd`, a `kind='crowd'` group of 40. That is the false-positive shape the `characters[]` player tier must refuse, so that a crowd is not spread into every scene as the "player". Also a layout subject (`TestPlayerScopedMotionEvidence`): `CombatPlatforms` made this `locomotion: "drive"` city a VerticalSurface, replayed through `_pre_layout`. |
| `neon_fugitive_run03_runaway_fill.prediction.json` | A **verbatim extraction**: bytes `[62, 135272)` of `lmstudio_aborted_fill_prediction.txt`, captured from the LM Studio server log for neon E2E batch run 03 (2026-11-17, `prompts/neon_fugitive.txt`). The extracted bytes are exactly the `Generated prediction:` JSON object, nothing added or reformatted. The source file is 138,277 bytes, sha256 `4a79012c9cea95c07ee369f5381384abc5f41459d9c0ad824e03c45199556c67`. The Pipeline LLM wrote its `content`: a Step 3 Unity schema fill that looped for 21 minutes until it was killed by hand. It is 118,392 chars with 235 complete `scripts` entries, 42 of them distinct. 193 entries repeat an earlier one, 192 of them from a cycle of 9 `...Manager` scripts. The reply stops while opening entry 236, before `missions`. LM Studio logged the killed generation as `finish_reason: "stop"` with no `completion_tokens`. The tests that replay it set `length` on their fake client. `tests/test_unity_schema_fill_budget.py` proves that `extract_json` would bracket-close this reply into a valid schema with no entities and no missions, and that the fill refuses it. |
| `arenaplane_baseline.json` | The ground-plane layout **as it was before archetype selection existed**: baked `entities`, `world_spec` and `terrain_bounds` for a walking prop scene and for the racetrack corridor variant, plus the file list of the whole generated `Assets/` tree. `tests/test_unity_layout_archetype.py` asserts ArenaPlane still reproduces it exactly. |

Regenerating `arenaplane_baseline.json` defeats its purpose — it is a recording
of `origin/main`, i.e. the layout WITHOUT the archetype work, not a snapshot of
current behaviour. Re-record it only when main itself legitimately moves, and say
so in the PR. It has been re-recorded twice. The first was when #108 added
`EntitySpec.elevation`. The second was when packing began to reserve instance
BODIES, not just centres: chord-spaced rings, plus `_packing_radius` and
`_preplaced_circle` in `src/unity_layout.py`. That second re-record moved only
packed groups: planar `Containers`, `Drones`, `ScrapPiles`, `Floodlights`, and
racing `Crates`, `RivalDrones`, plus their regions and terrain bounds. The
racetrack scaffold, its path groups and its preplaced karts are unchanged. The
old recording had no body overlap. It moves because every packed circle now
reserves a body, so the radii, the biggest-first order and the spiral step all
change.

It stores a file LIST rather than a byte hash, and is compared with floats
rounded to 1e-6: generated `.unity` text embeds float64 positions, and
`math.sin`/`cos` differ in their last bits between platforms — a ring of drones
came out 2 ULP apart on Linux CI versus the Windows box the first recording was
taken on. The exact positions are still asserted, just not their last bits.

## Checksums of verbatim captures

`.gitattributes` checks these files out with LF line endings, so the bytes on
disk match the hash below on every platform. The test that replays each file
asserts this hash first.

| file | sha256 |
| --- | --- |
| `neon_fugitive_run03_runaway_fill.prediction.json` | `a006cb450fa759682ad9a6f78aaa25a48d012b0697a9a9c8f22d6a26a28bbcb9` |
