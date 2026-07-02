# Free Asset Sources — Unity

## The asset catalog (how the library reaches scenes)

The compiler assigns a **real model to every entity group** through a
generated catalog of the whole on-disk library:

```
Models/…  ──python main.py unity-catalog──▶  lookups/unity/asset_catalog.json
   asset_catalog.json ──unity_entity_asset_resolver.py──▶ entity_model_paths
   entity_model_paths ──EntitySpawner/ModelSwap──▶ spawned scene objects
```

- `python main.py unity-catalog` scans the roots in config `assets.roots`
  (default `Models/FBX format`), derives keywords/category/kind hints from
  each filename, and merges curated metadata from `kenney_catalog.json`.
  **Re-run it after adding a pack**; `--check` is the CI staleness gate.
- Per entity, resolution tiers are: explicit kind in
  `lookups/unity/default_models.json` → semantic match against the full
  catalog (entity name + kind + `model_hint`) → strict hard-fail
  (config `assets.strict`, the FAIL-FAST doctrine) or a logged default-prop
  fallback when strict is off.
- Same-kind groups deterministically rotate among the top matches, so a
  forest isn't twelve copies of one tree.
- Kenney texture folders are copied beside the imported models so materials
  don't come in blank.

Every generated Unity project also ships with an empty
`Assets/_UserAssets/` folder. Drop your own meshes, textures, animations,
and audio in there. The leading underscore sorts it to the top of the
Project window so you can find it instantly.

The categories under `_UserAssets/` mirror the kinds of references the
generator's schema can produce:

```
Assets/_UserAssets/
├── Players/        first-person hands, third-person rigs
├── Enemies/        humanoids, creatures, drones
├── Vehicles/       cars, bikes, planes, boats
├── Weapons/        guns, swords, throwables
├── Objects/        pickups, props, interactables
├── Environment/    terrain pieces, modular kits, skyboxes
├── Audio/          SFX, music, ambient
└── UI/             icons, fonts, HUD art
```

Each subfolder has its own `README.md` with category-specific tips on file
formats, scale, and naming conventions.

---

## Where to get free assets

License rule of thumb: **CC0 is always safe to use and modify**. CC-BY
requires attribution somewhere in your build (usually `README.md` or an
in-game credits screen). Always re-check the license on each pack before
shipping commercially.

### Tier 1 — start here

| Source                                          | What you'll find                              | License        |
|-------------------------------------------------|-----------------------------------------------|----------------|
| [Kenney.nl](https://kenney.nl)                  | Huge low-poly packs: characters, vehicles, weapons, UI, audio. The bundled `KenneyImporter.cs` already auto-imports these. | CC0            |
| [Quaternius](https://quaternius.com)            | Stylized low-poly characters, animals, environment kits. | CC0            |
| [Poly Pizza](https://poly.pizza)                | Google's CC-BY 3D library — 8,000+ models.    | CC-BY 3.0      |
| [Mixamo](https://mixamo.com)                    | Rigged humanoid characters + thousands of animations. Free Adobe account. | Custom (free for personal & commercial) |
| [AmbientCG](https://ambientcg.com)              | PBR textures and HDRIs.                       | CC0            |
| [Freesound](https://freesound.org)              | Sound effects. Filter by CC0.                 | Mixed (filter) |

### Tier 2 — when you need more

| Source                                          | What you'll find                              | License        |
|-------------------------------------------------|-----------------------------------------------|----------------|
| [Sketchfab](https://sketchfab.com/3d-models?features=downloadable&licenses=72360a83a2ce4dd1b8d63ce29ddca6c4) | Massive 3D library. Filter by CC0 / Free Standard. | Mixed (filter) |
| [OpenGameArt](https://opengameart.org)          | Old-school 2D + 3D bundles, classic free assets. | Mixed (filter) |
| [Itch.io free asset packs](https://itch.io/game-assets/free) | Curated indie packs.            | Mixed (per-pack) |
| [Unity Asset Store — free](https://assetstore.unity.com/?free=true) | Massive free section. Pre-imported as Unity packages. | Custom (per-asset) |
| [Game-Icons.net](https://game-icons.net)        | 4,000+ vector icons.                          | CC-BY 3.0      |
| [Pixabay](https://pixabay.com/music/) / [Free Music Archive](https://freemusicarchive.org) | Music. | Mixed (filter) |

### Tier 3 — humanoid + AI animation pipelines

Useful when generating controller / AI micros and you want polish:

- **Mixamo**: rigged characters → Auto-Rigger → animation library. Export FBX
  with skin, import into Unity, drag onto a NavMesh, the generated AI scripts
  pick them up.
- **Adobe Fuse** (legacy but free) for character creation.
- **VRoid Studio** (free) for stylized anime characters.
- **MakeHuman** (free, open source) for parametric humanoids.

---

## How the generator references your assets

Today the Unity pipeline doesn't auto-wire `_UserAssets/` into prefabs — that
would couple the schema to specific file paths the LLM can't see. Instead, the
generator emits placeholder primitives (capsule = player, cube = enemy, etc.)
under `Assets/Prefabs/`. You replace them by:

1. Drop your model into the matching `_UserAssets/<Category>/` subfolder.
2. In Unity, open the generated prefab (e.g. `Assets/Prefabs/Player.prefab`).
3. Replace the MeshFilter mesh with the imported model; replace the
   MeshRenderer materials.
4. Save the prefab. The generated controller / AI / camera scripts continue
   to work — they reference the prefab, not the underlying mesh.

This keeps generation deterministic and reproducible: the schema describes
*roles* (Player, Enemy, Vehicle), and the prefabs are the swap point between
the generator's output and your art.

---

## Naming conventions (recommended, not enforced)

- Use **PascalCase** for prefab names: `RobotEnemy.prefab`, not
  `robot_enemy.prefab`.
- Use **lowercase + underscores** for texture and audio: `gunshot_loud.wav`,
  `metal_albedo.png`.
- Keep file paths under 240 characters total — Windows pathlength limits bite
  on deep `_UserAssets/` trees.
- Don't put spaces in filenames. Unity tolerates them; build pipelines often
  don't.

---

## Curated starter packs for the beta

If you want a known-good "drop these in and everything works" set:

1. **Kenney's "Platformer Kit"** — covers Players + Environment + Objects in
   one CC0 pack. Already supported by `KenneyImporter.cs` — just put it in
   `_UserAssets/Players/Kenney_Platformer/` and the importer wires materials.
2. **Quaternius "Ultimate Animated Character Pack"** — CC0 rigged humanoids,
   covers Players + Enemies.
3. **Mixamo "Idle / Walk / Run / Death" animation set** — drop into
   `_UserAssets/Players/Animations/`, retarget in Unity.
4. **AmbientCG "Asphalt 023"** + **"Concrete 034"** + an HDRI of your choice
   — covers Environment textures for any urban prototype.
5. **Freesound "Modern Gunshot" pack** (filter CC0) → `_UserAssets/Audio/`.

Five downloads, ~30 minutes, and a generated project goes from grey-capsule
prototype to something videoable.
