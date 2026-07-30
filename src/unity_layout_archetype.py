"""
Layout archetype selection (pipeline Step 5, immediately ahead of the layout
pass) — decides WHICH spatial primitive a schema's content belongs in, instead
of packing every game onto the one ground plane src/unity_layout.py has always
assumed.

The bug this closes
-------------------
`compute_layout()` packs non-overlapping entity footprints on a ground plane
around the scene origin, regardless of what kind of game the schema describes.
A skydiving schema — altitude-banded entities, a player who exits an aircraft
at 4 km and falls — came out flattened: every group at y = 0, the player
standing on the landing pad at t=0, nothing left to fall through. The geometry
was fine; the *primitive* was wrong.

The selection is a pure, deterministic read of the schema — no RNG, no LLM, no
I/O — producing three axes:

  motion_axis   vertical | volumetric | planar | path
                                           how the player's own motion is
                                           constrained
  medium        air | water | ground       what the space is made of
  goal_topology reach_point | survive_area | hit_target | traverse

and then a table lookup:

  (vertical,   air)    -> VerticalCorridor
  (vertical,   water)  -> VerticalCorridor
  (volumetric, air)    -> BoundedVolume
  (volumetric, water)  -> BoundedVolume
  (planar,     ground) -> ArenaPlane       (the existing packer, unchanged)
  anything else        -> ABSTAIN, hard-fail

`volumetric` is the free-medium axis: the player moves in all three axes
through air or water with no descent framing — a glider, a flight sim, a scuba
swim. It exists because the first cut derived `planar` for those and abstained,
which hard-failed projects that used to produce (bad) output. Descent evidence
still wins: a schema with freefall/dive/altitude-band content is `vertical` and
gets the corridor, unchanged.

ABSTAIN stays deliberate and load-bearing, and adding BoundedVolume did NOT
turn it into a default. A walking game whose world happens to be described in
sky vocabulary (planar + air), a `vertical` descent through solid ground, and
every path-axis rail game still have no honest home here, and quietly dropping
them onto ArenaPlane is precisely the failure this module exists to stop. It
raises `[layout.strict]` naming the unmatched axes — same doctrine, same
message shape, as `[assets.strict]` in src/unity_entity_asset_resolver.py.

Evidence thresholds
-------------------
Misclassifying a working ground game as "vertical" would hard-fail it, so
vertical evidence has to be STRONG: one unambiguous term (freefall, skydive,
parachute, wingsuit, altimeter, …), or two weaker descent verbs in a scene
that is already air/water, or real altitude data on the entities. A lone
"descent into the caves" never flips the axis.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence, TypeVar


class _EntityLike(Protocol):
    """The four fields every role predicate here reads.

    This module deliberately does not import EntitySpec — the axes and roles it
    derives are engine- and schema-shape-agnostic. The protocol keeps callers'
    concrete types intact while still type-checking the attribute access.
    """
    name:         str
    kind:         str
    model_hint:   str
    behavior:     str
    scale:        float
    group_radius: float


_Entity = TypeVar("_Entity", bound=_EntityLike)

# ── Evidence vocabularies ────────────────────────────────────────────────────
# Deliberately conservative. Every word here can flip a project from "packs
# fine" to "hard-fails", so ambiguous stems (bare "plane", "depth", "wreck")
# are left OUT even though they read as on-topic.

# One of these alone is enough: no ground game says "wingsuit".
_VERTICAL_STRONG: tuple[str, ...] = (
    "freefall", "free fall", "free-fall", "skydive", "skydives", "skydiving",
    "skydiver", "skydivers", "sky dive", "parachute", "parachutes",
    "parachuting", "wingsuit", "base jump", "basejump", "halo jump",
    "terminal velocity", "altimeter", "hard deck", "hard-deck", "drop zone",
    "dropzone", "pull altitude", "pull-altitude", "deploy altitude",
    "canopy flight", "bungee",
)
# Ambiguous on their own ("descent into the catacombs", "dive for cover") —
# two of them, in a scene already established as air or water, count.
_VERTICAL_WEAK: tuple[str, ...] = (
    "descend", "descends", "descending", "descent", "plummet", "plummets",
    "plunge", "plunges", "falling", "fall from", "falls from", "jump from",
    "jumps from", "leap from", "leaps from", "drop from", "drops from",
    "dive", "dives", "diving", "downward", "straight down", "sinking",
)
_AIR_WORDS: tuple[str, ...] = (
    "sky", "skies", "airborne", "aircraft", "airplane", "aeroplane", "biplane",
    "jet", "helicopter", "altitude", "freefall", "free fall", "skydive",
    "skydiving", "skydiver", "parachute", "canopy", "wingsuit", "glide",
    "glider", "gliding", "cloud", "clouds", "aerial", "flight", "flying",
    "stratosphere", "jump ship", "thermals", "jet stream",
    "updraft", "midair", "mid-air",
)
# NOT in that list: "atmosphere"/"atmospheric". They are mood and rendering
# vocabulary ("atmospheric scattering", "a tense atmosphere"), not a claim that
# the player is in the air. Live: a CLIMBING game — player on a rock face,
# medium plainly ground — derived medium='air' off the single word
# "atmosphere" in its concept, landed on planar+air, and hard-failed
# [layout.strict] on a build that should simply have packed. Same landmine
# class as the bare "drift" exclusion in src/unity_archetypes.py's racing
# table: one ambiguous word must never carry an axis on its own.
_WATER_WORDS: tuple[str, ...] = (
    "underwater", "undersea", "submarine", "submersible", "scuba", "reef",
    "coral", "ocean", "oceanic", "sea floor", "seafloor", "seabed", "lagoon",
    "abyss", "trench", "kelp", "aquatic", "submerged", "depths", "water",
    "lake", "river", "swim", "swims", "swimming", "diver", "divers",
    "diving bell", "buoyancy", "tide", "surf",
)
# The subset of the two lists above that states the MEDIUM outright rather
# than setting the scene. "sky", "cloud", "ocean", "abyss", "atmosphere" all
# describe a view; "freefall", "wingsuit", "scuba", "underwater" describe where
# the player's body is. Only these override a surface `locomotion` — see
# `_derive_medium` for the two live hard-failures that drew the line here.
_AIR_STRONG: tuple[str, ...] = (
    "freefall", "free fall", "free-fall", "skydive", "skydives", "skydiving",
    "skydiver", "skydivers", "parachute", "parachuting", "wingsuit",
    "altimeter", "airborne", "midair", "mid-air", "jump ship", "stratosphere",
    "base jump", "basejump", "halo jump", "terminal velocity",
)
_WATER_STRONG: tuple[str, ...] = (
    "underwater", "undersea", "scuba", "submarine", "submersible",
    "submerged", "seabed", "sea floor", "seafloor", "reef", "kelp",
    "diving bell", "aquatic",
)
# ── Ascent ───────────────────────────────────────────────────────────────────
# Descent moves THROUGH a volume; ascent moves ALONG a surface with things
# attached to it. Different primitives, so `vertical` carries a direction.
#
# Unambiguous physical climbing — strong enough to establish the AXIS, the same
# bar `_VERTICAL_STRONG` clears for descent. Nothing here has a common
# figurative reading: you cannot "handhold" your way up a leaderboard.
_ASCENT_STRONG: tuple[str, ...] = (
    "handhold", "handholds", "hand hold", "hand holds", "foothold",
    "footholds", "belay", "belays", "piton", "pitons", "bouldering",
    "boulderer", "free solo", "freesolo", "free climb", "freeclimb",
    "rock face", "cliff face", "climbing wall", "climbing route",
    "climbing game", "crimp", "crimps", "top out", "topping out",
    "summit push", "via ferrata", "chalk bag",
)
# Direction verbs. Deliberately NOT axis-establishing: "climb the leaderboard"
# and "sales are climbing" are ordinary English, and a walking game that says
# either must not become a climbing game. These only choose a direction once
# the axis is already vertical.
_ASCENT_VERBS: tuple[str, ...] = (
    "ascend", "ascends", "ascending", "ascent", "scale", "scales", "scaling",
    "climb", "climbs", "climbing", "climbed", "climber", "climbers",
    "clamber", "clambers", "summit", "summits", "upward", "upwards",
    "up the wall", "up the face",
)
_DESCENT_VERBS: tuple[str, ...] = (
    "freefall", "free fall", "descend", "descends", "descending", "descent",
    "drop", "drops", "dropping", "plummet", "plummets", "dive", "dives",
    "diving", "fall", "falls", "falling", "downward", "downwards", "sink",
    "sinks", "sinking", "rappel", "abseil",
)
# Structured per-entity climbing evidence — a tag or kind that says this group
# IS something you grab. Strong: it is authored structure, not prose.
_CLIMB_TAGS: frozenset[str] = frozenset({
    "holdable", "climbable", "grabbable", "handhold", "hold",
})
_CLIMB_KINDS: frozenset[str] = frozenset({
    "handhold", "hold", "climbable", "ledge", "grip",
})
# Things that hang on the wall to hurt you. They interleave with the holds but
# never count toward the reachable chain.
_HAZARD_TAGS: frozenset[str] = frozenset({"enemy", "hazard", "trap", "danger"})
_HAZARD_KINDS: frozenset[str] = frozenset({
    "enemy", "hazard", "obstacle", "trap", "creature",
})
# Free-medium locomotion the schema's own `locomotion` enum cannot express.
# UnitySchema.locomotion normalises to walk/drive/fly/swim, so "hover" and
# "float" arrive as prose or not at all — read them here rather than widen the
# enum, which would ripple through controller composition.
_FREE_MEDIUM_WORDS: tuple[str, ...] = (
    "hover", "hovers", "hovering", "hovercraft", "float", "floats",
    "floating", "levitate", "levitates", "levitating", "weightless",
    "zero gravity", "zero-g", "anti-gravity", "antigravity", "drift",
    "drifts", "drifting",
)
# Only an explicit rail constrains the player to a route. A racing circuit does
# NOT: the driver moves freely on a surface, which is why racetracks stay an
# ArenaPlane concern (their corridor packing already lives there) until the
# spline primitive lands.
_PATH_WORDS: tuple[str, ...] = (
    "on rails", "on-rails", "rail shooter", "rail-shooter", "railshooter",
    "endless runner", "auto-runner", "autorunner", "auto runner",
    "fixed route", "rail segment", "rides the rail",
)

_GOAL_WORD_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("reach_point", ("landing pad", "landing zone", "drop zone", "dropzone",
                     "landing", "touch down", "touchdown", "reach the",
                     "extraction", "extract", "finish line", "bullseye",
                     "precision landing", "goal", "exit point", "escape pod")),
    ("hit_target",  ("destroy", "destroys", "shoot", "shoots", "shooting",
                     "eliminate", "eliminates", "kill", "kills", "defeat",
                     "defeats", "take down", "takedown", "hit the target",
                     "score a hit", "gun down")),
    ("survive_area", ("survive", "survives", "survival", "wave", "waves",
                      "defend", "defends", "hold out", "hold off", "arena",
                      "last as long", "endure", "horde")),
    ("traverse",    ("race", "races", "racing", "lap", "laps", "circuit",
                     "course", "traverse", "traverses", "navigate",
                     "navigates", "reach the end", "cross the")),
)
# MissionObjective.type is authored structure, not prose — it outranks words.
_OBJECTIVE_TOPOLOGY: dict[str, str] = {
    "reach":   "reach_point",
    "collect": "reach_point",
    "defeat":  "hit_target",
    "survive": "survive_area",
    "escort":  "traverse",
    "timer":   "traverse",
}
# Stable tie-break when two topologies score equally — most specific first.
_TOPOLOGY_PRECEDENCE: tuple[str, ...] = (
    "reach_point", "hit_target", "survive_area", "traverse")
# Nothing in the schema says what winning looks like. "traverse" — moving
# through the space — is the weakest claim we can make and the only one that
# does not invent a win condition. goal_topology never selects the archetype
# (that is motion_axis x medium), so a wrong default costs provenance clarity,
# not geometry.
_TOPOLOGY_DEFAULT = "traverse"

# ── Altitude evidence ────────────────────────────────────────────────────────
# A racetrack's elevation changes run to a few metres; real altitude banding
# starts far above that. Below this, y-data is terrain relief, not a corridor.
_ALTITUDE_EVIDENCE_M = 50.0
# Corridor heights outside this range are a parse artefact, not a design.
_CORRIDOR_HEIGHT_MIN = 20.0
_CORRIDOR_HEIGHT_MAX = 12_000.0
# Named default when the schema carries no altitude at all — a standard
# civilian jump-run exit altitude, high enough that stratified bands are real
# separations rather than a stack of near-identical y values.
_CORRIDOR_HEIGHT_DEFAULT = 4000.0
# A BoundedVolume is a play box, not a drop: the player crosses it under their
# own power rather than falling through it, so its default is sized for a
# few tens of seconds of travel, not a jump run.
_VOLUME_HEIGHT_DEFAULT = 200.0
# A climbable face. Sized for a big crag rather than a mountain: the wall has
# to stay climbable with the hold count a schema actually authors (see the
# reachability invariant in unity_layout._layout_vertical_surface).
_SURFACE_HEIGHT_DEFAULT = 60.0

_FT_TO_M = 0.3048
# Longest units first: "metres" must not lose to a bare "m", and the trailing
# guard rejects speeds ("200 km/h" -> "km" followed by "/" is not an altitude).
_ALT_UNIT_RE = re.compile(
    r"(\d{2,6}(?:\.\d+)?)\s*"
    r"(kft|km|metres|meters|metre|meter|feet|foot|ft|m)"
    r"(?![/\w])")
_ALT_UNIT_TO_M: dict[str, float] = {
    "kft": 1000.0 * _FT_TO_M, "km": 1000.0,
    "metres": 1.0, "meters": 1.0, "metre": 1.0, "meter": 1.0, "m": 1.0,
    "feet": _FT_TO_M, "foot": _FT_TO_M, "ft": _FT_TO_M,
}
# A number with a unit is only an ALTITUDE if an altitude word sits near it —
# "12 m wide" and "3 m above and behind" are not corridor heights.
_ALT_CONTEXT_WORDS: tuple[str, ...] = (
    "altitude", "altimeter", "deck", "ceiling", "exit", "jump", "deploy",
    "pull", "height", "agl", "above ground", "elevation", "apex", "aloft",
    "descend", "descent", "freefall", "free fall", "chute", "canopy",
    "opening", "drop",
)
_ALT_CONTEXT_WINDOW = 56   # characters either side of the number


class LayoutStrictError(ValueError):
    """No Stage-1 primitive covers this schema's (motion_axis, medium).

    Subclasses ValueError so the pipeline's existing FAIL-FAST handling (and
    `except ValueError` schema-rescue paths) treat it like any other strict
    doctrine failure.
    """


@dataclass(frozen=True)
class LayoutArchetype:
    """The selected primitive plus every derivation that produced it.

    `evidence` maps each derived field to the human-readable reason it took
    the value it did — this is what the provenance block prints, so a run that
    picked the wrong primitive can be diagnosed from the log alone.
    """
    name:          str
    motion_axis:   str
    medium:        str
    goal_topology: str
    evidence:      dict[str, str] = field(default_factory=dict)
    # Sub-axis of `vertical` only — "ascent" | "descent" | "". Descent moves
    # THROUGH a volume (VerticalCorridor); ascent moves ALONG a surface
    # (VerticalSurface). Empty for every non-vertical axis.
    direction:     str = ""

    def spatial_primitive(self, has_route: bool = False) -> str:
        """This archetype in src/unity_spatial.py's vocabulary, so the layout
        pass and the playability evaluator (Step 7) describe the same scene
        instead of each deriving its own answer."""
        from .unity_spatial import primitive_for_archetype
        return primitive_for_archetype(self.name, self.medium, has_route)

    def provenance(self) -> str:
        """One line, same shape for every archetype, emitted on EVERY run."""
        axis = self.motion_axis
        if self.direction:
            axis += (f"/{self.direction} [{self.evidence.get('direction_tier', '?')}"
                     f": {self.evidence.get('direction', 'n/a')}]")
        return (
            f"Layout archetype {self.name} selected — "
            f"motion_axis={axis} ({self.evidence.get('motion_axis', 'n/a')}), "
            f"medium={self.medium} ({self.evidence.get('medium', 'n/a')}), "
            f"goal_topology={self.goal_topology} "
            f"({self.evidence.get('goal_topology', 'n/a')})"
        )


# ── Schema text corpus ───────────────────────────────────────────────────────

def _schema_text(schema) -> str:
    """Every prose field the schema carries, lowercased into one corpus.

    Deterministic order (declaration order of the schema's own collections) so
    the same schema always yields the same string, and therefore the same
    classification. Structured fields (locomotion, objective types) are read
    separately — they are stronger evidence than prose and must not be diluted
    by it.
    """
    parts: list[str] = [
        schema.description or "", schema.project_name or "",
        schema.visual_style or "",
    ]
    parts.extend(schema.scenes or [])
    parts.extend(schema.features or [])
    parts.extend(f"{s.name} {s.description}" for s in schema.scene_specs)
    parts.extend(f"{s.name} {s.type} {s.description}" for s in schema.scripts)
    parts.extend(f"{b.label} {b.description}" for b in schema.story_beats)
    parts.extend(f"{t.name} {t.description}" for t in schema.audio_tracks)
    parts.extend(f"{a.id} {a.description}" for a in schema.audio_specs)
    parts.extend(f"{m.name} {m.description}" for m in schema.procedural_meshes)
    parts.extend(f"{e.name} {e.kind} {e.model_hint} {e.behavior}"
                 for e in schema.entities)
    parts.extend(f"{c.name} {c.role}" for c in schema.characters)
    for m in schema.missions:
        parts.append(f"{m.title} {m.description}")
        parts.extend(f"{o.description} {o.type}" for o in m.objectives)
    if schema.world_spec is not None:
        parts.append(f"{schema.world_spec.biome} {schema.world_spec.weather}")
    if schema.environment is not None:
        parts.append(schema.environment.time_of_day or "")
    return " \n ".join(p for p in parts if p).lower()


def _hits(text: str, needles: Sequence[str]) -> list[str]:
    """Needles present in `text` as whole words/phrases, in table order.

    Word-bounded on purpose: a bare `in` test makes "sky" match "skyscraper"
    and "whiskey", which is how keyword gates quietly start lying.
    """
    return [n for n in needles
            if re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", text)]


# ── Altitude readings ────────────────────────────────────────────────────────

def _text_altitudes(text: str) -> list[tuple[float, str]]:
    """Altitude readings parsed out of the corpus, as (metres, quoted span).

    Only numbers that carry a length unit AND sit within
    `_ALT_CONTEXT_WINDOW` characters of an altitude word count — that is what
    separates "pull-altitude alarm at 1500m" from "12 m wide" and from the
    camera rig's "3m above and behind".
    """
    out: list[tuple[float, str]] = []
    for m in _ALT_UNIT_RE.finditer(text):
        lo = max(0, m.start() - _ALT_CONTEXT_WINDOW)
        window = text[lo:m.end() + _ALT_CONTEXT_WINDOW]
        if not _hits(window, _ALT_CONTEXT_WORDS):
            continue
        metres = float(m.group(1)) * _ALT_UNIT_TO_M[m.group(2)]
        if _CORRIDOR_HEIGHT_MIN <= metres <= _CORRIDOR_HEIGHT_MAX:
            out.append((metres, m.group(0).strip()))
    return out


def entity_altitudes(schema) -> list[tuple[str, float]]:
    """Per-entity altitude already baked into the schema's own geometry.

    `instance_positions` and `path_points` both carry y = elevation, so a
    scaffold (or a previous run of this pass) that already banded the scene is
    authoritative — this pass reads it rather than re-inventing it.
    """
    out: list[tuple[str, float]] = []
    for e in schema.entities:
        ys = [p[1] for p in e.instance_positions if len(p) > 1]
        ys += [p[1] for p in e.path_points if len(p) > 1]
        if ys:
            top = max(ys)
            if abs(top) > 1e-6:
                out.append((e.name, top))
    return out


def _schema_height(schema, default: float, default_name: str
                   ) -> tuple[float, str, bool]:
    """(height_m, why, used_default) for any primitive that needs a vertical
    extent.

    Schema data wins in this order: baked entity altitude (real geometry), then
    altitude/deck figures parsed from the prose, then the caller's named
    default with a warning. Never guesses from locomotion or genre.
    """
    baked = [y for _n, y in entity_altitudes(schema) if y >= _ALTITUDE_EVIDENCE_M]
    if baked:
        h = min(_CORRIDOR_HEIGHT_MAX, max(baked))
        return round(h, 3), f"top of baked entity altitude data ({h:g} m)", False
    readings = _text_altitudes(_schema_text(schema))
    if readings:
        metres, span = max(readings, key=lambda r: (r[0], r[1]))
        return (round(metres, 3),
                f"highest altitude figure in the schema text ({span!r})", False)
    return (default,
            f"named default {default_name} ({default:g} m) — schema carries no "
            "altitude or deck figure", True)


def corridor_height(schema) -> tuple[float, str, bool]:
    """Vertical extent of a VerticalCorridor — the drop from exit to terminal
    plane."""
    return _schema_height(schema, _CORRIDOR_HEIGHT_DEFAULT,
                          "_CORRIDOR_HEIGHT_DEFAULT")


def surface_height(schema) -> tuple[float, str, bool]:
    """Vertical extent of a VerticalSurface — base to summit of the wall."""
    return _schema_height(schema, _SURFACE_HEIGHT_DEFAULT,
                          "_SURFACE_HEIGHT_DEFAULT")


def volume_height(schema) -> tuple[float, str, bool]:
    """Vertical extent of a BoundedVolume — floor (or seabed) to ceiling (or
    water surface). Same schema evidence, smaller default: a play box is not a
    jump run."""
    return _schema_height(schema, _VOLUME_HEIGHT_DEFAULT,
                          "_VOLUME_HEIGHT_DEFAULT")


# ── Axis derivation ──────────────────────────────────────────────────────────

def _derive_medium(schema, text: str) -> tuple[str, str]:
    """What the space is made of. Locomotion is structured and outranks prose.

    SETTING vocabulary never lifts a walking or driving player off the ground;
    vocabulary that names the medium outright still does. That line was learned
    twice, live, on the same climbing-horror brief: the concept said
    "atmosphere" once, so medium came out `air`; the word was removed, and the
    next run said "abyss" once, so medium came out `water`. Both produced
    planar+air / planar+water, which no primitive covers, and hard-failed a
    build that ArenaPlane handles perfectly — the player is on a rock face in
    both readings. Mood and scenery vocabulary is unbounded, so filtering it
    word by word is a losing game.

    But the opposite blanket rule broke a real skydiving schema whose
    `locomotion` was left at the "walk" default: "skydiving" is not scene
    dressing, it is a statement about where the player's body is. So prose
    overrides a surface locomotion only through `_AIR_STRONG`/`_WATER_STRONG`
    (or by saying outright that the player hovers/floats) — the same
    strong-evidence tiering `_derive_motion_axis` uses, and for the same
    reason: one ambiguous word must never carry an axis.
    """
    if schema.locomotion == "swim":
        return "water", "locomotion='swim'"
    if schema.locomotion == "fly":
        return "air", "locomotion='fly'"

    air, water = _hits(text, _AIR_WORDS), _hits(text, _WATER_WORDS)
    air_strong, water_strong = _hits(text, _AIR_STRONG), _hits(text, _WATER_STRONG)
    free = _hits(text, _FREE_MEDIUM_WORDS)
    if not (free or air_strong or water_strong):
        why = f"locomotion={schema.locomotion!r} is a surface locomotion"
        if air or water:
            why += (f"; setting vocabulary {(air + water)[:4]} describes the "
                    "scene, not what the player moves through")
        return "ground", why

    lead = (f"free-medium prose {free[:3]}" if free
            else f"medium-defining vocabulary {(air_strong + water_strong)[:3]}")
    if water_strong and not air_strong:
        return "water", f"{lead} + water vocabulary {water[:4]}"
    if air_strong and not water_strong:
        return "air", f"{lead} + air vocabulary {air[:4]}"
    if water and len(water) > len(air):
        return "water", f"{lead} + water vocabulary {water[:4]}"
    if air:
        # Ties go to air: a jump over the sea is an air corridor terminating on
        # a water surface, and only locomotion='swim' says the player is IN it.
        return "air", f"{lead} + air vocabulary {air[:4]}"
    if water:
        return "water", f"{lead} + water vocabulary {water[:4]}"
    return "ground", (f"{lead} but no air or water vocabulary to say which "
                      "medium")


def _free_medium_locomotion(schema, text: str) -> Optional[str]:
    """Why this schema's player moves freely through its medium rather than
    over a surface — or None. Structured `locomotion` first, then the hover/
    float vocabulary the enum cannot carry."""
    if schema.locomotion in ("fly", "swim"):
        return f"locomotion={schema.locomotion!r}"
    free = _hits(text, _FREE_MEDIUM_WORDS)
    return f"free-medium vocabulary {free[:3]}" if free else None


def _derive_motion_axis(schema, text: str, medium: str) -> tuple[str, str]:
    """How the player's own motion is constrained.

    Order matters: an explicit rail is the most specific claim, then descent,
    then free movement through a medium, then the planar default. Descent needs
    strong evidence precisely because getting it wrong hard-fails an otherwise
    healthy ground game — and it is checked BEFORE the free-medium test, so a
    skydiver (locomotion='fly' + freefall) still gets a corridor, not a box.
    """
    rails = _hits(text, _PATH_WORDS)
    if rails:
        return "path", f"rail vocabulary {rails[:3]}"

    strong = _hits(text, _VERTICAL_STRONG)
    if strong:
        return "vertical", f"descent vocabulary {strong[:4]}"

    # Ascent clears the same bar descent does, and only that bar: unambiguous
    # climbing nouns, or an entity authored as something you grab. The ascent
    # VERBS are deliberately excluded here — "climb the leaderboard" is
    # ordinary English and must not turn a walking game into a climbing one.
    climb_words = _hits(text, _ASCENT_STRONG)
    climbable = [e.name for e in schema.entities if is_climbable_entity(e)]
    if climb_words or climbable:
        why = (f"climbing vocabulary {climb_words[:3]}" if climb_words
               else "")
        if climbable:
            why += (" + " if why else "") + f"climbable entities {climbable[:3]}"
        return "vertical", why

    banded = [(n, y) for n, y in entity_altitudes(schema)
              if abs(y) >= _ALTITUDE_EVIDENCE_M]
    if banded:
        top = max(banded, key=lambda b: (abs(b[1]), b[0]))
        return "vertical", (f"entity altitude data — {top[0]!r} at y={top[1]:g} "
                            f"(>= {_ALTITUDE_EVIDENCE_M:g} m)")

    weak = _hits(text, _VERTICAL_WEAK)
    if len(weak) >= 2 and medium in ("air", "water"):
        return "vertical", f"descent verbs {weak[:4]} in a {medium} scene"

    # Free movement through air or water with no descent framing. Gated on the
    # medium as well as the locomotion: a WALKING game merely described in sky
    # vocabulary is not volumetric, and floating it in a box would be exactly
    # the "wrong primitive" failure the abstain path exists to prevent.
    if medium in ("air", "water"):
        free_why = _free_medium_locomotion(schema, text)
        if free_why:
            extra = (f"; descent verbs {weak[:3]} too weak to make it vertical"
                     if weak else "")
            return "volumetric", f"{free_why}, no descent evidence{extra}"

    why = "no rail, descent or altitude evidence"
    if weak:
        why += f" (descent verbs {weak[:3]} too weak on their own)"
    return "planar", why


def _derive_goal_topology(schema, text: str) -> tuple[str, str]:
    """What winning looks like, spatially. Authored objective types first."""
    scores: dict[str, int] = {}
    obj_types: list[str] = []
    for m in schema.missions:
        for o in m.objectives:
            topo = _OBJECTIVE_TOPOLOGY.get(o.type)
            if topo:
                scores[topo] = scores.get(topo, 0) + 1
                obj_types.append(o.type)
    if scores:
        best = _best_topology(scores)
        return best, f"mission objective types {sorted(set(obj_types))}"

    matched: dict[str, list[str]] = {}
    for topo, words in _GOAL_WORD_TABLE:
        found = _hits(text, words)
        if found:
            scores[topo] = len(found)
            matched[topo] = found
    if scores:
        best = _best_topology(scores)
        return best, f"goal vocabulary {matched[best][:4]}"
    return _TOPOLOGY_DEFAULT, "no objective types or goal vocabulary — defaulted"


def _best_topology(scores: dict[str, int]) -> str:
    """Highest score wins; ties break on `_TOPOLOGY_PRECEDENCE`, never on dict
    iteration order."""
    return min(scores, key=lambda t: (-scores[t], _TOPOLOGY_PRECEDENCE.index(t)))


# ── Selection ────────────────────────────────────────────────────────────────

# The whole Stage-1 table. Adding a primitive means adding a row here and a
# builder in src/unity_layout.py — never widening an existing row's reach.
_ARCHETYPE_BY_AXES: dict[tuple[str, str], str] = {
    ("vertical",   "air"):    "VerticalCorridor",
    ("vertical",   "water"):  "VerticalCorridor",
    ("volumetric", "air"):    "BoundedVolume",
    ("volumetric", "water"):  "BoundedVolume",
    ("planar",     "ground"): "ArenaPlane",
}
# `vertical` splits on direction before the medium table is consulted at all.
# Ascent is a surface, whatever it is surrounded by — a sea cliff and a desert
# tower are the same layout problem — so it does not vary by medium the way
# descent does. Descent keeps the table above verbatim, which is what holds
# every already-resolving VerticalCorridor schema in place.
_ASCENT_ARCHETYPE = "VerticalSurface"
# What a user can change to land on a row that exists. Keyed by the axis pair
# that abstained, so the error names a real next step instead of "unsupported".
# No ("planar", "air") / ("planar", "water") rows: those pairs are unreachable
# by construction now. `_derive_medium` only returns air/water when the player
# is stated to fly, swim, hover or float — and every one of those makes
# `_derive_motion_axis` return "volumetric" (or "vertical"/"path"), never
# "planar". They used to be reachable off one incidental mood word, which is
# exactly how a climbing game hard-failed twice.
_ABSTAIN_HINTS: dict[tuple[str, str], str] = {
    ("vertical", "ground"): (
        "descent through solid ground needs a shaft primitive, which does not "
        "exist yet. If the descent is through open air or water, set "
        "locomotion to 'fly'/'swim' or say so in the description"),
    ("path", "ground"): (
        "an on-rails route needs the spline primitive, which does not exist "
        "yet. A freely driven circuit is NOT on rails — drop the rail wording "
        "and the existing corridor packing in ArenaPlane handles the track"),
    ("path", "air"): (
        "an on-rails air route needs the spline primitive, which does not "
        "exist yet"),
    ("path", "water"): (
        "an on-rails water route needs the spline primitive, which does not "
        "exist yet"),
}


def select_archetype(schema) -> LayoutArchetype:
    """Pick the spatial primitive this schema's content belongs in.

    Pure and deterministic — same schema, same archetype, every run. Raises
    `LayoutStrictError` when no Stage-1 primitive covers the derived axes;
    there is no fall-through to ArenaPlane, by design.
    """
    text = _schema_text(schema)
    medium, why_medium = _derive_medium(schema, text)
    motion_axis, why_axis = _derive_motion_axis(schema, text, medium)
    goal_topology, why_goal = _derive_goal_topology(schema, text)

    evidence = {
        "motion_axis":   why_axis,
        "medium":        why_medium,
        "goal_topology": why_goal,
    }

    # `locomotion='climb'` is the one signal allowed to establish the axis AND
    # the direction at once — it states outright what the player is doing, the
    # same standing `locomotion='fly'`/'swim' already has for the medium.
    direction, why_dir, dir_tier = "", "", ""
    if motion_axis == "vertical" or (schema.locomotion or "").lower() == "climb":
        _dir, why_dir, dir_tier = _derive_direction(schema, text)
        evidence["direction"] = why_dir
        evidence["direction_tier"] = dir_tier
        if _dir is None:
            raise LayoutStrictError(
                f"[layout.strict] motion is vertical but the DIRECTION is "
                f"undecided for project {schema.project_name!r}: could be "
                f"'ascent' (VerticalSurface — a wall the player climbs) or "
                f"'descent' (VerticalCorridor — a drop the player falls "
                f"through), and the two are different layouts, not different "
                f"numbers. Evidence: {why_dir}. Derived from: motion_axis <- "
                f"{why_axis}; medium <- {why_medium}. Fix: say which way the "
                "player travels (climb/ascend/summit vs fall/dive/descend), "
                "tag the climbable groups 'Holdable', or name the aircraft/"
                "platform they jump from."
            )
        direction = _dir

    # Ascent bypasses the medium table entirely — a wall is a wall whether it
    # rises out of a desert or a sea.
    name: Optional[str] = (_ASCENT_ARCHETYPE if direction == "ascent"
                           else _ARCHETYPE_BY_AXES.get((motion_axis, medium)))
    if name is None:
        supported = ", ".join(
            [f"{a}+{m} -> {n}" for (a, m), n in _ARCHETYPE_BY_AXES.items()]
            + [f"vertical/ascent -> {_ASCENT_ARCHETYPE}"])
        hint = _ABSTAIN_HINTS.get(
            (motion_axis, medium),
            "no Stage-1 primitive covers this combination")
        raise LayoutStrictError(
            f"[layout.strict] no layout primitive covers motion_axis="
            f"{motion_axis!r} + medium={medium!r} "
            f"(goal_topology={goal_topology!r}) for project "
            f"{schema.project_name!r}. Derived from: motion_axis <- "
            f"{why_axis}; medium <- {why_medium}; goal_topology <- {why_goal}. "
            f"Implemented primitives: {supported}. Fix: {hint}. The layout pass "
            "will not fall back to a ground plane — a game laid out in the "
            "wrong primitive is worse than one that stops here."
        )
    return LayoutArchetype(name=name, motion_axis=motion_axis, medium=medium,
                           goal_topology=goal_topology, evidence=evidence,
                           direction=direction)


# ── Corridor role classification ─────────────────────────────────────────────
# Which entity plays which part in a VerticalCorridor. Read by
# src/unity_layout.py's builder; kept here beside the vocabularies it shares.

# Kinds whose whole point is to BE the ground/surface — they belong on the
# terminal plane no matter how tall the corridor is.
_TERMINAL_KINDS: frozenset[str] = frozenset({
    "terrain", "ground", "floor", "road", "track", "surface", "platform",
    "water", "arena", "landscape",
})
# Unity tags carrying the same meaning. Live LLM schemas routinely author
# kind='prop' with the real role in the tag.
_TERMINAL_TAGS: frozenset[str] = frozenset({
    "ground", "terrain", "floor", "surface", "road", "water",
})
# NOT in that set: "Environment". It is Unity's catch-all scenery tag, not a
# claim about the floor — live it was carrying a cloud layer, which the
# terminal plane promptly parked at y=0 in a skydiving corridor.
# ...and the same meaning spelled out in the NAME. Kept separate from
# `_GOAL_NAME_WORDS`: a valley floor is the terminal plane, not the objective.
_TERMINAL_NAME_WORDS: tuple[str, ...] = (
    "valley floor", "floor", "ground plane", "groundplane", "terrain",
    "landscape", "bedrock", "seabed", "sea bed", "riverbed", "canyon floor",
    "desert floor", "plain", "plains", "field", "meadow", "basin",
)
_GOAL_KINDS: frozenset[str] = frozenset({
    "goal", "checkpoint", "finish", "objective", "landing_pad", "landingpad",
})
# Bare "target" is deliberately absent: a formation-lock target, a practice
# target and a targeting reticle are all mid-air content, and pinning them to
# the terminal plane is exactly the flattening this primitive removes.
_GOAL_NAME_WORDS: tuple[str, ...] = (
    "landing", "landing pad", "landingpad", "landing zone", "pad",
    "drop zone", "dropzone", "bullseye", "goal", "finish", "arena", "runway",
    "helipad", "touchdown", "extraction",
)
# Things that live IN the water and therefore hang below its surface.
_WATER_DWELLER_WORDS: tuple[str, ...] = (
    "fish", "shark", "sharks", "coral", "reef", "kelp", "seaweed", "anemone",
    "urchin", "jellyfish", "turtle", "eel", "ray", "rays", "wreck",
    "shipwreck", "seabed", "sea floor", "seafloor", "treasure", "chest",
    "submarine", "submersible", "diver", "divers", "buoy", "mine", "mines",
)
# Things whose home is the floor of a BoundedVolume — the only content allowed
# to sit at y = 0 in an air box.
_GROUND_DWELLER_WORDS: tuple[str, ...] = (
    "tree", "trees", "forest", "rock", "rocks", "boulder", "hill", "hills",
    "mountain", "mountains", "ridge", "building", "buildings", "house",
    "houses", "tower", "ruin", "ruins", "terrain", "ground", "grass",
    "landing pad", "helipad", "runway", "hangar", "village", "town",
)
# Things that ride ON a water surface rather than under it.
_SURFACE_KINDS: frozenset[str] = frozenset({
    "boat", "ship", "vessel", "raft", "buoy", "surface", "dock", "pier",
})
_SURFACE_WORDS: tuple[str, ...] = (
    "boat", "boats", "ship", "ships", "raft", "buoy", "buoys", "dock",
    "docks", "pier", "pontoon", "yacht", "kayak", "canoe", "dinghy",
    "surface marker", "water surface", "floating platform",
)
# ── Exit platform (VerticalCorridor) ─────────────────────────────────────────
# The thing the player jumps FROM. Left to footprint-size stratification it
# sank below the player's own spawn — a jump ship under the jumper.
_EXIT_PLATFORM_KINDS: frozenset[str] = frozenset({
    "vehicle", "aircraft", "plane", "airplane", "helicopter", "ship",
    # Live LLM output authored the jump plane as kind='structure' — the model
    # reaches for a generic bucket far more often than the exact noun, so the
    # allowlist has to cover the buckets it actually uses.
    "structure", "platform", "transport",
})
_EXIT_PLATFORM_WORDS: tuple[str, ...] = (
    "plane", "airplane", "aeroplane", "cessna", "jet", "helicopter",
    "chopper", "balloon", "blimp", "airship", "dirigible", "platform",
    "jump ship", "jumpship", "aircraft", "gantry", "ledge", "cliff edge",
    "dropship", "transport",
    # Jump-aircraft vocabulary a skydiving brief actually uses, plus the
    # "…Exit" naming the schema-fill favours (live: "TurbopropExit").
    "turboprop", "caravan", "twin otter", "skyvan", "otter", "jump plane",
    "jump run", "exit", "exit point", "door", "hatch", "ramp", "fuselage",
)
# "the player jumps from the Cessna" / "spawns off the gantry" — an authored
# relation, and the strongest signal there is. The tail is matched against
# candidate entity names, so a phrase naming nothing in the schema (jump from
# "the ledge") simply produces no relation.
_EXIT_RELATION_RE = re.compile(
    r"(?:jump|jumps|jumping|jumped|exit|exits|exiting|leap|leaps|leaping|"
    r"bail|bails|dive|dives|diving|drop|drops|deploy|deploys|spawn|spawns|"
    r"launch|launches|step|steps)\s+"
    r"(?:out\s+|off\s+|away\s+)?"
    r"(?:of\s+|from\s+|off\s+)\s*"
    r"(?:the\s+|a\s+|an\s+|their\s+|its\s+)?"
    r"([a-z][a-z0-9' -]{2,40})")
# "the player exits a Cessna Caravan" — the same relation with the connector
# left out, which is how briefs usually write it. Restricted to verbs that
# already MEAN leaving a vehicle: "jumps the queue" and "deploys the canopy"
# must not read as exit relations, and they do not appear here.
_EXIT_RELATION_BARE_RE = re.compile(
    r"(?:exit|exits|exiting|exited|bail|bails|disembark|disembarks|"
    r"disembarking|deplane|deplanes)\s+"
    r"(?:the\s+|a\s+|an\s+|their\s+|its\s+)?"
    r"([a-z][a-z0-9' -]{2,40})")
# Tokens too generic to identify an entity by (every schema says "the player").
_EXIT_RELATION_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "their", "its", "his", "her", "player", "ground",
    "sky", "air", "water", "top", "edge", "side", "back", "front", "here",
    "there", "it", "them", "this", "that", "and", "then", "at", "on", "in",
})
# The regex tail runs to the end of the clause, so "dives from the surface and
# descends past a coral reef" would offer "coral"/"reef" as exit-platform
# tokens and hand the role to a shoal of fish. Cut the phrase at the first
# connector and cap it at a short noun phrase — what follows a connector
# belongs to the next clause, not to the thing being exited.
_RELATION_PHRASE_CONNECTORS: frozenset[str] = frozenset({
    "and", "or", "then", "but", "to", "past", "before", "after", "while",
    "with", "on", "in", "at", "for", "from", "of", "into", "over", "under",
    "through", "toward", "towards", "onto", "down", "up", "so", "as", "when",
    "who", "which", "that", "the", "a", "an",
})
_RELATION_PHRASE_MAX_WORDS = 4


def _trim_relation_phrase(phrase: str) -> str:
    """The exited THING, without the rest of the sentence trailing off it."""
    out: list[str] = []
    for word in phrase.split()[:_RELATION_PHRASE_MAX_WORDS]:
        if word in _RELATION_PHRASE_CONNECTORS:
            break
        out.append(word)
    return " ".join(out)


def _entity_words(entity) -> str:
    """This entity's own text, CamelCase split apart.

    Entity names arrive as "LandingPad" / "CessnaCaravan" / "FloatingIslands",
    and `_hits` is word-bounded — without the split, "cessna" never matches
    "cessnacaravan" and every role predicate silently reads names as noise.
    Deliberately local to the ROLE predicates: `_schema_text` (which drives
    archetype selection) is left alone so no already-resolving schema changes
    classification.

    `tag` is in here because live LLM output puts the semantics THERE and not
    in `kind`: a DropZone run authored its ground as
    kind='prop', tag='Ground', name='ValleyFloor' — every kind-based predicate
    read "prop" and floated the valley floor at 1466 m.
    """
    raw = (f"{entity.name} {entity.kind} {getattr(entity, 'tag', '')} "
           f"{entity.model_hint} {entity.behavior}")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw).lower()


def _tag_of(entity) -> str:
    return (getattr(entity, "tag", "") or "").strip().lower()


def is_surface_entity(entity) -> bool:
    """This entity IS the ground — terrain, a floor, a road surface.

    Reads kind, tag AND name, because live schemas scatter the signal across
    all three (kind='prop', tag='Ground', name='ValleyFloor'). Any one of them
    is enough: a false positive parks scenery on the floor, a false negative
    floats the terrain a kilometre up.
    """
    if (entity.kind or "").lower().strip() in _TERMINAL_KINDS:
        return True
    if _tag_of(entity) in _TERMINAL_TAGS:
        return True
    return bool(_hits(_entity_words(entity), _TERMINAL_NAME_WORDS))


def is_terminal_entity(entity) -> bool:
    """Belongs on the corridor's terminal plane — the goal/landing target, or
    a surface that IS the ground. Everything else is corridor content."""
    return is_surface_entity(entity) or is_goal_entity(entity)


# Kinds that can BE the player avatar — mirrors unity_concept_entities'
# _AVATAR_KINDS and the generator's _PLAYER_MERGE_KINDS.
_PLAYER_KINDS: frozenset[str] = frozenset({
    "character", "player", "npc", "creature", "vehicle",
})
# "Player", "PlayerDiver", "PlayerAvatar" — the schema-fill's own naming.
_PLAYER_NAME_RE = re.compile(r"^\s*(?:the\s+)?player\b", re.IGNORECASE)
_PLAYER_ALIASES: tuple[str, ...] = ("avatar", "protagonist", "hero", "pc")


def player_rank(entity) -> int:
    """How strongly this entity claims to be the player's own group.

      3 — tagged Player AND an avatar kind (what the generator merges)
      2 — tagged Player, whatever the kind
      1 — named Player / avatar / protagonist / hero
      0 — not the player

    The old gate demanded rank 3 and nothing else. A live DropZone run authored
    its avatar as kind='prop', tag='Untagged', name='Player' — rank 1 — so the
    layout never found a player at all and stratified them at 475 m like
    scenery, in a corridor whose whole contract is "the player starts at the
    top". Falling back through weaker signals is strictly better than silently
    having no player.
    """
    tag  = _tag_of(entity)
    kind = (entity.kind or "").lower().strip()
    if tag == "player":
        return 3 if kind in _PLAYER_KINDS else 2
    if _PLAYER_NAME_RE.match(entity.name or ""):
        return 1
    if kind in _PLAYER_KINDS and _hits(_entity_words(entity), _PLAYER_ALIASES):
        return 1
    return 0


def pick_player_entity(entities: Sequence[_Entity]) -> tuple[Optional[_Entity], str]:
    """The player's group and the signal that found it. Strongest rank wins;
    ties go to the earliest in schema order."""
    best: Optional[_Entity] = None
    best_rank = 0
    for e in entities:
        rank = player_rank(e)
        if rank > best_rank:
            best, best_rank = e, rank
    why = {3: "tag='Player' + avatar kind", 2: "tag='Player'",
           1: "name/alias"}.get(best_rank, "")
    return best, why


def goal_rank(entity) -> int:
    """How strongly this entity claims to be the thing the player is falling
    TOWARD: 2 = an authored goal `kind`, 1 = goal wording in its name/hint,
    0 = not a goal. An authored kind always outranks prose."""
    if (entity.kind or "").lower().strip() in _GOAL_KINDS:
        return 2
    return 1 if _hits(_entity_words(entity), _GOAL_NAME_WORDS) else 0


def is_goal_entity(entity) -> bool:
    return goal_rank(entity) > 0


def pick_goal_entity(entities: Sequence[_Entity]) -> Optional[_Entity]:
    """The single goal pinned to the terminal plane under the drop axis.
    Highest `goal_rank` wins; ties go to the earliest in schema order, so the
    choice never depends on iteration accidents."""
    best: Optional[_Entity] = None
    best_rank = 0
    for e in entities:
        rank = goal_rank(e)
        if rank > best_rank:
            best, best_rank = e, rank
    return best


def is_water_dweller(entity) -> bool:
    """Lives below a water surface plane rather than in the air above it."""
    return bool(_hits(_entity_words(entity), _WATER_DWELLER_WORDS))


def is_ground_dweller(entity) -> bool:
    """Belongs on a BoundedVolume's floor. The ONLY thing allowed to sit at
    y = 0 in an air box — everything else is interior content, because
    defaulting content to the floor is the flattening these primitives exist
    to remove.

    An entity that says it floats/hovers/drifts is never a ground dweller,
    whatever it is MADE of: a "drifting rock island" is airborne content whose
    material vocabulary would otherwise pin it to the terrain.
    """
    words = _entity_words(entity)
    if _hits(words, _FREE_MEDIUM_WORDS):
        return False
    if is_surface_entity(entity):
        return True
    return bool(_hits(words, _GROUND_DWELLER_WORDS))


def is_surface_dweller(entity) -> bool:
    """Rides ON a water surface (boat, buoy, dock) rather than swimming below
    it. Without this every hull would be parked underwater."""
    kind = (entity.kind or "").lower().strip()
    if kind in _SURFACE_KINDS:
        return True
    return bool(_hits(_entity_words(entity), _SURFACE_WORDS))


def is_climbable_entity(entity) -> bool:
    """This group IS something the player grabs — a handhold, a ledge, a grip.
    These are the only entities that count toward the reachable chain."""
    if _tag_of(entity) in _CLIMB_TAGS:
        return True
    if (entity.kind or "").lower().strip() in _CLIMB_KINDS:
        return True
    return bool(_hits(_entity_words(entity), _ASCENT_STRONG))


def is_hazard_entity(entity) -> bool:
    """Hangs on the wall to hurt you. Interleaves with the holds, never
    satisfies the reach chain."""
    return (_tag_of(entity) in _HAZARD_TAGS
            or (entity.kind or "").lower().strip() in _HAZARD_KINDS)


def pick_wall_surface(entities: Sequence[_Entity]) -> tuple[Optional[_Entity], str]:
    """The entity that IS the climbing surface, and the signal that chose it.

      1. an authored surface: kind structure/creature/terrain carrying the
         Environment tag — the shape a schema-fill actually produces for
         "the cliff face"
      2. otherwise the largest group by footprint, which is what a wall is

    Returns (None, "") when there is nothing wall-like; the caller then builds
    the face as bare geometry.
    """
    wall_kinds = {"structure", "creature", "terrain", "building", "landscape"}
    for e in entities:
        if (e.kind or "").lower().strip() in wall_kinds and _tag_of(e) == "environment":
            return e, f"kind={e.kind!r} + tag='Environment'"
    surfacey = [e for e in entities
                if (e.kind or "").lower().strip() in wall_kinds
                or is_surface_entity(e)]
    if surfacey:
        best = max(surfacey, key=lambda e: (e.group_radius, e.scale, e.name))
        return best, (f"largest wall-like group by footprint "
                      f"(r={best.group_radius:.2f}, kind={best.kind!r})")
    return None, ""


def _derive_direction(schema, text: str) -> tuple[Optional[str], str, str]:
    """Which way the vertical axis runs: 'ascent' | 'descent' | None.

    Tiered exactly like the medium rule — structured signal first, unambiguous
    vocabulary next, entity structure last, and never one unqualified prose
    hit. Returns (direction, why, tier); None means the caller must abstain
    rather than guess, because guessing picks the wrong PRIMITIVE, not just
    the wrong numbers.
    """
    # 1. locomotion. The enum is walk/drive/fly/swim today, so "climb" cannot
    #    arrive through it — handled anyway so widening the enum Just Works,
    #    and because it is the one signal allowed to establish the axis too.
    loco = (schema.locomotion or "").lower()
    if loco == "climb":
        return "ascent", "locomotion='climb'", "locomotion"
    if loco in ("fall", "dive", "skydive"):
        return "descent", f"locomotion={loco!r}", "locomotion"

    # 2. Vocabulary, by weight of evidence.
    up, down = _hits(text, _ASCENT_VERBS), _hits(text, _DESCENT_VERBS)
    strong_up = _hits(text, _ASCENT_STRONG)
    strong_down = _hits(text, _VERTICAL_STRONG)
    if strong_up and not strong_down:
        return "ascent", f"climbing vocabulary {strong_up[:4]}", "strong-vocabulary"
    if strong_down and not strong_up:
        return "descent", f"descent vocabulary {strong_down[:4]}", "strong-vocabulary"
    if strong_up and strong_down:
        # Both named outright — go with the heavier prose, ties to descent so
        # a schema that already resolved to VerticalCorridor keeps doing so.
        if len(strong_up) > len(strong_down):
            return "ascent", (f"climbing vocabulary {strong_up[:3]} outweighs "
                              f"descent {strong_down[:3]}"), "strong-vocabulary"
        return "descent", (f"descent vocabulary {strong_down[:3]} outweighs "
                           f"climbing {strong_up[:3]}"), "strong-vocabulary"
    if up and len(up) > len(down):
        return "ascent", f"ascent verbs {up[:4]} outweigh descent {down[:4]}", "vocabulary"
    if down and len(down) > len(up):
        return "descent", f"descent verbs {down[:4]} outweigh ascent {up[:4]}", "vocabulary"

    # 3. Entity structure — authored tags/kinds, not prose.
    climbable = [e.name for e in schema.entities if is_climbable_entity(e)]
    if climbable:
        return "ascent", f"climbable entities {climbable[:3]}", "entity"
    platform, why_platform = pick_exit_platform(schema, None, schema.entities, "air")
    if platform is not None:
        return "descent", f"exit platform {platform.name!r} ({why_platform})", "entity"

    # 4. Nothing decides it.
    return None, (f"ascent evidence {(up or ['none'])[:3]} vs descent evidence "
                  f"{(down or ['none'])[:3]} — neither decides"), "none"


def _relation_targets(schema, player) -> list[str]:
    """Noun phrases the schema says the player jumps/exits/dives FROM.

    Read from the player's own `behavior`/`model_hint` first (the most direct
    statement), then the project description and story beats. Deterministic
    order; deduplicated.
    """
    sources: list[str] = []
    if player is not None:
        sources.append(_entity_words(player))
    sources.append((schema.description or "").lower())
    sources.extend(f"{b.label} {b.description}".lower() for b in schema.story_beats)
    for m in schema.missions:
        sources.append(f"{m.title} {m.description}".lower())
        sources.extend(o.description.lower() for o in m.objectives)
    out: list[str] = []
    # Connector form first ("jumps FROM the X") — it states the relation
    # unambiguously; the bare form is the fallback reading.
    for pattern in (_EXIT_RELATION_RE, _EXIT_RELATION_BARE_RE):
        for src in sources:
            for match in pattern.finditer(src):
                phrase = _trim_relation_phrase(match.group(1).strip())
                if phrase and phrase not in out:
                    out.append(phrase)
    return out


def _name_tokens(text: str) -> set[str]:
    """Lowercase word tokens, splitting CamelCase so "CessnaCaravan" matches
    the phrase "cessna caravan"."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return {t for t in re.split(r"[^a-zA-Z0-9]+", spaced.lower())
            if t and t not in _EXIT_RELATION_STOPWORDS}


