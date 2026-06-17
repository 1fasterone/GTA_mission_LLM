# _UserAssets/Environment

Terrain pieces, modular building kits, skyboxes, vegetation, PBR textures.

## What goes here

- Modular building kits (walls, floors, roofs).
- Terrain heightmaps and splatmaps.
- Skyboxes (cubemap or 6-sided).
- HDRI lighting probes.
- Vegetation: trees, grass, rocks.
- PBR material textures (albedo, normal, roughness, AO).

## Naming

- `Env_<Region>_<Piece>.fbx`: `Env_City_Building_A.fbx`,
  `Env_Forest_Tree_Pine.fbx`.
- Textures: `<material>_<channel>.png` — `concrete_albedo.png`,
  `concrete_normal.png`, `concrete_roughness.png`.
- Skyboxes: `Skybox_<MoodOrName>.hdr` or 6× `Skybox_<Name>_<face>.png`.

## Free sources

- **Kenney "City Kit"**, **"Nature Kit"**, **"Castle Kit"** (CC0).
- **Quaternius "Ultimate Nature Pack"** (CC0).
- **AmbientCG** — CC0 PBR materials and HDRIs, hundreds of options.
- **Poly Haven** — CC0 HDRIs, textures, and models.
- **Sketchfab** — search "modular" + CC0 filter.

## Wiring

URP / HDRP projects: drop PBR textures into a folder, right-click → Create
Material, assign textures. The generator's `MaterialPalette.cs` script can
load colors at runtime if you want palette-based theming — see its
generated comments for the format.

For HDRIs: Window → Rendering → Lighting → Environment, drag your HDR into
the Skybox Material slot, bake or set to Realtime.
