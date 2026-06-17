# _UserAssets/UI

HUD icons, fonts, panel backgrounds, screen art.

## What goes here

- Icons (weapons, items, status effects).
- Fonts (TTF / OTF or pre-baked TextMeshPro atlases).
- Panel backgrounds, frames, buttons, dialog boxes.
- Loading screen / splash art.
- Pixel-art tilesets for 2D games.

## Recommended format

- **Icons**: PNG with transparency. 128×128 or 256×256 for HD HUDs.
- **Fonts**: TTF, then run TextMeshPro → Font Asset Creator to bake a
  signed-distance-field atlas. Drop the resulting `.asset` next to the TTF.
- **Pixel art**: PNG, Sprite Mode = Multiple in the importer if it's a
  tileset.

## Naming

- `Icon_<Category>_<Name>.png`: `Icon_Weapon_Rifle.png`,
  `Icon_Status_Poison.png`.
- Fonts: `Font_<Use>.ttf` — `Font_HUD.ttf`, `Font_Dialogue.ttf`.

## Free sources

- **Game-Icons.net** — 4,000+ CC-BY vector icons in a consistent style.
- **Kenney "UI Pack"** + **"Game Icons"** — CC0.
- **Google Fonts** — most are SIL Open Font License (free for commercial).
- **DaFont** — filter by "100% Free" license.
- **OpenGameArt** — broad selection of 2D UI art.

## Wiring

The generated `Assets/Scripts/UIManager.cs` looks for icons under
`Assets/UI/Icons/` by name convention. Drop your PNGs in
`_UserAssets/UI/Icons/`, then move (or symlink, or copy) them into
`Assets/UI/Icons/` so the importer picks them up. The naming pattern in
the generated HUD micro README documents the exact mapping.
