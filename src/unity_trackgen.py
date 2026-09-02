"""
Procedural race-circuit CENTERLINE generator — a bpy-free port of the LAYOUT
stage of _blender/racetracksgen.py (the "winner" parametric track: randomized
left AND right sweepers, long straights, linked corners and hairpins, with
curvature-derived banking).

Why a port and not a call: racetracksgen.py builds a Blender MESH (it imports
`bpy`, `bmesh`, `mathutils`) and is meant to run inside Blender. The Unity
pipeline never needs the mesh — the circuit's ROAD, barriers, cones, banners
and karts are authored as tiled path entities by src/unity_racetrack.py and
built at runtime by EntitySpawner. All that module needs from a track is its
CENTERLINE: a list of [x, y, z(, bank°)] points. So this file reproduces only
the layout math (Catmull-Rom control loop → arc-length resample → signed
curvature → banking), in pure stdlib, and hands the result to the SAME
`unity-create --route` seam a hand-authored or image-traced course uses
(src/unity_pathinput.build_route → normalize → compose_racetrack). No new
placement code: the generator only decides the shape of the loop.

Deterministic: a local `random.Random(seed)` (never the global RNG), pure
arithmetic, fixed tie-breaks — same (style, seed) → byte-identical route, in
the spirit of racetracksgen.py's own `SEED`. No bpy, no LLM.

Elevation is flat (y = 0) BY DEFAULT, and still is in the only place that ships
today: src/unity_racetrack.py rides the centerline's y for the road surface,
but its start grid and finish-line marker hardcode y=0.0 independent of the
centerline, and there is no track-conforming terrain pass — a tiled road
riding non-zero elevation over Unity's flat ground plane would float/dip with
nothing under it, and karts would spawn off the surface. So `amplitude`
(below) is a tested, opt-in elevation PRIMITIVE only: pass it explicitly if
you're building a new consumer that also fixes the start-grid/finish-line
assumption and the ground-plane question; every existing caller (including
unity_racetrack.py) leaves it at the 0.0 default and gets today's exact flat
output, byte-for-byte. Banking (roll from curvature) rides along as the 4th
path component, which the schema and normalizer already preserve.
"""

from __future__ import annotations

import math
import random

from .unity_pathinput import build_route

# ── Unity-scaled defaults ─────────────────────────────────────────────────────
# racetracksgen.py targets real-world lengths (3000 m circumference); a Unity
# scene wants a circuit comparable to the parametric oval in unity_racetrack.py
# (~250 m perimeter) — big enough to read as a real track, small enough that
# 64 path points (the schema cap) still resolve corners and everything stays
# inside the ±2000 m coordinate clamp.
_DEFAULT_LENGTH = 600.0          # target circumference (meters)
_DEFAULT_WIDTH = 10.0            # drivable width (meters) → route path_width
# One station under the schema's 64-point path cap (a closed loop appends a
# closing duplicate: 62 distinct + 1 = 63 ≤ 64). Generating AT the cap means
# build_route passes the loop through without a second arc-length resample, so
# the banking computed per station survives to the route verbatim.
_DEFAULT_RESOLUTION = 62
_SAMPLES_PER_SEGMENT = 24        # dense spline sampling before arc-length resample
_BANK_SMOOTH = 3                 # curvature smoothing passes (kills banking jitter)
_MAX_GEN_ATTEMPTS = 60           # regenerate until the layout is clean
_ELEV_KEYFRAMES = 6              # seeded height control points spaced around the loop

# Layout-shaping presets — the SHAPE knobs from racetracksgen.py's
# STYLE_PRESETS. TRACK_LENGTH/TRACK_WIDTH overrides are intentionally dropped:
# length/width are Unity-scaled parameters here so every style stays
# scene-appropriate. Keys mirror the original for cross-reference.
_STYLE_PRESETS: dict[str, dict[str, float]] = {
    "flowing": {
        "control_points": 12, "angle_jitter": 0.50, "radius_walk": 0.22,
        "radius_min": 0.55, "radius_max": 1.70, "hairpin_chance": 0.05,
        "hairpin_tightness": 0.45, "banking_max": 22.0,
    },
    "technical": {
        "control_points": 18, "angle_jitter": 0.85, "radius_walk": 0.30,
        "radius_min": 0.40, "radius_max": 1.50, "hairpin_chance": 0.22,
        "hairpin_tightness": 0.28, "banking_max": 14.0,
    },
    "high_speed": {
        "control_points": 10, "angle_jitter": 0.90, "radius_walk": 0.14,
        "radius_min": 0.60, "radius_max": 2.00, "hairpin_chance": 0.03,
        "hairpin_tightness": 0.50, "banking_max": 32.0,
    },
    "kart": {
        "control_points": 16, "angle_jitter": 0.80, "radius_walk": 0.28,
        "radius_min": 0.38, "radius_max": 1.40, "hairpin_chance": 0.28,
        "hairpin_tightness": 0.30, "banking_max": 8.0,
    },
}

