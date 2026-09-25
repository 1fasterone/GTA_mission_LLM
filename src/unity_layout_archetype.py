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

  motion_axis   vertical | volumetric | planar | path | static
                                           how the player's own motion is
                                           constrained
  medium        air | water | ground | screen
                                           what the space is made of
  goal_topology reach_point | survive_area | hit_target | traverse

and then a table lookup:

  (vertical,   air)    -> VerticalCorridor
  (vertical,   water)  -> VerticalCorridor
  (volumetric, air)    -> BoundedVolume
  (volumetric, water)  -> BoundedVolume
  (planar,     ground) -> ArenaPlane       (the existing packer, unchanged)
  (static,     screen) -> UISurface        (non-embodied: a board of panels)
  anything else        -> ABSTAIN, hard-fail

`static` + `screen` is the one pair not derived from evidence at all. Both
axes fall out of a single declaration — `locomotion='none'`, the schema saying
it has no avatar — and that declaration is checked BEFORE every vocabulary
tier below, rails included. It has to be: this is the one game shape whose
prose is systematically about something other than space. A game that teaches
a version control system says branch, tree, cloud, push, pull, merge and drop
in every paragraph, and not one of them describes a place. Scoring them would
hand a game with no body an air medium and a descent axis.

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
vertical evidence has to be STRONG. Ascent and descent clear the SAME bar —
this is the settled rule, not a relaxation of a stricter one: a climb is as
much a vertical game as a drop is, and holding climbing evidence to a higher
standard is what produced a flat arena for the living-wall brief. Either of:

  * one unambiguous term — descent (freefall, skydive, parachute, wingsuit,
    altimeter, …) or climbing (handhold, belay, piton, rock face, free solo,
    …); or a group authored as something you grab (a Holdable tag, a
    handhold/ledge/grip kind), which is structure rather than prose and
    therefore stronger still;
  * two weaker descent verbs in a scene already established as air/water;
  * real altitude data on the entities.

What does NOT clear it, in either direction, is a single ambiguous verb.
"descent into the caves" never flips the axis, and neither does "climb the
leaderboard" or "sales are climbing" — the ascent VERBS choose a direction once
the axis is already vertical, and never establish the axis themselves. That
asymmetry between climbing NOUNS (axis-establishing) and climbing VERBS
(direction-only) is the whole guard against prose-only false positives.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence, TypeVar

from .unity_schema import is_embodied
from .unity_spatial_facts import (UNIT_ALTERNATION, UNIT_TO_M, match_facts,
                                  spatial_facts_of)

_log = logging.getLogger(__name__)


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
    "handhold", "hold", "climbable", "ledge", "grip", "platform",
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

# Unit spelling and unit VALUE come from the one table in
# src/unity_spatial_facts.py, so the brief-side extraction pass and this
# schema-side parser can never normalise the same figure differently. The
# trailing guard rejects speeds ("200 km/h" -> "km" followed by "/" is not an
# altitude); the two-digit minimum is this parser's own, since a single-digit
# altitude is always terrain relief.
_ALT_UNIT_RE = re.compile(
    r"(\d{2,6}(?:\.\d+)?)\s*(" + UNIT_ALTERNATION + r")(?![/\w])")
# A number with a unit is only an ALTITUDE if an altitude word sits near it —
# "12 m wide" and "3 m above and behind" are not corridor heights.
_ALT_CONTEXT_WORDS: tuple[str, ...] = (
    "altitude", "altimeter", "deck", "ceiling", "exit", "jump", "deploy",
    "pull", "height", "agl", "above ground", "elevation", "apex", "aloft",
    "descend", "descent", "freefall", "free fall", "chute", "canopy",
    "opening", "drop",
)
_ALT_CONTEXT_WINDOW = 56   # characters either side of the number