def pick_exit_platform(schema, player, candidates: Sequence[_Entity],
                       medium: str) -> tuple[Optional[_Entity], str]:
    """The one entity the player jumps FROM, and the signal that named it.

    Priority, highest first:
      1. relation — the schema states it ("the player exits a Cessna Caravan")
         and the phrase matches a candidate's name or model hint
      2. kind     — a vehicle/aircraft in a vertical+air layout; nothing else
         in an air corridor is a thing you can stand on
      3. vocabulary — plane / cessna / helicopter / balloon / platform / …

    At most one entity ever wins. Ties inside a signal go to the earliest
    candidate in schema order. Returns (None, "") when nothing matches — an
    unattached player spawn at the corridor top is a fine outcome.
    """
    for phrase in _relation_targets(schema, player):
        wanted = _name_tokens(phrase)
        if not wanted:
            continue
        for e in candidates:
            if wanted & _name_tokens(f"{e.name} {e.model_hint}"):
                return e, f"relation — schema says the player exits {phrase!r}"

    if medium == "air":
        for e in candidates:
            if (e.kind or "").lower().strip() in _EXIT_PLATFORM_KINDS:
                return e, f"kind — {e.kind!r} in a vertical+air layout"

    for e in candidates:
        found = _hits(_entity_words(e), _EXIT_PLATFORM_WORDS)
        if found:
            return e, f"vocabulary {found[:3]}"
    return None, ""


def entity_altitude_hint(entity) -> Optional[float]:
    """An altitude this specific group's own text pins it to, e.g. an
    "altitude gate ring at 2000m". None when the group carries no band data —
    the caller then stratifies it. Deterministic: highest reading wins."""
    readings = _text_altitudes(_entity_words(entity))
    if not readings:
        return None
    return round(max(r[0] for r in readings), 3)


__all__ = [
    "LayoutArchetype", "LayoutStrictError", "select_archetype",
    "corridor_height", "volume_height", "surface_height",
    "is_climbable_entity", "is_hazard_entity", "pick_wall_surface",
    "entity_altitudes",
    "entity_altitude_hint", "is_terminal_entity", "is_goal_entity",
    "goal_rank", "pick_goal_entity", "is_water_dweller", "is_ground_dweller",
    "is_surface_dweller", "is_surface_entity", "pick_exit_platform",
    "player_rank", "pick_player_entity",
]
