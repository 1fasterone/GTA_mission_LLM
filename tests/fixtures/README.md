# tests/fixtures

Checked-in inputs and locked baselines for the layout tests.

| file | what it is |
| --- | --- |
| `dropzone_skydive.schema.json` | A filled `UnitySchema` for the DropZone skydiving brief (`prompts/dropzone.txt`) — altitude-banded content, `locomotion: "fly"`, a Cessna the player exits, a landing pad on the ground. The VerticalCorridor + exit-platform fixture. |
| `skyline_glider.schema.json` | `locomotion: "fly"` with no descent framing, a 900 m ceiling, floating islands and one terrain ridge. The BoundedVolume **air** fixture — this shape used to hard-fail `[layout.strict]`. |
| `lagoon_survey.schema.json` | `locomotion: "swim"` with no descent framing and no altitude figure anywhere (so it exercises the named-default warning), plus a surface-marked survey boat. The BoundedVolume **water** fixture. |
| `dropzone_arcade_llm.schema.json` | A **real** schema-fill output captured verbatim from a live `unity-create` run. Generic kinds with the meaning in `tag` — the shape every hand-written fixture failed to represent, and the one that broke role detection. |
| `living_wall_llm.schema.json` | A **real** schema-fill output from the living-wall climbing run: `tag='Holdable'` holds, a `structure`/`Environment` cliff, wall-attached hazards. The VerticalSurface fixture. |
| `skyline_strike_step35.acceptance.json` | A **real** visual schema capture with persisted Step-3.5 micro inputs and dialogue, plus its frozen Slice-6.3 fusion artifact. `tests/test_frozen_step35_acceptance.py` replays it with no LLM or network, requires every spatial check to pass, reconstructs `Assets/Micros` with `unity-validate --compose`, and enforces strict playability at 95/100. |
| `vinewood_rooftop_infiltration_llm.schema.json` | A **real** GTA5 `MissionSchema` schema-fill output captured verbatim from a live `generate` run against the held-out brief `prompts/vinewood_rooftop_infiltration.txt`. First live capture where the LLM correctly paired a `teleport_points` entry with a `keybinds` entry targeting it (`F6` -> teleport to the rooftop) — the vocabulary gap two earlier live runs against other gauntlet briefs exposed (one drifted into an unrelated race archetype and dropped the mechanic entirely; the other added a `teleport_points` entry but no keybind to fire it), which is what motivated tightening the `keybinds` field's instructions in `src/prompts.py`. See `tests/test_mission_keybinds.py::TestVinewoodRooftopLiveCapture`. |
| `arenaplane_baseline.json` | The ground-plane layout **as it was before archetype selection existed**: baked `entities`, `world_spec` and `terrain_bounds` for a walking prop scene and for the racetrack corridor variant, plus the file list of the whole generated `Assets/` tree. `tests/test_unity_layout_archetype.py` asserts ArenaPlane still reproduces it exactly. |

Regenerating `arenaplane_baseline.json` defeats its purpose — it is a recording
of `origin/main`, i.e. the layout WITHOUT the archetype work, not a snapshot of
current behaviour. Re-record it only when main itself legitimately moves (it was
re-recorded once, when #108 added `EntitySpec.elevation`), and say so in the PR.

It stores a file LIST rather than a byte hash, and is compared with floats
rounded to 1e-6: generated `.unity` text embeds float64 positions, and
`math.sin`/`cos` differ in their last bits between platforms — a ring of drones
came out 2 ULP apart on Linux CI versus the Windows box the first recording was
taken on. The exact positions are still asserted, just not their last bits.