# ── Brief spatial facts: referent vocabulary per dimension ───────────────────
# Which of the brief's numbers is THIS dimension (see `_schema_height` tier 2).
# A fact only counts when its own referent window names the thing being sized —
# a brief may state a wall height, an approach distance and a fog radius in the
# same paragraph, and the largest of them is not automatically the wall.
#
# Deliberately short: every word here can silently resize a play space, so the
# lists carry the nouns that NAME the dimension and their plurals, nothing that
# merely co-occurs with it. Plurals only, because `_hits` is word-bounded and
# "cliffs" does not match "cliff".
_SURFACE_HEIGHT_REFERENTS: tuple[str, ...] = (
    "summit", "summits", "wall", "walls", "cliff", "cliffs", "peak", "peaks",
    "top",
)
_CORRIDOR_HEIGHT_REFERENTS: tuple[str, ...] = (
    "altitude", "altitudes", "jump", "jumps", "deck", "decks", "drop", "drops",
)
_VOLUME_HEIGHT_REFERENTS: tuple[str, ...] = (
    "depth", "depths", "ceiling", "ceilings",
)
# Tier 1 and tier 2 disagreeing by more than this fraction is a schema-fill
# FIDELITY signal, not an error: the brief and the schema both named a height
# and the model changed it on the way through. Tier 1 still wins — an explicit
# schema figure is the authored intent — but a silent 4x rewrite is exactly the
# class of loss this sidecar exists to make visible.
_FACT_DISAGREEMENT_FRACTION = 0.25


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
        metres = float(m.group(1)) * UNIT_TO_M[m.group(2)]
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


def _schema_dimension(schema) -> Optional[tuple[float, str]]:
    """(metres, why) for a vertical extent the SCHEMA itself states, or None.

    Baked entity altitude first — that is real geometry, either authored or
    banded by an earlier run of this pass — then altitude/deck figures parsed
    out of the schema's own prose. Never guesses from locomotion or genre.
    """
    baked = [y for _n, y in entity_altitudes(schema) if y >= _ALTITUDE_EVIDENCE_M]
    if baked:
        h = min(_CORRIDOR_HEIGHT_MAX, max(baked))
        return round(h, 3), f"top of baked entity altitude data ({h:g} m)"
    readings = _text_altitudes(_schema_text(schema))
    if readings:
        metres, span = max(readings, key=lambda r: (r[0], r[1]))
        return (round(metres, 3),
                f"highest altitude figure in the schema text ({span!r})")
    return None


def _disagrees(a: float, b: float) -> bool:
    """True when two readings of the same dimension are far enough apart to be
    different DESIGNS rather than different roundings."""
    scale = max(abs(a), abs(b))
    return scale > 0.0 and abs(a - b) / scale > _FACT_DISAGREEMENT_FRACTION


def _schema_height(schema, default: float, default_name: str,
                   referents: Sequence[str]) -> tuple[float, str, bool]:
    """(height_m, why, used_default) for any primitive that needs a vertical
    extent. One consumption rule, shared by every dimension:

      1. the schema's own figure — baked altitude or a parsed prose reading —
         wins whenever there is one;
      2. else a spatial fact the BRIEF stated whose referent names this
         dimension (see `_SURFACE_HEIGHT_REFERENTS` and friends), largest
         first, because the schema-fill LLM routinely compresses the brief's
         numbers away and layout would otherwise default silently;
      3. else the caller's named default, with the warning it has always had.

    Tier 2 exists because of a live failure: a brief said "the summit is a
    nerve cluster 900m up", the schema that came back carried no height at all,
    and a 60 m default wall was built for it. The number was in the brief the
    whole time — it just had no way past Step 3 (see
    src/unity_spatial_facts.py).

    When tiers 1 and 2 BOTH have an answer and they disagree materially, tier 1
    still wins — an explicit schema figure is authored intent — but the gap is
    logged. That is a schema-fill fidelity signal, not an error.

    The `[layout.facts]` tag lives in the LOG, never in the returned `why`.
    `why` ends up inside a Rich `console.print` in the pipeline, and Rich reads
    `[layout.facts]` as a markup tag and silently deletes it — caught live, the
    e2e printed "height H=900 m ( sized from the brief". Same reason
    `[layout.strict]` only ever appears in exception text.

    The primary decision logs at WARNING, not INFO: main.py defaults the level
    to WARNING, and tier 2 firing always means schema-fill dropped a figure the
    brief stated. That is worth the same default visibility as the tier-3
    fallback warning it replaces — recovered, but still a loss. The runner-up
    candidates stay at INFO for `--verbose`.
    """
    facts = spatial_facts_of(schema)
    candidates = match_facts(facts, referents,
                             _CORRIDOR_HEIGHT_MIN, _CORRIDOR_HEIGHT_MAX)
    schema_reading = _schema_dimension(schema)

    if schema_reading is not None:
        metres, why = schema_reading
        if candidates and _disagrees(metres, candidates[0].value_m):
            top = candidates[0]
            _log.warning(
                "[layout.facts] %r: the schema says %g m but the brief says "
                "%s. Using the schema figure (authored intent wins), but the "
                "gap means schema-fill changed a number the brief stated.",
                schema.project_name, metres, top.describe())
            why += (f"; brief disagrees — {top.value_m:g} m from "
                    f"{top.referent_text!r}")
        return metres, why, False

    if candidates:
        top = candidates[0]
        _log.warning(
            "[layout.facts] %r: the schema carries no figure for this "
            "dimension, so it is sized from the brief — %s. schema-fill "
            "dropped a number the brief stated; the sidecar recovered it.",
            schema.project_name, top.describe())
        for other in candidates[1:]:
            _log.info("[layout.facts] %r: also considered — %s",
                      schema.project_name, other.describe())
        why = f"spatial fact from the brief — {top.describe()}"
        if len(candidates) > 1:
            why += (f"; largest of {len(candidates)} matching facts "
                    f"({', '.join(f'{c.value_m:g} m' for c in candidates[1:])} "
                    "also matched)")
        return top.value_m, why, False

    return (default,
            f"named default {default_name} ({default:g} m) — schema carries no "
            "altitude or deck figure", True)


