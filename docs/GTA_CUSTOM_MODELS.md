# GTA5 — Custom Models & AddOn Injection

Short version: **GTA5 doesn't load mesh files at runtime.** Every ped, vehicle,
prop, and weapon the game can spawn is identified by a fixed **model hash**
(CRC of the model name) baked into a `.rpf` archive. To "add a model" you
must register a new archive that the game's streaming engine can read on
boot. This is the AddOn system.

This doc explains how AddOn models work, how the generator currently handles
stock models, and how to extend it for custom AddOn names.

---

## How GTA5 finds a model

The chain of resolution inside the game:

```
Script calls:   World.CreatePed(new Model("a_m_m_business_01"), pos)
                       │
                       ▼
ScriptHookV asks the streaming engine for the model.
                       │
                       ▼
Streaming engine hashes the name → looks up an entry in its loaded archives.
                       │
                       ▼
Archive sources, in order:
  1. mods/update/x64/dlcpacks/*/dlc.rpf   (mod-loader installed packs)
  2. update/x64/dlcpacks/*/dlc.rpf         (stock DLC)
  3. x64*.rpf in the install root          (base game)
```

If no archive exposes a model with that name's hash, the call fails silently
and the ped/vehicle never spawns. That's why a `.3.cs` script referencing
"my_super_car" will compile but spawn nothing unless an AddOn is installed.

---

## The three ways to "inject" a model

### 1. **AddOn pack (recommended for custom content)**

You ship the model in your own `dlc.rpf`:

```
mods/update/x64/dlcpacks/my_custom_pack/
└── dlc.rpf
    ├── x64/
    │   └── levels/gta5/vehicles/my_custom_pack.rpf  (the actual .yft model)
    ├── common/data/
    │   ├── vehicles.meta                            (registers the model)
    │   ├── handling.meta                            (physics)
    │   └── carcols.meta                             (colours)
    └── content.xml + setup2.xml                     (DLC manifest)
```

Then the player edits `mods/update/update.rpf/common/data/dlclist.xml` to
add the new pack. The game boots, the streaming engine reads the pack, and
suddenly `Model("my_custom_car")` resolves.

**The script doesn't change.** Same `World.CreateVehicle("my_custom_car",
pos)` call works against either a stock vehicle or an AddOn — the name just
needs to resolve in some loaded archive.

This is what the generator should target: **emit scripts that work with any
model name, then let the tester/distributor decide which AddOn packs are
installed.**

### 2. **Replacement mod (overwrites a stock model)**

Player uses OpenIV to replace a stock model's `.yft` inside the original
archive. The model name and hash stay the same; the geometry changes. From
the script's perspective nothing changed — `World.CreatePed("a_m_m_business_01")`
just looks different now.

**Useful when you want to make every business-suit NPC look like your custom
character without writing any new scripts.**

### 3. **Stream-side asset (advanced)**

For vehicles only, a script can call:
```csharp
Function.Call(Hash.REQUEST_MODEL, modelHash);
while (!Function.Call<bool>(Hash.HAS_MODEL_LOADED, modelHash)) Yield();
```

This forces streaming of an AddOn that's already in the archive but
hasn't been loaded yet. SHVDN3's `Model.Request()` wraps this. The generator
already emits this pattern for every script — that's why long-tail AddOn
models work without modification.

---

## How this generator currently handles models

Look in `lookups/`:

```
lookups/
├── peds.json         # synonyms → stock model names + valid_models list
├── vehicles.json
├── objects.json
├── weapons.json
└── audio.json
```

Each file has two sections:

```json
{
  "synonyms": {
    "drug dealer": "a_m_m_business_01",
    "biker thug":  "g_m_y_lost_01"
  },
  "valid_models": [
    "a_m_m_business_01",
    "a_m_m_business_02",
    ...
  ]
}
```

The flow at generation time (`src/resolver.py`):

1. LLM produces a plain-English description: *"a biker thug in leather"*.
2. RAG (if enabled) queries the local ChromaDB `gta5` domain.
3. Falls back to the local `synonyms` table, then to a word-overlap search.
4. The chosen model is **validated against `valid_models`** — if it's not
   in the stock list, the script falls back to `UNRESOLVED`.

That last step is the **point where AddOn support hooks in**.

---

## How to extend the generator for AddOn models

You don't need to modify the pipeline code. Just add the AddOn names to the
lookup tables — the resolver will treat them as first-class once they're in
`valid_models`. Two clean patterns:

### A. Edit the JSON directly (project-wide)

Open `lookups/vehicles.json` and add:

```json
{
  "valid_models": [
    "...existing stock list...",
    "addon_supra_mk4",
    "addon_porsche_911"
  ],
  "synonyms": {
    "supra":     "addon_supra_mk4",
    "old porsche": "addon_porsche_911"
  }
}
```

Any prompt that lands on those synonyms now produces a script that calls
`World.CreateVehicle("addon_supra_mk4", pos)`. As long as the player has
the matching AddOn pack installed, it spawns.

### B. Sidecar overlay (tester-friendly, planned for Iteration 5)

A future build will load `lookups/<category>.user.json` if present and merge
it over the stock table. Testers can drop their own AddOn lists in there
without editing the project's tracked files:

```json
// lookups/vehicles.user.json
{
  "valid_models": ["addon_my_custom_truck"],
  "synonyms":     { "custom truck": "addon_my_custom_truck" }
}
```

This pattern keeps the stock JSON pristine for source control and lets
testers express their own AddOn library.

---

## The generated script's safety net

The pipeline already has two passes that protect testers from AddOn-name
typos and from the LLM hallucinating model names:

1. **`UNRESOLVED` token**: if a model can't be resolved, the schema marks it
   `UNRESOLVED` and the code generator emits a `console.print` warning at
   compile time so the tester sees it before the script ships.
2. **Deterministic pick** (`resolver.pick_model_deterministic`, Step 5b):
   a description the resolver left `UNRESOLVED` is matched against the words
   inside each `valid_models` ID (`s_m_y_cop_01` → "cop"). Variants of one
   model (`_01`/`_02`, a ped's age letter) count as one; a tie between
   different models, or no shared word, keeps `UNRESOLVED` and names the
   reason in the log. No LLM runs after the schema fill, and there is no
   first-candidate fallback.

AddOn names you add to `valid_models` take part in the same match, so give
them descriptive IDs (or a `synonyms` entry) for them to be picked.

---

## Quick reference for testers

| Goal                                         | What to do                                              |
|----------------------------------------------|---------------------------------------------------------|
| Use a stock GTA5 character                   | Nothing — generator already handles all 1,200+ stock models. |
| Use an AddOn character pack you installed    | Add its model name to `lookups/peds.json` → `valid_models` and a synonym. |
| Make every cop look like your custom mesh    | Use OpenIV to replace the stock cop `.yft`. Script doesn't change. |
| Try a model that may or may not exist        | Run with `--basic --debug` Custom Args; watch the log for `UNRESOLVED`. |
| Find what stock models are available         | `python main.py resolve "biker" peds` prints the resolved name; `lookups/*.json` lists everything. |

---

## What about peds with custom voices, animations, etc.?

That's all handled inside the AddOn pack's `.ymt`, `.ydd`, and `.ycd` files
— the script just calls `World.CreatePed(model, pos)` and the streaming
engine pulls in everything tied to that model hash. **No script change
needed.** The generator's mission scripts don't care whether a ped is stock
or AddOn — only the spawn call matters, and that's already the same code
path for both.
