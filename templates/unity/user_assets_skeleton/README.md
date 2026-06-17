# _UserAssets — drop your own models here

This folder is yours. The generator never writes inside `_UserAssets/`
beyond shipping these README placeholders. Replace them, delete them, add
subfolders — Unity will pick everything up on next import.

## Why the leading underscore?

Unity's Project window sorts alphabetically, and `_UserAssets/` sorts to
the top so you can find it instantly.

## Folder layout

```
_UserAssets/
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
formats, scale, and naming.

## Wiring your assets to the generated prefabs

The generator emits **placeholder primitives** under `Assets/Prefabs/` —
capsules for players, cubes for enemies, etc. Once you've imported a real
model into `_UserAssets/`, replace the prefab's mesh:

1. Open `Assets/Prefabs/Player.prefab` (or Enemy / Vehicle / etc.).
2. Click the root → swap the MeshFilter mesh to your imported model.
3. Swap the MeshRenderer materials to match.
4. Save the prefab.

The generated controller / camera / AI / HUD scripts reference the
**prefab**, not the underlying mesh, so all generator code keeps working.

## Where to find free assets

See `docs/FREE_ASSETS.md` in the project bundle. Top tier:
[Kenney.nl](https://kenney.nl) (CC0), [Quaternius](https://quaternius.com)
(CC0), [Poly Pizza](https://poly.pizza) (CC-BY), [Mixamo](https://mixamo.com)
(rigged characters + animations).
