# _UserAssets/Enemies

Hostile NPCs: humanoids, creatures, drones, turrets, bosses.

## What goes here

- Humanoid enemy rigs (FBX, Humanoid Animation Type for Mixamo
  compatibility).
- Creature meshes (Generic Animation Type).
- Drone / vehicle-shaped enemies.
- Enemy-specific animations: idle, patrol, attack, hit, death.

## Naming

- `Enemy_<Type>.fbx`: `Enemy_Goblin.fbx`, `Enemy_Drone.fbx`, `Enemy_BossRobot.fbx`.

## Free sources

- **Mixamo** — pre-rigged zombies, robots, monsters with animation libraries.
- **Quaternius "Robots Pack"** / **"Animated Monsters"** (CC0).
- **Kenney's "Animated Characters"** for low-poly enemies.
- **Sketchfab** filter: CC0 + "enemy" / "monster".

## Wiring into the generated enemy prefab

Open `Assets/Prefabs/Enemy.prefab` (or any AI-generated micro prefab),
replace the cube with your model. The generator's NavMesh + AI scripts
expect the enemy to have a Rigidbody/NavMeshAgent at the root — your model
goes underneath as a child mesh, so the navigation logic doesn't break.
