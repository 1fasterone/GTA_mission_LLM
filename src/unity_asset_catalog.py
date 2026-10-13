"""
Asset catalog builder — deterministic scan of on-disk model libraries.

Walks configured asset roots (Models/FBX format etc.), derives category,
keywords and kind hints from each filename, merges curated metadata from the
hand-written kenney_catalog.json, and emits lookups/unity/asset_catalog.json.

The output is byte-idempotent: assets sorted by relpath, keys sorted, no
timestamps — running the builder twice over the same tree yields identical
bytes. Downstream (unity_entity_asset_resolver.py, unity_asset_resolver.py)
treats the catalog as read-only lookup data, keeping Steps 4-6 deterministic.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from src.ident import chash, fhash

CATALOG_VERSION = 1
DEFAULT_ROOTS = ("Models/FBX format",)
DEFAULT_FORMATS = ("fbx",)
# 2D sprite library roots (PNG only — Unity has no native SVG importer, and
# the Double/ variants are the same art at 2x resolution).
DEFAULT_SPRITE_ROOTS = (
    "Models/FishPack/PNG/Default",
    "Models/2d-space-kit",
    "Models/space-shooter-pack/PNG",
    "Models/space-shooter-remastered/PNG",
)
SPRITE_FORMATS = ("png",)
CATALOG_RELPATH = Path("unity") / "asset_catalog.json"
CURATED_RELPATH = Path("unity") / "kenney_catalog.json"
# Frozen Sketchfab picks (src/sketchfab_freeze.py writes this) — merged into
# the "assets" list by load_catalog(), never scanned from disk here. Kept in
# its own file so a Sketchfab-less checkout's asset_catalog.json stays
# byte-identical to today, and resetting the Sketchfab cache is a single-file
# delete.
SKETCHFAB_RELPATH = Path("unity") / "sketchfab_catalog.json"
CATALOG_PROVENANCE_FORMAT = "unity-asset-catalog/provenance/v1"

# Directories that never contain spawnable scene models: shared textures,
# animation clip libraries, and the static default pack (already covered by
# default_models.json).
_SKIP_DIRS = frozenset({"textures", "players", "defaults"})

# First filename token → catalog category. Unmapped tokens fall back to "prop".
_CATEGORY_MAP: dict[str, str] = {
    "tree":       "nature",
    "rock":       "nature",
    "plant":      "nature",
    "floor":      "structure",
    "structure":  "structure",
    "steps":      "structure",
    "wall":       "structure",
    "support":    "structure",
    "ramp":       "track",
    "rail":       "track",
    "bowl":       "track",
    "bend":       "track",
    "curve":      "track",
    "helix":      "track",
    "funnel":     "track",
    "slant":      "track",
    "split":      "track",
    "straight":   "track",
    "wave":       "track",
    "bump":       "track",
    "tunnel":     "track",
    "cross":      "track",
    "corner":     "track",
    "end":        "track",
    "half":       "track",
    "s":          "track",
    "marble":     "track",
    "fan":        "mechanism",
    "banner":     "prop",
    "pallet":     "prop",
    "obstacle":   "obstacle",
    "character":  "character",
    "skateboard": "vehicle",
    # dungeon kit
    "corridor":   "structure",
    "room":       "structure",
    "gate":       "structure",
    "door":       "structure",
    "template":   "structure",
    "stairs":     "structure",
    # racing kit
    "fence":      "structure",
    "barrier":    "structure",
    "grand":      "structure",    # grandStand* (camelCase → first token "grand")
    "tower":      "structure",
    "billboard":  "prop",
    "flag":       "prop",
    "grass":      "nature",
    # nature kit
    "cliff":      "nature",
    "stone":      "nature",
    "ground":     "nature",
    "crops":      "nature",
    "flower":     "nature",
    "stump":      "nature",
    "log":        "nature",
    "mushroom":   "nature",
    "bridge":     "structure",
    "path":       "structure",
    "terrain":    "nature",
    # car kit
    "debris":     "obstacle",
    "cone":       "obstacle",
    "wheel":      "prop",
    "car":        "vehicle",
    "kart":       "vehicle",
    "tractor":    "vehicle",
    "truck":      "vehicle",
    "suv":        "vehicle",
    "sedan":      "vehicle",
    "van":        "vehicle",
    "taxi":       "vehicle",
    "ambulance":  "vehicle",
    "police":     "vehicle",
    "race":       "vehicle",
    "delivery":   "vehicle",
    "firetruck":  "vehicle",
    "hatchback":  "vehicle",
    # space kit
    "pipe":       "structure",
    "platform":   "structure",
    "monorail":   "track",
    "rocket":     "vehicle",
    "craft":      "vehicle",
    "hangar":     "structure",
    "machine":    "mechanism",
    "satellite":  "mechanism",
    "radar":      "mechanism",
    "alien":      "character",
    "astronaut":  "character",
    "desk":       "prop",
    "chair":      "prop",
    # city kit
    "building":   "structure",
    "driveway":   "structure",
    "planter":    "prop",
    # aviation (loose CC0 models — the skydiving gap)
    "parachute":  "vehicle",
    "avion":      "vehicle",
    "corsair":    "vehicle",
    "plane":      "vehicle",
    "jet":        "vehicle",
    "helicopter": "vehicle",
    # watercraft kit
    "boat":       "vehicle",
    "ship":       "vehicle",
    "canoe":      "vehicle",
    "raft":       "vehicle",
    "sailboat":   "vehicle",
    # weapon kit
    "blaster":    "weapon",
    "sword":      "weapon",
    "gun":        "weapon",
    "rifle":      "weapon",
    "pistol":     "weapon",
    "dagger":     "weapon",
    "axe":        "weapon",
    "bow":        "weapon",
    "shield":     "weapon",
}

# Token → extra keywords, so entity names like "PineForest" or "GrindRail"
# overlap the derived keyword sets without curated entries.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "tree":       ("forest", "pine", "wood", "nature", "plant", "foliage"),
    "rock":       ("stone", "boulder", "nature"),
    "floor":      ("ground", "surface", "tile", "terrain", "base"),
    "structure":  ("platform", "frame", "building", "scaffold"),
    "steps":      ("stairs", "staircase", "climb", "elevation"),
    "wall":       ("barrier", "fence", "divider"),
    "support":    ("pillar", "column", "post", "scaffold", "beam"),
    "ramp":       ("slope", "incline", "jump", "skate", "quarter"),
    "rail":       ("grind", "fence", "bar", "handrail"),
    "bowl":       ("pool", "skatepark", "basin"),
    "bend":       ("turn", "curve", "corner"),
    "curve":      ("turn", "bend", "arc"),
    "helix":      ("spiral", "corkscrew", "loop"),
    "funnel":     ("cone", "vortex"),
    "slant":      ("slope", "incline", "diagonal"),
    "split":      ("fork", "branch", "junction"),
    "straight":   ("track", "lane", "segment"),
    "wave":       ("ripple", "undulating"),
    "bump":       ("mound", "hill", "hump"),
    "tunnel":     ("pipe", "tube", "passage", "underground"),
    "cross":      ("intersection", "junction", "crossing"),
    "corner":     ("turn", "edge", "bend"),
    "end":        ("cap", "terminus", "finish"),
    "half":       ("halfpipe", "pipe", "skate"),
    "marble":     ("ball", "sphere", "toy"),
    "fan":        ("spinner", "propeller", "rotor", "windmill"),
    "banner":     ("flag", "sign", "pennant", "decoration"),
    "arch":       ("archway",),
    "pallet":     ("crate", "box", "warehouse", "cargo"),
    "obstacle":   ("barrier", "block", "hazard", "cover"),
    "character":  ("person", "human", "npc", "skater", "figure"),
    "skateboard": ("board", "deck", "ride"),
    "solid":      (),          # structural modifier — keep token, no expansion
    "hole":       ("gap", "opening"),
    "hanging":    ("suspended", "overhead"),
    "standing":   ("upright",),
    "color":      ("colored", "painted"),
    # dungeon kit
    "corridor":   ("hall", "hallway", "passage", "dungeon", "interior"),
    "room":       ("chamber", "hall", "interior", "dungeon"),
    "gate":       ("door", "portal", "entrance", "gateway"),
    "door":       ("gate", "entrance", "doorway"),
    "template":   ("wall", "floor", "modular", "dungeon", "tile"),
    "stairs":     ("steps", "staircase", "climb", "elevation"),
    # racing kit
    "fence":      ("barrier", "rail", "wall", "railing"),
    "barrier":    ("wall", "fence", "block", "obstacle"),
    "grand":      ("grandstand", "stands", "bleachers", "seating", "audience"),
    "billboard":  ("sign", "advertisement", "display", "signage"),
    "flag":       ("banner", "checkered", "marker", "pennant"),
    # NOTE: no "field" — too ambiguous ("DebrisFields" grabbed grass.fbx live)
    "grass":      ("lawn", "ground", "turf"),
    # nature kit
    "cliff":      ("rock", "mountain", "ledge", "crag"),
    "stone":      ("rock", "boulder", "pebble"),
    "ground":     ("terrain", "floor", "dirt", "earth", "tile"),
    "crops":      ("farm", "wheat", "corn", "plant", "harvest"),
    "flower":     ("plant", "bloom", "garden"),
    "stump":      ("tree", "log", "wood"),
    "log":        ("wood", "timber", "trunk"),
    "mushroom":   ("fungus", "toadstool", "forest"),
    "bridge":     ("crossing", "span", "walkway"),
    "path":       ("road", "trail", "walkway", "pavement"),
    "terrain":    ("ground", "land", "surface"),
    # car kit
    "debris":     ("wreckage", "wreck", "fragment", "scrap", "junk", "rubble"),
    "cone":       ("traffic", "marker", "hazard"),
    "wheel":      ("tire", "tyre", "rim"),
    "kart":       ("car", "racer", "gokart"),
    "tractor":    ("farm", "vehicle"),
    "truck":      ("lorry", "vehicle", "cargo"),
    "taxi":       ("cab", "car"),
    "ambulance":  ("medical", "emergency", "rescue"),
    "police":     ("cop", "patrol", "emergency"),
    "race":       ("racing", "racer", "sports"),
    # space kit
    "pipe":       ("tube", "conduit", "duct"),
    "platform":   ("ledge", "walkway", "deck"),
    # NOT "rail": a monorail train car is a vehicle, not a railing. The synonym
    # made every monorail_* carriage a strong candidate for any kind="rail"
    # entity and a "deck rail" resolved to monorail_trainPassenger.
    "monorail":   ("train", "transit"),
    "rocket":     ("spaceship", "ship", "missile", "launch"),
    "craft":      ("spaceship", "ship", "shuttle", "spacecraft"),
    "hangar":     ("garage", "bay", "shed"),
    "machine":    ("device", "generator", "equipment"),
    "satellite":  ("dish", "antenna", "orbital"),
    "radar":      ("dish", "antenna", "scanner"),
    "alien":      ("creature", "monster", "extraterrestrial", "npc"),
    "astronaut":  ("spaceman", "cosmonaut", "person", "npc"),
    # city kit
    "building":   ("house", "home", "structure", "suburban"),
    "driveway":   ("road", "pavement", "entrance"),
    "planter":    ("pot", "garden", "plant"),
    # aviation
    "parachute":  ("chute", "canopy", "skydive", "skydiver", "glider", "wingsuit"),
    "avion":      ("plane", "aircraft", "airplane"),
    "corsaire":   ("corsair", "plane", "fighter", "aircraft"),
    "corsair":    ("plane", "fighter", "aircraft", "warplane", "airplane"),
    "army":       ("military", "camo", "soldier"),
    # watercraft kit
    "boat":       ("ship", "watercraft", "vessel", "water"),
    "canoe":      ("boat", "paddle", "kayak"),
    "raft":       ("boat", "float"),
    # weapon kit
    "blaster":    ("gun", "weapon", "laser", "pistol", "shooter"),
    "sword":      ("blade", "weapon", "melee"),
    "gun":        ("weapon", "firearm", "shooter"),
    "rifle":      ("gun", "weapon", "firearm"),
    "shield":     ("armor", "defense", "weapon"),
}


def _source_tree_fingerprint(entries: Iterable[dict]) -> str:
    """Hash source paths and bytes without bloating every catalog row."""
    records: list[tuple[str, str, str]] = []
    for entry in entries:
        root = str(entry.get("root") or "")
        relpath = str(entry.get("relpath") or "")
        path = Path(root) / relpath
        records.append((root, relpath, fhash(path)))
    return chash("asset", {"sources": sorted(records)})


def _augment_keywords(keywords: Iterable[str]) -> list[str]:
    """Add deterministic visual aliases without changing authored nouns.

    The asset filenames are sometimes tokenized as two words while a visual
    inventory uses their normal compound noun (``boat-speed`` ->
    ``speedboat``).  Keep both forms in the catalog index.  This is an index
    repair, not a rewrite of the visual inventory or of a Sketchfab query.
    """
    result: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    for value in keywords:
        add(str(value))

    tokens = {token for value in result for token in tokenize(value)}
    if "arch" in tokens:
        add("archway")
    if {"boat", "speed"} <= tokens:
        add("speedboat")
    return result

# Category → entity kinds (default_models.json vocabulary) the asset can serve.
_KIND_HINTS: dict[str, tuple[str, ...]] = {
    "nature":    ("tree",),
    "structure": ("building", "ground", "prop"),
    "track":     ("ramp", "rail", "road", "obstacle"),
    "mechanism": ("prop", "hazard"),
    "prop":      ("prop",),
    "obstacle":  ("obstacle", "hazard"),
    "character": ("npc", "crowd"),
    "vehicle":   ("vehicle",),
    "weapon":    ("pickup", "item", "prop"),
}

# First token → kind hints that are more specific than the category default.
_TOKEN_KIND_HINTS: dict[str, tuple[str, ...]] = {
    "ramp":     ("ramp",),
    "rail":     ("rail",),
    "floor":    ("ground", "road"),
    "steps":    ("prop", "building"),
    "tree":     ("tree",),
    "fence":    ("obstacle", "prop"),
    "barrier":  ("obstacle", "hazard"),
    "grass":    ("ground",),
    "debris":   ("obstacle", "hazard", "prop"),
    "building": ("building",),
    "ground":   ("ground", "road"),
    "terrain":  ("ground", "road"),
    "path":     ("road", "ground"),
    # Monorail pieces are transit track and carriages — never railings. Without
    # this the category default ("track") hands them kind_hint "rail", and the
    # +0.5 kind bonus let a train carriage outscore every real rail model.
    "monorail": ("road", "prop"),
    "alien":    ("enemy", "npc"),
    "astronaut": ("player", "npc"),
    "parachute": ("vehicle", "prop", "pickup"),
    "avion":     ("vehicle",),
    "corsair":   ("vehicle",),
    "boat":      ("vehicle",),
}

# Category → default import scale. Kenney packs are authored around 1 unit;
# marble-track pieces read better slightly larger next to a 1.8u character.
_CATEGORY_SCALE: dict[str, float] = {}

# ── 2D sprite vocabulary (FishPack and future sprite packs) ───────────────────

_SPRITE_CATEGORY_MAP: dict[str, str] = {
    "fish":       "creature",
    "bubble":     "fx",
    "seaweed":    "vegetation",
    "rock":       "environment",
    "terrain":    "environment",
    "background": "background",
    "hud":        "ui",
    # 2D space packs (2d-space-kit / space-shooter-*): filenames are often
    # prefixed "space..." so category comes from the FIRST MAPPED token.
    "ship":       "ship",
    "ships":      "ship",
    "craft":      "ship",
    "rocket":     "ship",
    "cockpit":    "ship",
    "wing":       "ship",
    "ufo":        "ship",
    "turret":     "ship",
    "laser":      "fx",
    "missile":    "fx",
    "bolt":       "fx",
    "effect":     "fx",
    "meteor":     "environment",
    "asteroid":   "environment",
    "pipe":       "environment",
    "corridor":   "environment",
    "platform":   "environment",
    "hangar":     "environment",
    "machine":    "environment",
    "monorail":   "environment",
    "star":       "background",
    "astronaut":  "creature",
    "astronauts": "creature",
    "player":     "creature",
    "enemy":      "creature",
    "alien":      "creature",
}

_SPRITE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "fish":       ("swim", "sea", "ocean", "underwater", "creature", "animal", "enemy"),
    "bubble":     ("air", "water", "pop", "float"),
    "seaweed":    ("plant", "kelp", "underwater", "vegetation", "grass"),
    "rock":       ("stone", "boulder", "obstacle"),
    "terrain":    ("ground", "floor", "sand", "dirt", "platform", "tile"),
    "background": ("backdrop", "scenery", "distant"),
    "hud":        ("ui", "icon", "number", "interface"),
    "skeleton":   ("bone", "dead", "spooky"),
    "outline":    (),
}

_SPRITE_KIND_HINTS: dict[str, tuple[str, ...]] = {
    "creature":    ("enemy", "npc", "crowd"),
    "ship":        ("player", "enemy", "vehicle"),
    "fx":          ("prop",),
    "vegetation":  ("tree", "prop"),
    "environment": ("ground", "obstacle", "prop"),
    "background":  ("prop",),
    "ui":          (),          # HUD digits/icons are not spawnable scene content
}


# Tokens that describe size/variant, not semantics — excluded from keywords.
# Single-character tokens are always noise (variant letters, the "x" left over
# from dimension names like fence-1x2 — which matched an entity named "X" live).
_NOISE_TOKENS = frozenset({
    "large", "medium", "small", "wide", "long", "short",
    "high", "low", "left", "right", "inner", "outer", "single", "double",
    "two", "four", "top", "middle", "bottom", "simple", "quarter", "sides",
})


def _is_noise(tok: str) -> bool:
    return len(tok) < 2 or tok in _NOISE_TOKENS


def tokenize(stem: str) -> list[str]:
    """Split a filename stem (or entity name) into lowercase word tokens."""
    # camelCase / PascalCase → separate words, then split on -, _, spaces, digits
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    return [t for t in re.split(r"[-_\s.\d]+", spaced.lower()) if t]


def scan_assets(roots: Iterable[Path],
                formats: tuple[str, ...] = DEFAULT_FORMATS) -> list[tuple[Path, Path]]:
    """Return sorted (file, root) pairs for every model file under the roots.

    Skips Textures/Players/Defaults subtrees and non-model files. Sorting by
    relpath makes the result independent of os.walk order.
    """
    exts = {f".{f.lower().lstrip('.')}" for f in formats}
    found: list[tuple[Path, Path]] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            rel_parts = path.relative_to(root).parts[:-1]
            if any(p.lower() in _SKIP_DIRS for p in rel_parts):
                continue
            found.append((path, root))
    found.sort(key=lambda pair: (pair[0].relative_to(pair[1]).as_posix(),
                                 pair[1].as_posix()))
    return found


def derive_entry(path: Path, root: Path) -> dict:
    """Derive a catalog entry from a model file's name and location."""
    tokens = tokenize(path.stem)
    first = tokens[0] if tokens else "prop"
    category = _CATEGORY_MAP.get(first, "prop")

    keywords: list[str] = []
    seen: set[str] = set()

    def _add(word: str) -> None:
        if word and word not in seen:
            seen.add(word)
            keywords.append(word)

    for tok in tokens:
        if not _is_noise(tok):
            _add(tok)
        for syn in _SYNONYMS.get(tok, ()):
            _add(syn)
    keywords = _augment_keywords(keywords)
    _add(category)

    kind_hints = list(_TOKEN_KIND_HINTS.get(first, _KIND_HINTS.get(category, ("prop",))))

    return {
        "filename": path.name,
        "relpath": path.relative_to(root).as_posix(),
        "root": root.as_posix(),
        "format": path.suffix.lstrip(".").lower(),
        "pack": _pack_for_root(root),
        "source": "kenney",
        "category": category,
        "description": f"{' '.join(tokens)} — {category} model",
        "keywords": keywords,
        "kind_hints": kind_hints,
        "game_contexts": ["any"],
        "generic_score": 0,
        "scale": _CATEGORY_SCALE.get(category, 1.0),
        "curated": False,
    }


