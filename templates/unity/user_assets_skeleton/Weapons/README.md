# _UserAssets/Weapons

Guns, blades, throwables, energy weapons.

## Naming

- `Weapon_<Type>.fbx`: `Weapon_AssaultRifle.fbx`, `Weapon_Katana.fbx`,
  `Weapon_Grenade.fbx`.

## Mount / muzzle conventions

If you want the generated controller/shooting scripts to attach weapons
without rewiring, expose two child transforms on the weapon prefab:

- `Muzzle` — at the barrel tip, +Z forward; used as the spawn point for
  projectiles / line-cast origin.
- `GripHand` — where the player's hand grabs the weapon; used for hand-IK.

## Free sources

- **Kenney "Weapon Pack"** — CC0, dozens of low-poly stylized weapons.
- **Quaternius "Weapons Pack"** (CC0).
- **Sketchfab** — filter CC0 + "weapon" or specific names like "rifle",
  "katana".
- **Mixamo** has weapon-holding animations (pistol idle, rifle aim, sword
  swing) you can retarget onto any humanoid.

## Wiring into the generated weapon prefab

For HUD/inventory icons, drop a `Texture2D` next to the weapon prefab named
`<WeaponName>_Icon.png`. The generated `Assets/Scripts/UIManager.cs` looks
for an icon at that path when displaying ammo / weapon-swap UI.
