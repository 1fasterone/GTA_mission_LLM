# tests/fixtures

Checked-in inputs and locked baselines for the layout tests.

| file | what it is |
| --- | --- |
| `dropzone_skydive.schema.json` | A filled `UnitySchema` for the DropZone skydiving brief (`prompts/dropzone.txt`) — altitude-banded content, `locomotion: "fly"`, a Cessna the player exits, a landing pad on the ground. The VerticalCorridor + exit-platform fixture. |
| `skyline_glider.schema.json` | `locomotion: "fly"` with no descent framing, a 900 m ceiling, floating islands and one terrain ridge. The BoundedVolume **air** fixture — this shape used to hard-fail `[layout.strict]`. |
| `lagoon_survey.schema.json` | `locomotion: "swim"` with no descent framing and no altitude figure anywhere (so it exercises the named-default warning), plus a surface-marked survey boat. The BoundedVolume **water** fixture. |
| `dropzone_arcade_llm.schema.json` | A **real** schema-fill output captured verbatim from a live `unity-create` run. Generic kinds with the meaning in `tag` — the shape every hand-written fixture failed to represent, and the one that broke role detection. |
| `living_wall_llm.schema.json` | A **real** schema-fill output from the living-wall climbing run: `tag='Holdable'` holds, a `structure`/`Environment` cliff, wall-attached hazards. The VerticalSurface fixture. |
| `arenaplane_baseline.json` | The ground-plane layout **as it was before archetype selection existed**: baked `entities`, `world_spec` and `terrain_bounds` for a walking prop scene and for the racetrack corridor variant, plus the SHA-256 of the whole generated `Assets/` tree. `tests/test_unity_layout_archetype.py` asserts ArenaPlane still reproduces it exactly. |

Regenerating `arenaplane_baseline.json` defeats its purpose — it is a
pre-change recording, not a snapshot of current behaviour. If ArenaPlane output
must legitimately change, that is a deliberate decision to state in the PR, not
a file to refresh quietly.