# Banking is written to the route in degrees; the schema clamps bank to ±45,
# so cap the request well under that regardless of preset/style.
_BANK_CAP_DEG = 40.0

STYLES = tuple(_STYLE_PRESETS) + ("random",)


def _random_style(rng: random.Random) -> dict[str, float]:
    """A fresh random SHAPE (deterministic for a given rng) — the port of
    racetracksgen._random_style, shape knobs only."""
    return {
        "control_points": rng.randint(10, 18),
        "angle_jitter": round(rng.uniform(0.40, 0.95), 2),
        "radius_walk": round(rng.uniform(0.12, 0.32), 2),
        "radius_min": round(rng.uniform(0.38, 0.60), 2),
        "radius_max": round(rng.uniform(1.40, 2.00), 2),
        "hairpin_chance": round(rng.uniform(0.00, 0.28), 2),
        "hairpin_tightness": round(rng.uniform(0.28, 0.50), 2),
        "banking_max": round(rng.uniform(8.0, 32.0), 1),
    }


def _resolve_params(style: str, rng: random.Random) -> dict[str, float]:
    if style == "random":
        return _random_style(rng)
    preset = _STYLE_PRESETS.get(style)
    if preset is None:
        raise ValueError(
            f"unknown track style {style!r}; choose one of {', '.join(STYLES)}")
    return dict(preset)


# ── 2D XZ vector helpers (Blender's mathutils.Vector, stripped to what we use) ─
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _add(a, b): return (a[0] + b[0], a[1] + b[1])
def _sub(a, b): return (a[0] - b[0], a[1] - b[1])
def _scale(a, s): return (a[0] * s, a[1] * s)
def _dist(a, b): return math.hypot(a[0] - b[0], a[1] - b[1])
def _length(a): return math.hypot(a[0], a[1])


def _catmull_rom_centripetal(p0, p1, p2, p3, t, alpha=0.5):
    """Centripetal Catmull-Rom interpolation on the p1->p2 segment (t in 0..1),
    2D port of racetracksgen.catmull_rom_centripetal."""
    def knot(ti, a, b):
        return ti + max(_dist(a, b), 1e-6) ** alpha

    t0 = 0.0
    t1 = knot(t0, p0, p1)
    t2 = knot(t1, p1, p2)
    t3 = knot(t2, p2, p3)
    tt = t1 + (t2 - t1) * t

    a1 = _add(_scale(p0, (t1 - tt) / (t1 - t0)), _scale(p1, (tt - t0) / (t1 - t0)))
    a2 = _add(_scale(p1, (t2 - tt) / (t2 - t1)), _scale(p2, (tt - t1) / (t2 - t1)))
    a3 = _add(_scale(p2, (t3 - tt) / (t3 - t2)), _scale(p3, (tt - t2) / (t3 - t2)))
    b1 = _add(_scale(a1, (t2 - tt) / (t2 - t0)), _scale(a2, (tt - t0) / (t2 - t0)))
    b2 = _add(_scale(a2, (t3 - tt) / (t3 - t1)), _scale(a3, (tt - t1) / (t3 - t1)))
    return _add(_scale(b1, (t2 - tt) / (t2 - t1)), _scale(b2, (tt - t1) / (t2 - t1)))


def _sample_closed_spline(cps, per_seg):
    n = len(cps)
    out = []
    for i in range(n):
        p0, p1 = cps[(i - 1) % n], cps[i]
        p2, p3 = cps[(i + 1) % n], cps[(i + 2) % n]
        for s in range(per_seg):
            out.append(_catmull_rom_centripetal(p0, p1, p2, p3, s / per_seg))
    return out