def corridor_height(schema) -> tuple[float, str, bool]:
    """Vertical extent of a VerticalCorridor — the drop from exit to terminal
    plane."""
    return _schema_height(schema, _CORRIDOR_HEIGHT_DEFAULT,
                          "_CORRIDOR_HEIGHT_DEFAULT",
                          _CORRIDOR_HEIGHT_REFERENTS)


def surface_height(schema) -> tuple[float, str, bool]:
    """Vertical extent of a VerticalSurface — base to summit of the wall."""
    return _schema_height(schema, _SURFACE_HEIGHT_DEFAULT,
                          "_SURFACE_HEIGHT_DEFAULT",
                          _SURFACE_HEIGHT_REFERENTS)


def volume_height(schema) -> tuple[float, str, bool]:
    """Vertical extent of a BoundedVolume — floor (or seabed) to ceiling (or
    water surface). Same schema evidence, smaller default: a play box is not a
    jump run."""
    return _schema_height(schema, _VOLUME_HEIGHT_DEFAULT,
                          "_VOLUME_HEIGHT_DEFAULT",
                          _VOLUME_HEIGHT_REFERENTS)


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

    `locomotion='none'` short-circuits all of it. A non-embodied game's play
    space is a SCREEN — there is no medium because there is no body in it — and
    the check has to come first, not last, because this is the one game shape
    whose vocabulary is systematically about something else. A repo-teaching
    sim says branch, tree, cloud, stream, push, pull, merge, drop; a card game
    says deck and flood; a logistics sim says freight and port. Those are its
    NOUNS. Scoring them as evidence about where the player's body is would give
    a game with no body a medium, and the medium is half the archetype key.
    """
    if not is_embodied(schema):
        return "screen", ("locomotion='none' — the game declares no avatar, so "
                          "the play space is a screen, not a volume of air, "
                          "water or ground")
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

    A declared non-embodied game has no player motion to constrain, and that
    answer outranks every vocabulary tier below — including the rail check,
    which is otherwise the most specific claim there is. This is the same
    stickiness the declaration already has against the intent classifier
    (src/unity_archetypes.py): schema-fill read the whole brief and said there
    is no avatar; these tables read keywords, and a game about a version
    control system will say "branch", "drop" and "push" in every paragraph
    without one metre of anything moving.
    """
    if not is_embodied(schema):
        return "static", ("locomotion='none' — the player acts through a "
                          "pointer, so there is no motion to constrain")

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
    # The only row not reached through evidence. Both of its axes come from one
    # declaration (`locomotion='none'`), so the pair is either exactly this or
    # the game is embodied — there is no partial match to abstain on.
    ("static",     "screen"): "UISurface",
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


