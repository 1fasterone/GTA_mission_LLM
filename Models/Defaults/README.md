# Default Models — what generated projects use instead of capsules/cubes

Every generated Unity project copies the models in this tree into
`Assets/Resources/Models/Default/` and swaps them in for the primitive
placeholders (Player capsule, Enemy sphere, entity cubes, vehicle box).
**Anything missing here simply falls back to the primitive — nothing breaks.**

## Current seed (works today)
- `player/Player.fbx` — Kenney skate-boy (CC0, rigged)
- `player/Run.fbx, Jump.fbx, Attack.fbx, Crouch.fbx` — animation clips
- `enemy/Enemy.fbx` — Kenney skate-girl (CC0, rigged)

## Shopping list (free downloads to upgrade the pack)

| Drop here | What to get | Where |
|---|---|---|
| `player/Player.fbx` | A nicer rigged humanoid, T-pose, **"FBX for Unity" + With Skin** | mixamo.com (free Adobe account) |
| `player/Idle.fbx` | Idle animation, same character, **Without Skin** | Mixamo |
| `player/Death.fbx` | Death animation, Without Skin | Mixamo |
| `enemy/Enemy.fbx` | 1–2 rigged humanoids (clips retarget automatically via Humanoid rig) | Mixamo or quaternius.com (CC0) |
| `vehicle/Car.fbx` | Low-poly car **with separate wheel meshes** | kenney.nl → Car Kit (CC0) |
| `crowd/Spectator.fbx` | 2–3 static low-poly figures (no rig needed) | kenney.nl → Mini Characters |
| `props/Tree.fbx`, `Rock.fbx`, `Building.fbx`, `Barrier.fbx`, `Cone.fbx` | Environment props | kenney.nl → Nature Kit / City Kit (CC0) |

Notes:
- Mixamo FBX exports cannot be redistributed in the product zip — they stay on
  your machine; Kenney/Quaternius CC0 files can ship.
- File names matter: the pipeline looks up the exact names in
  `lookups/unity/default_models.json` — add new kinds/files there too.
- Rigged characters import as **Humanoid** automatically (the pipeline writes
  the .meta); clips from any Mixamo character retarget to any other.