def derive_sprite_entry(path: Path, root: Path) -> dict:
    """Derive a catalog entry for a 2D sprite (PNG).

    Category comes from the first MAPPED token, not the literal first token —
    space packs prefix everything with the pack name ("spaceShips_001"), so
    the meaningful family word is usually second.
    """
    tokens = tokenize(path.stem)
    category = next((_SPRITE_CATEGORY_MAP[t] for t in tokens
                     if t in _SPRITE_CATEGORY_MAP), "prop")

    keywords: list[str] = []
    seen: set[str] = set()

    def _add(word: str) -> None:
        if word and word not in seen:
            seen.add(word)
            keywords.append(word)

    for tok in tokens:
        if not _is_noise(tok):
            _add(tok)
        for syn in _SPRITE_SYNONYMS.get(tok, ()):
            _add(syn)
    keywords = _augment_keywords(keywords)
    _add(category)

    return {
        "filename": path.name,
        "relpath": path.relative_to(root).as_posix(),
        "root": root.as_posix(),
        "format": path.suffix.lstrip(".").lower(),
        "pack": _pack_for_root(root),
        "source": "kenney",
        "category": category,
        "description": f"{' '.join(tokens)} — 2D {category} sprite",
        "keywords": keywords,
        "kind_hints": list(_SPRITE_KIND_HINTS.get(category, ("prop",))),
        "game_contexts": ["2d"],
        "generic_score": 0,
        "scale": 1.0,
        "curated": False,
    }


