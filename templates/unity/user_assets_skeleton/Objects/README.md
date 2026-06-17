# _UserAssets/Objects

Interactable props, pickups, crates, barrels, computers, switches, doors.

## What goes here

- **Pickups**: health, ammo, keys, collectibles.
- **Static props**: crates, barrels, debris.
- **Interactables**: switches, computers, levers, doors.
- **Set dressing**: small environmental detail you place by hand.

## Naming

- `Object_<Type>.fbx`: `Object_HealthPickup.fbx`, `Object_Crate.fbx`,
  `Object_DoorAutomatic.fbx`.

## Free sources

- **Kenney "Furniture Kit"**, **"Industrial Kit"**, **"Tower Defense Kit"**
  — all CC0.
- **Quaternius "Stylized Nature Pack"** for outdoor props (CC0).
- **Poly Pizza** — wide variety of CC-BY props.
- **AmbientCG** for PBR textures on your own modeled crates / barrels.

## Wiring into the generated pickup prefab

The Unity pipeline generates `Assets/Scripts/Collectible.cs` for pickups.
Drop your model into `_UserAssets/Objects/`, then assign it to the
collectible prefab's MeshFilter. The script handles the rotate/bob
animation and `OnTriggerEnter` pickup logic.
