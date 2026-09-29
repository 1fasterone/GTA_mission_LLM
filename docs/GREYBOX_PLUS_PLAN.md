# Greybox+ image-to-scene plan

## Goal

Turn a reference image and a natural-language gameplay brief into a
reproducible Unity Greybox+ scene: primitive-only geometry that preserves the
reference's spatial composition, silhouette, massing, navigation cues, and
landmarks without copying its textures, lighting, shaders, or final art.

The generated scene must be editable in Unity, exportable back to canonical
source, rebuildable without transform drift, and suitable as a transform/role
handoff to a later modular asset kit.

## Authority and boundaries

| Input | Authority |
| --- | --- |
| Natural-language brief | Gameplay semantics, required spaces, traversal, and named landmarks. |
| Reference image (`--viz`) | Composition, massing, silhouette, landmark placement, and approximate vertical relationships. It is not authority for final art. |
| Accepted Greybox+ IR | The sole authority for every generated transform, hierarchy, primitive role, and validation result. |
| Unity Editor | A controlled authoring surface only. Moved transforms become authoritative only after canonical export and validation. |

The Pipeline LLM may interpret the prompt and image in Steps 2–3. Steps 4–7
remain deterministic and make no LLM calls.

## Target command contract

This is a proposed interface, not a command that exists yet:

```powershell
python main.py unity-create "Playable industrial extraction yard with a tank zone, pipe corridor, and elevated catwalk route" `
  --geometry-only --greybox-plus `
  --viz "C:\references\industrial_yard.png" `
  --seed 17
```

`--geometry-only` guarantees no generated final-art assets. `--greybox-plus`
selects the image-aware primitive layout contract. `--viz` records the image
as composition evidence; it does not turn on texture, lighting, material, or
prop generation.

## Versioning and hierarchy

The existing `Dungeon_Greybox/{Geometry,Rooms,Corridors,Elevation,Markers}`
contract remains version 1. Greybox+ is a new, versioned hierarchy so existing
dungeon projects do not silently change shape:

```text
GreyboxPlus_Level/
  Geometry/
    Macro/
      Buildings/
      TerrainBlocks/
      Elevation/
    Meso/
      Pipes/
      Tanks/
      Machinery/
      Catwalks/
    Micro/
      Cover/
      Blockers/
      Railings/
  Markers/
    Spawns/
    AI_Nodes/
    CameraVolumes/
```

The slice-one migration must make the root/version choice explicit in the
schema, transport payload, editor template, exporter, and validators together.
No compatibility-breaking root rename may be hidden inside a generator.

## Canonical primitive contract

The Greybox+ IR retains the dungeon contract's spatial truth (`rooms`,
`corridors`, `elevation_changes`, `unity_primitives`, and `hierarchy`) and adds
versioned Macro/Meso/Micro roles to each serialized primitive.

Every primitive records a stable ID, primitive kind, hierarchy parent, exact
grid transform, dimensions, rotation, and role. Structural primitives build
rooms, floors, perimeter walls, openings, ramps, and catwalks. Industrial
primitives build ducts, pipes, tanks, supports, and machinery silhouettes.
Micro primitives supply cover, blockers, railings, and traversal cues.

No Unity-only guessed geometry is allowed. The generated ProBuilder scene is a
direct reading of the canonical primitive list.

## Slice plan

### Slice 1 — command and IR boundary

1. Add the proposed `--greybox-plus` command mode and wire it to `--viz`.
2. Define the versioned Greybox+ IR, transport representation, root hierarchy,
   primitive-role vocabulary, and strict geometry-only exclusions.
3. Extend Step 3 prompting so image composition is interpreted only while the
   Pipeline LLM is still permitted to work; text remains semantic authority.
4. Add deterministic schema/IR validators for hierarchy, role vocabulary,
   grid alignment, image-binding completeness, and exact coordinates.
5. Add deterministic fixtures for a tank, pipe, catwalk reference layout.

**Slice 1 acceptance:** the command produces a valid canonical Greybox+ IR and
Unity transport sidecar from an image-bearing schema, while producing no Unity
art/gameplay output yet.

### Slice 2 — ProBuilder realization and controlled editing

1. Generate Macro/Meso/Micro ProBuilder primitives from the IR under the
   versioned hierarchy.
2. Build navigable floors, walls, door openings, corridors, ramps, catwalks,
   supports, tanks, ducts, and sightline blockers from explicit primitives.
3. Add `Export Canonical Greybox+ IR` and `Rebuild From IR` Unity editor
   commands. Export snaps transforms to the grid and fails loudly on invalid
   hierarchy, unknown roles, or non-grid movement.
4. Add `Freeze for Assetization`, which exports transforms and role IDs for a
   modular-kit workflow without generating final art.

**Slice 2 acceptance:** an edited Unity scene exports, validates, rebuilds, and
matches its exported transforms exactly.

### Slice 3 — visual acceptance and E2E evidence

1. Produce a deterministic editor capture camera for evidence only; it never
   becomes persistent scene lighting or art.
2. Compare image-derived composition bindings with the generated primitive
   layout: landmark ordering, occupancy regions, major massing, route shape,
   and player-height readability.
3. Extend E2E verification to require Unity compilation, saved `Main.unity`,
   hierarchy realization, topology, exact transforms, a healthy capture (no
   missing shader), and composition acceptance.
4. Run a held-out reference image through the full path and retain the IR,
   Unity project, capture, export, and rebuild evidence.

**Slice 3 acceptance:** a reference image becomes a healthy, navigable,
primitive-only Unity scene whose spatial identity is recognizable as distinct
areas such as a tank zone, pipe corridor, and catwalk route.

## Non-goals

- Pixel-for-pixel recreation of a final-art image.
- Texture, material, lighting, shader, enemy, item, prop, or gameplay-script
  generation in geometry-only mode.
- Hand-editing a generated Unity scene without exporting the edits back to
  canonical IR.
- Treating a static topology score as visual or runtime proof.

## E2E evidence chain

```text
brief + reference image
  -> Step 2/3 concept and schema
  -> deterministic Greybox+ IR
  -> deterministic ProBuilder scene
  -> Unity compile + saved Main.unity
  -> topology + transform + composition checks
  -> Unity edit -> canonical export -> rebuild equivalence
  -> assetization transform/role handoff
```