def _pack_for_root(root: Path) -> str:
    """Pack label from the root path (packs are merged on disk).

    Prefers the component right after "Models" so nested roots like
    Models/FishPack/PNG/Default label as the pack (fishpack), not the
    resolution folder (default).
    """
    parts = [p.lower() for p in root.parts]
    name = root.name.lower()
    if "models" in parts:
        after = parts[parts.index("models") + 1:]
        if after:
            name = after[0]
    name = name.replace(" ", "_")
    return f"kenney_{name}" if "kenney" not in name else name


def _load_curated(curated_path: Path) -> dict[str, dict]:
    """Filename → curated entry from the hand-written kenney catalog."""
    try:
        data = json.loads(curated_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {a["filename"]: a for a in data.get("assets", []) if "filename" in a}


# Curated fields that override derived values when present.
_CURATED_FIELDS = ("category", "keywords", "kind_hints", "game_contexts",
                   "description", "generic_score", "scale", "pack")


def build_catalog(roots: Iterable[str | Path],
                  curated_path: Optional[Path] = None,
                  formats: tuple[str, ...] = DEFAULT_FORMATS,
                  sprite_roots: Iterable[str | Path] = ()) -> dict:
    """Scan the roots and return the full catalog dict (stable ordering).

    3D model roots and 2D sprite roots share one catalog — entries carry a
    `format` field (fbx vs png) so the resolver can filter by game type.
    """
    root_paths = [Path(r) for r in roots]
    sprite_root_paths = [Path(r) for r in sprite_roots]
    curated = _load_curated(curated_path) if curated_path else {}

    assets: list[dict] = []
    for path, root in scan_assets(root_paths, formats):
        entry = derive_entry(path, root)
        cur = curated.get(entry["filename"])
        if cur:
            for field in _CURATED_FIELDS:
                if field in cur:
                    entry[field] = cur[field]
            entry["keywords"] = _augment_keywords(entry.get("keywords") or [])
            entry["curated"] = True
        assets.append(entry)

    for path, root in scan_assets(sprite_root_paths, SPRITE_FORMATS):
        assets.append(derive_sprite_entry(path, root))

    categories = sorted({a["category"] for a in assets})
    tag_counts: Counter[str] = Counter()
    for a in assets:
        tag_counts.update(a["keywords"])
    # Top tags by frequency, ties broken alphabetically → deterministic.
    top_tags = [t for t, _ in sorted(tag_counts.items(),
                                     key=lambda kv: (-kv[1], kv[0]))[:40]]

    return {
        "version": CATALOG_VERSION,
        "roots": sorted(p.as_posix() for p in root_paths),
        "provenance": {
            "format": CATALOG_PROVENANCE_FORMAT,
            "builder": "src.unity_asset_catalog.build_catalog",
            "model_roots": sorted(p.as_posix() for p in root_paths),
            "sprite_roots": sorted(p.as_posix() for p in sprite_root_paths),
            "model_formats": list(formats),
            "sprite_formats": list(SPRITE_FORMATS),
            "curated_catalog": curated_path.as_posix() if curated_path else None,
            "asset_count": len(assets),
            "source_tree_fingerprint": _source_tree_fingerprint(assets),
            "category_counts": {
                category: sum(1 for asset in assets if asset["category"] == category)
                for category in categories
            },
        },
        "vocabulary": {"categories": categories, "tags": sorted(top_tags)},
        "assets": assets,
    }


def catalog_json(catalog: dict) -> str:
    """Serialize with stable key order — byte-idempotent output."""
    return json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_catalog(catalog: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(catalog_json(catalog), encoding="utf-8")
    return out_path


def is_stale(catalog: dict, out_path: Path) -> bool:
    """True when the on-disk catalog differs from a fresh build."""
    try:
        return out_path.read_text(encoding="utf-8") != catalog_json(catalog)
    except FileNotFoundError:
        return True


def load_catalog(lookups_dir: str = "lookups", *,
                 sketchfab_catalog_path: str | Path | None = None) -> dict:
    """Read the generated catalog (falling back to the curated kenney catalog,
    whose entries share the same shape, so old checkouts keep working), then
    merge in the frozen Sketchfab partition if one exists.

    ``sketchfab_catalog_path`` is an optional explicit frozen partition.  It
    supports a shared, already-verified Sketchfab cache outside a disposable
    feature worktree without copying its large binary library into that
    worktree.  When supplied it replaces the local partition path; an invalid
    explicit path is a configuration error rather than a silent fallback to
    unrelated local assets.  The default remains the local
    ``lookups/unity/sketchfab_catalog.json`` path byte-for-byte.

    The Sketchfab merge is additive-only: entries loaded from asset_catalog.json
    / kenney_catalog.json are tagged source="kenney" when the field is absent
    (pre-existing catalogs predate the `source` key), and sketchfab_catalog.json
    entries (already tagged source="sketchfab" by write_sketchfab_entry) are
    appended. `vocabulary` is deliberately left untouched by the merge — it
    feeds the LLM schema-fill prompt (see asset_vocabulary()) and stays scoped
    to the stable, pre-generated Kenney vocabulary so per-project live-fetched
    content never churns that prompt.
    """
    base: dict = {}
    for rel in (CATALOG_RELPATH, CURATED_RELPATH):
        path = Path(lookups_dir) / rel
        try:
            base = json.loads(path.read_text(encoding="utf-8"))
            break
        except (FileNotFoundError, json.JSONDecodeError):
            continue

    sk_entries: list[dict] = []
    sk_path = (Path(sketchfab_catalog_path)
               if sketchfab_catalog_path is not None
               else Path(lookups_dir) / SKETCHFAB_RELPATH)
    try:
        sk = json.loads(sk_path.read_text(encoding="utf-8"))
        sk_entries = list(sk.get("entries", []))
    except (FileNotFoundError, json.JSONDecodeError):
        if sketchfab_catalog_path is not None:
            raise ValueError(
                f"explicit frozen Sketchfab catalog is unavailable or invalid: {sk_path}"
            )

    # Neither file exists — preserve the pre-Sketchfab contract of returning
    # {} exactly (callers/tests treat that as "no catalog", not "empty one").
    if not base and not sk_entries:
        return {}

    assets = list(base.get("assets", []))
    for a in assets:
        a.setdefault("source", "kenney")
    assets.extend(sk_entries)

    base["assets"] = assets
    return base


def write_sketchfab_entry(entry: dict, out_path: Path) -> dict:
    """Append (or replace, keyed by sketchfab_uid) one frozen Sketchfab pick
    into the on-disk sketchfab_catalog.json, and return the full catalog dict
    that was written.

    Read-merge-write, not a blind append: re-fetching the same Sketchfab model
    (same `sketchfab_uid`) updates its entry in place instead of duplicating
    it. Serialized with the same byte-idempotent discipline as catalog_json()
    (sort_keys, no ambient timestamps beyond the entry's own `fetched_at`) so
    running the same fetch twice in a row produces identical bytes.
    """
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"version": CATALOG_VERSION, "entries": []}

    entries = [e for e in data.get("entries", [])
              if e.get("sketchfab_uid") != entry.get("sketchfab_uid")]
    entries.append(entry)
    entries.sort(key=lambda e: e.get("sketchfab_uid", ""))
    data["entries"] = entries
    data["version"] = CATALOG_VERSION

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return data


def asset_vocabulary(lookups_dir: str = "lookups") -> str:
    """Compact asset-library vocabulary for the schema-fill prompt.

    Keeps the LLM's view small (categories + top tags, not 580 filenames) so
    it can emit useful entity kinds / model_hints without prompt bloat.
    Missing catalog → empty string (prompt unchanged).
    """
    catalog = load_catalog(lookups_dir)
    vocab = catalog.get("vocabulary") or {}
    categories = vocab.get("categories") or []
    tags = vocab.get("tags") or []
    if not categories and not tags:
        return ""
    lines = ["ASSET LIBRARY — set each entity's model_hint to 2-5 plain words "
             "matching a real model. Vocabulary:"]
    if categories:
        lines.append(f"  categories: {', '.join(categories)}")
    if tags:
        lines.append(f"  tags: {', '.join(tags)}")
    return "\n".join(lines)


if __name__ == "__main__":   # pragma: no cover
    # This is a library module — running it directly used to do nothing,
    # silently. Point at the real entry point instead.
    import sys
    sys.exit("unity_asset_catalog.py is a library. Rebuild the catalog with:\n"
             "  python main.py unity-catalog            # scan + write\n"
             "  python main.py unity-catalog --dry-run  # stats only\n"
             "  python main.py unity-catalog --check    # CI staleness gate")
