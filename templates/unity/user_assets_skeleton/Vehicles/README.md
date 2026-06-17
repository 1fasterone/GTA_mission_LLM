# _UserAssets/Vehicles

Cars, bikes, planes, boats, hover-craft, anything with wheels or thrust.

## Naming

- `Vehicle_<Type>.fbx`: `Vehicle_MuscleCar.fbx`, `Vehicle_Bike_Sport.fbx`,
  `Vehicle_Cessna.fbx`.

## Pivot + axis

Unity expects vehicles to face **+Z** at import. If your mesh ships
oriented differently, set the Y-rotation in the prefab rather than
re-exporting.

## Free sources

- **Kenney's "Car Kit"**, **"Plane Kit"**, **"Watercraft Kit"** — CC0,
  low-poly, all the same scale.
- **Quaternius "Vehicles Pack"** (CC0).
- **Poly Pizza** — search "low poly car" / "low poly bike" with CC-BY filter.
- **Sketchfab** — premium-look free vehicles under CC0.

## Wiring into the generated vehicle prefab

The Unity micro vehicle controller already emits a `WheelCollider` rig
underneath the vehicle prefab. Replace the visible cube with your model,
and align your wheel meshes to the four `WheelCollider` transforms — they're
named `WheelFL`, `WheelFR`, `WheelRL`, `WheelRR` inside the prefab.