def _is_player_tag(tag: str) -> bool:
    """Recognize the schema-fill's explicit Player-prefixed tag aliases."""
    return tag == "player" or tag.startswith("player")


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
    if _is_player_tag(tag):
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
    why = {3: "tag='Player' or Player-prefixed alias + avatar kind",
           2: "tag='Player' or Player-prefixed alias",
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


# ── Trackside relationship (ArenaPlane route corridor) ──────────────────────
# What spatial relationship a packable group has to an authored route
# corridor. `_corridor_pack` (src/unity_layout.py) used to treat every group
# identically — one anchor beside the track, `count` copies clustered
# locally around that single point — which is the right answer for a knot of
# spectators or a pit-stop kit but the wrong one for "twelve streetlights":
# that group wants one instance per interval spread along the WHOLE corridor,
# not twelve lamps stacked in one spot. Pure keyword/kind classification,
# same pattern as `is_ground_dweller` above — no new schema field, no LLM
# involvement post-schema.
#
# Checkpoints/finish-arch ("span the route" / "cross the route at a point")
# are the "route_gate" relationship — checked FIRST, ahead of edge/boundary,
# because a checkpoint is routinely also authored with a word from those
# tables (a live schema names one "CheckpointBanners", and "banner" is an
# _EDGE_DISTRIBUTED_WORDS hit) and the more specific claim must win: a gate
# spans the route and is crossed AT a point, an edge light stands beside the
# whole length of it. Perpendicular-gate placement math lives in
# `_route_gate_pack` (src/unity_layout.py).
_ROUTE_GATE_WORDS: tuple[str, ...] = (
    "checkpoint", "checkpoints", "finish line", "finish arch", "finish gate",
    "start line", "start gate", "startline", "gate", "gates",
)
_ROUTE_GATE_KINDS: frozenset[str] = frozenset({"checkpoint", "finish", "gate"})
# The subset that names the END of the route specifically — a finish arch is
# ONE thing, not `entity.count` of them strung along the track the way
# checkpoints are. Deliberately excludes bare "gate"/"checkpoint": those name
# the RELATIONSHIP (spans the route), not which end it's at. Also excludes
# bare "finish" — an ambiguous stem, same class this module already avoids
# elsewhere ("plane", "depth", "wreck"): "FinishFlags" is edge-of-track
# bunting (edge_distributed), not the arch itself, and only the multi-word
# phrases below actually name a gate.
_FINISH_GATE_WORDS: tuple[str, ...] = (
    "finish line", "finish arch", "finish gate",
)
_FINISH_GATE_KINDS: frozenset[str] = frozenset({"finish"})
_EDGE_DISTRIBUTED_WORDS: tuple[str, ...] = (
    "streetlight", "streetlights", "light", "lights", "lamp", "lamps",
    "lantern", "lanterns", "torch", "torches", "sign", "signs", "signpost",
    "marker", "markers", "pylon", "pylons", "cone", "cones", "flag", "flags",
    "banner", "banners", "post", "posts", "bollard", "bollards",
)
_BOUNDARY_FOLLOW_WORDS: tuple[str, ...] = (
    "barrier", "barriers", "fence", "fences", "fencing", "guardrail",
    "guardrails", "railing", "railings", "wall", "walls", "hedge", "hedges",
    "rope", "ropes", "tire wall", "tyre wall", "crash barrier",
)


def is_route_gate_entity(entity) -> bool:
    """This group spans the route and is crossed AT a point — a checkpoint
    or finish arch — rather than standing beside it (edge_distributed) or
    running along it (boundary_follow). An authored kind always outranks
    prose, same precedence every other role predicate in this module uses."""
    if (entity.kind or "").lower().strip() in _ROUTE_GATE_KINDS:
        return True
    return bool(_hits(_entity_words(entity), _ROUTE_GATE_WORDS))


def is_finish_gate_entity(entity) -> bool:
    """This gate is THE finish — one marker at the end of the route, not one
    of `entity.count` checkpoints spread along its length."""
    if (entity.kind or "").lower().strip() in _FINISH_GATE_KINDS:
        return True
    if (entity.kind or "").lower().strip() == "checkpoint":
        return False
    # Identity outranks asset-search hints ("checkpoint banner finish line"
    # describes a reusable model, not permission to delete checkpoints).
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", entity.name).lower()
    if _hits(name, ("checkpoint", "checkpoints")):
        return False
    return bool(_hits(_entity_words(entity), _FINISH_GATE_WORDS))
# A COHESIVE in-game unit/squad/pack that belongs staying together in one
# spot — rival karts at a start grid, a squad of guards — as opposed to
# decorative/scenery content whose `count` means "there are this many of
# this thing around the level", not "these instances form one group". This
# is the line between the two remaining relationships below: a formation
# keeps today's original single-anchor placement (correct here — these
# instances ARE meant to share one spot); everything else now spreads
# across the level instead (see "scenery_distributed" below and
# src/unity_layout._scenery_distributed_pack) — replacing what used to be
# an unconditional single-anchor default for EVERY unclassified group,
# which is exactly the "clusters of the same model in one place" complaint
# that motivated this table's existence in the first place.
#
# kind carries the actor signal; tag carries the relational-role signal
# independent of kind, since live schema-fill output puts hostility in
# `tag` far more reliably than in `kind` (see unity_concept_entities.py's
# own tag="Enemy" detection). Either one is enough — a `kind="prop"
# tag="Enemy"` landmine still reads as a formation, correctly, rather than
# getting scattered like a prop.
_FORMATION_KINDS: frozenset[str] = frozenset({
    "vehicle", "aircraft", "character", "creature",
})
_FORMATION_TAGS: frozenset[str] = frozenset({"enemy", "ally", "player"})


def is_formation_entity(entity) -> bool:
    return ((entity.kind or "").lower().strip() in _FORMATION_KINDS
            or _tag_of(entity) in _FORMATION_TAGS)


def spatial_relationship_of(entity) -> str:
    """How this packable group relates to an authored route corridor:

      "route_gate"          — spans the route and is crossed AT a point: a
                              checkpoint or finish arch (`is_route_gate_
                              entity`). Centered ON the centerline, facing
                              along the direction of travel — see
                              `_route_gate_pack` (src/unity_layout.py).
      "edge_distributed"    — one instance per arc-length interval along the
                              WHOLE route, offset outside the edge, facing
                              back toward the road (streetlights, torches,
                              signs).
      "boundary_follow"     — a continuous chain hugging one side of the
                              route (barriers, fences, guardrails).
      "formation"           — a cohesive squad/pack (`is_formation_entity`)
                              that stays at ONE anchor — today's original
                              single-anchor-plus-local-pattern behaviour,
                              correct here, not a leftover default.
      "scenery_distributed" — the actual default: everything else
                              (spectators, pit equipment, trees, rocks,
                              collectibles...) spreads across multiple stops
                              along the route instead of one clustered
                              anchor. A small group collapses to one stop
                              anyway (see _scenery_distributed_pack), so a
                              handful of decorative instances still lands
                              exactly where the old single-anchor behaviour
                              would have put them.

    Checked in this order: route-gate FIRST — a live schema names a
    checkpoint group "CheckpointBanners", which also hits
    _EDGE_DISTRIBUTED_WORDS ("banner"), and the more specific claim (spans
    the route) has to win before the weaker one (stands beside it) gets a
    chance. Then edge-distributed and boundary-follow (a name that could
    plausibly read as either, e.g. "light fence", is rare in practice, but
    ties go to the more specific relationship); formation checked before
    falling through to the scenery default, since a squad that also happens
    to carry a matched keyword — unlikely, but a fence-named tag="Enemy"
    group is not impossible — should still read as edge/boundary first
    (that's about the OBJECT the group physically is, which
    formation-vs-scenery doesn't override).
    """
    if is_route_gate_entity(entity):
        return "route_gate"
    words = _entity_words(entity)
    if _hits(words, _EDGE_DISTRIBUTED_WORDS):
        return "edge_distributed"
    if _hits(words, _BOUNDARY_FOLLOW_WORDS):
        return "boundary_follow"
    if is_formation_entity(entity):
        return "formation"
    return "scenery_distributed"


# ── UISurface roles ──────────────────────────────────────────────────────────
# A non-embodied game's entities are not scenery standing around an avatar —
# they ARE the interface, and their identity is a UI element rather than a
# prop. One table answers that question for both passes that need it: the
# layout (which cell a group takes on the board, which z-layer it sits on) and
# the asset resolver (which `ui:` element it is built as, since the library
# holds fifteen HUD glyphs and no UI kit — see
# src/unity_entity_asset_resolver.py). Two tables would let a group be laid
# out as a panel and built as an icon.
#
# Ordered most-specific first: the first row whose words hit wins, so a
# "TerminalWindow" is a window and not merely a panel, and a "CommitNode" is a
# node-card rather than an unlabelled icon.
_UI_ELEMENT_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("window", ("window", "windows", "terminal", "terminals", "console",
                "consoles", "editor", "editors", "viewport", "workspace",
                "canvas", "screen", "screens", "monitor", "display",
                "dashboard", "inspector", "browser")),
    ("list",   ("list", "lists", "listing", "log", "logs", "history",
                "timeline", "queue", "feed", "table", "grid view", "tree view",
                "column", "columns", "inbox", "changelog", "stack trace")),
    ("panel",  ("panel", "panels", "pane", "panes", "board", "boards",
                "sidebar", "side panel", "toolbar", "palette", "tray",
                "container", "containers", "folder", "folders", "drawer",
                "section", "region", "zone", "slot", "slots", "well",
                # PLAYING SURFACES. A board game's places are not called
                # panels — they are areas, mats, racks, piles and lanes — and
                # without them a cast of pieces has nowhere legal to go, so
                # board.board_options_for_entities abstained and no legal-move
                # logic was composed at all. Measured: 27 of 39 ordinary
                # board-game nouns classified as nothing.
                "area", "areas", "surface", "surfaces", "tabletop", "table top",
                "mat", "mats", "playmat", "play mat", "rack", "racks", "bay",
                "bays", "lane", "lanes", "bin", "bins", "shelf", "shelves",
                "pile", "piles", "stack", "stacks", "hand", "hands",
                "grid", "grids", "track", "tracks", "space", "spaces",
                "station", "stations", "dock", "workbench", "bench", "field",
                "fields", "square", "squares", "tile grid", "hex", "hexes")),
    ("button", ("button", "buttons", "switch", "switches", "toggle",
                "toggles", "control", "controls", "key", "keys", "lever",
                "action", "actions", "command", "commands", "menu item")),
    ("label",  ("label", "labels", "caption", "captions", "title", "titles",
                "header", "headers", "heading", "footer", "legend", "legends",
                "tooltip", "tooltips", "hint", "hints", "readout", "banner",
                "annotation", "callout", "status bar", "breadcrumb")),
    ("card",   ("card", "cards", "tile", "tiles", "node", "nodes", "commit",
                "commits", "branch", "branches", "ticket", "tickets", "task",
                "tasks", "note", "notes", "entry", "entries", "record",
                "records", "row", "rows", "cell", "cells", "chip", "badge",
                "token", "tokens", "block", "blocks",
                # THE THINGS YOU MOVE. Same gap as the playing surfaces above,
                # from the other side: a cast of pawns and meeples with no
                # place to go is the same dead board as a cast of places with
                # nothing to put in them.
                "piece", "pieces", "pawn", "pawns", "counter", "counters",
                "meeple", "meeples", "chips", "disc", "discs", "disk", "disks",
                "stone", "stones", "die", "dice", "domino", "dominoes",
                "coin", "coins", "gem", "gems", "cube", "cubes", "bead",
                "beads", "peg", "pegs", "checker", "checkers", "brick",
                "bricks", "figurine", "figurines")),
    ("icon",   ("icon", "icons", "glyph", "glyphs", "avatar", "thumbnail",
                "marker", "markers", "pin", "pins", "dot", "sprite", "symbol",
                "file", "files", "document", "documents", "cursor")),
)
# Kinds that name the element outright. The LLM writes kind="prop" for most of
# a UI game's content (that is the whole reason the `ui:` route exists), but
# when it does reach for a UI kind it is the strongest signal available.
#
# `pickup`/`collectible`/`item` are not UI words — they are the IR's own words
# for a thing the player TAKES, and on a board that is exactly a piece. They are
# here because leaving them out cost a whole board: a lost-property sim filled
# kind="pickup", count=12 for its only movable group and named it
# `LostPropertyPile`, so the kind said nothing, the word table read "pile" as a
# place to stack onto, and a cast of twelve draggable objects classified as two
# panels with nothing to put in them. `board_options_for_entities` abstained,
# correctly, from a cast that was never actually short of pieces.
#
# Read only for a non-embodied project — `ui_element_of`'s callers are the UI
# board layout, the board micro (`locomotion='none'` only) and the asset
# resolver's `if not embodied` branch — so an ordinary 3D game's pickups are
# untouched and still resolve to real models.
_UI_ELEMENT_KINDS: dict[str, str] = {
    "ui": "panel", "hud": "label", "menu": "panel", "widget": "panel",
    "panel": "panel", "window": "window", "button": "button",
    "label": "label", "text": "label", "icon": "icon", "card": "card",
    "tile": "card", "list": "list",
    "pickup": "card", "collectible": "card", "item": "card",
}
# Behind everything, filling the frame: not content, backing. Kept apart from
# the element table because a backdrop takes no cell in the reading flow — it
# is the surface the flow is laid on.
_UI_BACKDROP_KINDS: frozenset[str] = frozenset({
    "background", "backdrop", "skybox", "sky", "terrain", "ground", "floor",
    "wallpaper", "environment",
})
_UI_BACKDROP_WORDS: tuple[str, ...] = (
    "background", "backdrop", "wallpaper", "backboard", "back plate",
    "backplate", "vignette", "grid lines", "gridlines",
)
# Elements drawn IN FRONT of the board rather than on it: chrome that reads
# the game rather than being played. A label is the archetype; a button is
# NOT — a button is the game.
_UI_OVERLAY_ELEMENTS: frozenset[str] = frozenset({"label"})