def _closed_length(points):
    n = len(points)
    return sum(_dist(points[(i + 1) % n], points[i]) for i in range(n))


def _resample_closed(points, count):
    """Resample a closed polyline into `count` evenly arc-length-spaced points."""
    import bisect
    n = len(points)
    seg = [_dist(points[(i + 1) % n], points[i]) for i in range(n)]
    total = sum(seg)
    cum = [0.0]
    for s in seg:
        cum.append(cum[-1] + s)
    out = []
    for k in range(count):
        target = (k / count) * total
        j = min(max(bisect.bisect_right(cum, target) - 1, 0), n - 1)
        frac = (target - cum[j]) / seg[j] if seg[j] > 1e-9 else 0.0
        out.append(_add(points[j], _scale(_sub(points[(j + 1) % n], points[j]), frac)))
    return out


def _smooth_closed(vals, passes):
    for _ in range(passes):
        n = len(vals)
        vals = [(vals[(i - 1) % n] + 2 * vals[i] + vals[(i + 1) % n]) / 4.0
                for i in range(n)]
    return vals


def _seg_intersect(a, b, c, d):
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0])
    return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)


def _self_intersects(pts):
    """True if the closed centerline crosses itself (ignoring adjacent edges)."""
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _seg_intersect(a, b, pts[j], pts[(j + 1) % n]):
                return True
    return False


def _generate_control_points(base_radius, rng, p):
    """Place control points around a closed loop with randomized angular
    spacing (straights vs. clustered corners) and a random-walk radius
    (sweeping in/out, i.e. left and right curvature). Some become hairpins.
    2D XZ port of racetracksgen.generate_control_points."""
    n = int(p["control_points"])
    jitter = p["angle_jitter"]
    steps = [max(0.15, 1.0 + rng.uniform(-jitter, jitter)) for _ in range(n)]
    total_steps = sum(steps)

    points = []
    angle = rng.uniform(0, 2 * math.pi)
    radius = base_radius
    for i in range(n):
        radius += rng.uniform(-1, 1) * base_radius * p["radius_walk"]
        radius = _clamp(radius, base_radius * p["radius_min"],
                        base_radius * p["radius_max"])
        r = radius
        if rng.random() < p["hairpin_chance"]:
            r = radius * p["hairpin_tightness"]
        points.append((math.cos(angle) * r, math.sin(angle) * r))
        angle += (steps[i] / total_steps) * 2 * math.pi
    return points


def _elevation_profile(n: int, rng: random.Random, amplitude: float,
                       keyframes: int = _ELEV_KEYFRAMES) -> list[float]:
    """Deterministic closed-loop value noise: seed `keyframes` random heights
    evenly spaced around the loop, cosine-interpolate between them indexed by
    each point's position in the arc-length-uniform points array (a good
    arc-length proxy, since _resample_closed already produced it).

    amplitude<=0 draws NOTHING from `rng` and returns an all-zero profile —
    this is the safety property every existing caller relies on for
    byte-identical output at the 0.0 default."""
    if amplitude <= 0.0 or n <= 0:
        return [0.0] * n
    heights = [rng.uniform(-amplitude, amplitude) for _ in range(keyframes)]
    out = []
    for i in range(n):
        pos = (i / n) * keyframes
        k0 = int(pos) % keyframes
        k1 = (k0 + 1) % keyframes
        frac = pos - math.floor(pos)
        mu = (1.0 - math.cos(frac * math.pi)) / 2.0   # smooth at keyframe joins
        out.append(round(heights[k0] * (1.0 - mu) + heights[k1] * mu, 3))
    return out


