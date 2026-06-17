# _UserAssets/Players

Player meshes, rigs, and animations.

## What goes here

- First-person hands / arms
- Third-person humanoid rigs (FBX preferred)
- Mixamo humanoid characters
- Player-specific animations (idle, walk, run, jump, death)
- Custom skinned meshes for the player avatar

## Recommended format

- **Mesh**: FBX with embedded media. Apply scale 0.01 on import if your DCC
  exports cm (Unity is meters).
- **Animations**: Humanoid rig type so you can retarget across characters.
  Set Animation Type to *Humanoid* in the model importer.
- **Textures**: PNG with alpha for skin, JPG for clothing/armor diffuse.

## Naming

- `Player.fbx`, `Player_Idle.fbx`, `Player_Run.fbx` — keep the prefix
  consistent so the generated controller can find them later.

## Free sources

- **Mixamo** — best place to start. Auto-rig your own character then download
  hundreds of compatible animations.
- **Quaternius "Ultimate Animated Character Pack"** — CC0 humanoids, ready
  to drop in.
- **Kenney's "Mini Characters Kit"** — low-poly, no rig (animate
  programmatically).

## Wiring into the generated player prefab

After import, open `Assets/Prefabs/Player.prefab`, swap the placeholder
capsule's MeshFilter and MeshRenderer with your imported model. If you have
animations, drop them on the Animator component the generator already wired.