# Reading-order rank. The board is read the way a page is: the thing being
# worked in, then the things worked with, then everything else, then the
# chrome that annotates it. Deliberately NOT size-ordered — `_pack_groups`
# sorts by footprint so the biggest circles claim the centre, which is right
# for a world and wrong for an interface, where position carries meaning.
_UI_RANK_BY_ELEMENT: dict[str, int] = {
    "window": 0, "panel": 1, "list": 1,
    "card": 2, "button": 2, "icon": 3,
    "label": 4,
}
_UI_RANK_DEFAULT = 3


# Phrases that name a PLAYING SURFACE and contain a word an earlier row claims.
# The table is first-row-wins, so "table top" was resolved by `table` in the
# `list` row and a surface you drop pieces onto became a read-only log. Checked
# ahead of the table rather than reordering it: `table` really is a list in a
# UI, and only the compound says otherwise.
_UI_SURFACE_OVERRIDES: tuple[str, ...] = (
    "table top", "tabletop", "game table", "card table", "board surface",
    "play area", "playing area", "playing field", "game board",
)


def ui_element_of(entity) -> Optional[str]:
    """Which UI element this group reads as — or None when nothing says.

    None is a real answer and the callers treat it as one: the layout gives an
    unclassified group an ordinary board cell, and the asset resolver lets it
    keep looking for a real model. A teaching game about an engine legitimately
    contains an engine.
    """
    kind = (entity.kind or "").lower().strip()
    hit = _UI_ELEMENT_KINDS.get(kind)
    if hit:
        return hit
    words = _entity_words(entity)
    if _hits(words, _UI_SURFACE_OVERRIDES):
        return "panel"
    for element, needles in _UI_ELEMENT_TABLE:
        if _hits(words, needles):
            return element
    return None


def is_ui_backdrop(entity) -> bool:
    """Backing for the board rather than content on it."""
    if (entity.kind or "").lower().strip() in _UI_BACKDROP_KINDS:
        return True
    return bool(_hits(_entity_words(entity), _UI_BACKDROP_WORDS))


def ui_board_layer(entity) -> str:
    """Which z-plane of the board this group belongs on: 'backdrop' (behind
    the content), 'board' (the content itself) or 'overlay' (chrome in front
    of it). Three planes, so a caption never occludes the thing it captions
    and a backboard never hides the board."""
    if is_ui_backdrop(entity):
        return "backdrop"
    if ui_element_of(entity) in _UI_OVERLAY_ELEMENTS:
        return "overlay"
    return "board"


def ui_board_rank(entity) -> int:
    """Reading-order rank, low first. Ties are broken by schema order at the
    call site, never by size — an interface is read, not surveyed."""
    element = ui_element_of(entity)
    if element is None:
        return _UI_RANK_DEFAULT
    return _UI_RANK_BY_ELEMENT.get(element, _UI_RANK_DEFAULT)


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
    "spatial_relationship_of", "is_route_gate_entity", "is_finish_gate_entity",
]