def _build_centerline(control_points, length, resolution, banking_max,
                      rng: random.Random | None = None, amplitude: float = 0.0):
    """Turn control points into (points_xz, banks_deg, tightest_radius, elev).

    Points are arc-length-uniform around the loop, scaled to `length`; banking
    is the signed curvature normalized to ±banking_max degrees. 2D XZ port of
    racetracksgen.build_centerline. `elev` is the optional value-noise height
    profile from `_elevation_profile` — see the module docstring for why it
    defaults to (and stays) all-zero unless a caller explicitly opts in."""
    dense = _sample_closed_spline(control_points, _SAMPLES_PER_SEGMENT)
    pts = _resample_closed(dense, resolution)

    total = _closed_length(pts)
    if total > 0:
        scale = length / total
        pts = [_scale(pp, scale) for pp in pts]

    n = len(pts)
    curv = []
    for i in range(n):
        pm, pc, pn = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        a, b = _sub(pc, pm), _sub(pn, pc)
        cross = a[0] * b[1] - a[1] * b[0]          # + = left, - = right
        dot = a[0] * b[0] + a[1] * b[1]
        ds = (_length(a) + _length(b)) * 0.5
        curv.append(math.atan2(cross, dot) / ds if ds > 1e-9 else 0.0)

    curv = _smooth_closed(curv, _BANK_SMOOTH)
    max_curv = max((abs(c) for c in curv), default=0.0) or 1e-9
    tightest = 1.0 / max_curv
    cap = min(banking_max, _BANK_CAP_DEG)
    banks = [round((c / max_curv) * cap, 3) for c in curv]

    # amplitude is capped as a fraction of track length (same spirit as
    # banking_max/_BANK_CAP_DEG capping bank degrees) rather than coupled to
    # curvature — climb and turn stay independent signals, on purpose.
    elev_amp = min(amplitude, length * 0.05) if amplitude > 0.0 else 0.0
    elev = _elevation_profile(n, rng or random.Random(0), elev_amp)
    return pts, banks, tightest, elev


def generate_centerline(style: str = "flowing", seed: int = 42,
                        length: float = _DEFAULT_LENGTH,
                        width: float = _DEFAULT_WIDTH,
                        resolution: int = _DEFAULT_RESOLUTION,
                        elevation_amplitude: float = 0.0,
                        ) -> list[list[float]]:
    """Generate a clean closed race centerline as [x, y, z, bank°] points
    (last duplicates the first — the closed-loop convention build_route and
    compose_racetrack expect). Regenerates until the layout neither folds a
    corner tighter than the track can support nor crosses itself, mirroring
    racetracksgen.generate_track's validation. Deterministic in (style, seed).

    `elevation_amplitude` defaults to 0.0 — y stays exactly 0.0 for every
    point, byte-identical to this function's pre-elevation output. Non-zero
    values are a tested primitive only; see the module docstring before
    wiring this into any consumer.
    """
    rng = random.Random(seed)
    params = _resolve_params(style, rng)
    base_radius = length / (2 * math.pi)
    min_corner = width * 0.9
    resolution = max(8, min(int(resolution), 62))

    last: tuple[list, list, float, list] | None = None
    for _ in range(_MAX_GEN_ATTEMPTS):
        cps = _generate_control_points(base_radius, rng, params)
        pts, banks, tightest, elev = _build_centerline(
            cps, length, resolution, params["banking_max"],
            rng=rng, amplitude=elevation_amplitude)
        last = (pts, banks, tightest, elev)
        if tightest < min_corner:
            continue
        if _self_intersects(pts):
            continue
        break

    assert last is not None
    pts, banks, _tightest, elev = last
    route_pts: list[list[float]] = [
        [round(x, 3), y, round(z, 3), bank]
        for (x, z), bank, y in zip(pts, banks, elev)
    ]
    route_pts.append(list(route_pts[0]))   # close the loop
    return route_pts


def generate_track_route(style: str = "flowing", seed: int = 42,
                         length: float = _DEFAULT_LENGTH,
                         width: float = _DEFAULT_WIDTH,
                         resolution: int = _DEFAULT_RESOLUTION,
                         elevation_amplitude: float = 0.0,
                         ) -> dict:
    """Generate a procedural circuit as a canonical `unity-create --route`
    dict — identical shape to what `unity-path` writes from text/image, so it
    flows through the same build_route (cap + close) and normalize_route (P1 at
    origin, heading +Z) path, then into compose_racetrack unchanged.

    Returns {path_points, path_width, closed: True, source: "trackgen:<style>"}.
    `elevation_amplitude` defaults to 0.0 — see generate_centerline.
    """
    pts = generate_centerline(style, seed, length, width, resolution,
                              elevation_amplitude=elevation_amplitude)
    route = build_route(pts, width=width, close=True,
                        source=f"trackgen:{style}")
    route["seed"] = int(seed)
    route["style"] = style
    return route
