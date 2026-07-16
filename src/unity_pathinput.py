"""
Deterministic path-input front door (the `unity-path` CLI command, main.py):
turns TEXT point specs or a top-down IMAGE of a course into a canonical
P1-normalized route file (≤64 [x, y, z(, bank)] points — the schema's
path_points cap) that `unity-create --route` feeds to the racetrack composer.

Two sources, one output contract:

  TEXT   "x,z ; x,z ; ..." (2, 3 or 4 numbers per point: x,z | x,y,z |
         x,y,z,bank°). Authored in meters; >64 points are resampled
         arc-length-uniform down to the cap (bank/elevation lerped).

  IMAGE  a dark line drawn on a light background (or inverted — detected
         automatically). Threshold → Zhang-Suen thinning to a 1-px skeleton
         → ordered nearest-neighbor walk → Douglas-Peucker simplification to
         ≤64 points → scaled so the drawing's larger extent spans
         `world_size` meters (image y-down flipped to world +Z-up). Pure
         Pillow + stdlib; imported lazily so environments without Pillow
         only fail when an image is actually requested. Bank/elevation can't
         come from a top-down drawing — points carry y=0, no bank.

Both are normalized through src/unity_pathnorm.normalize_route, so the
emitted route already honors the P1 invariants (P1 at the origin, travel
direction +Z, P1 = start line). Everything here is deterministic: no RNG,
no LLM, fixed tie-breaks.
"""

from __future__ import annotations

import math

from .unity_pathnorm import normalize_route

MAX_POINTS = 64          # schema cap for EntitySpec.path_points
# Endpoints closer than this fraction of total arc length close the loop.
_CLOSE_FRACTION = 0.05
# Images are downscaled so their larger side never exceeds this (bounds the
# pure-Python thinning cost; plenty of resolution for a 64-point route).
_MAX_IMAGE_SIDE = 384


# ── text input ───────────────────────────────────────────────────────────────

def parse_points_text(spec: str) -> list[list[float]]:
    """Parse 'x,z ; x,z ; ...' (separators: ';' or newlines) into [x, y, z]
    / [x, y, z, bank] points. 2 numbers = x,z on the ground plane; 3 =
    x,y,z; 4 = x,y,z,bank°. Raises ValueError on anything malformed —
    explicit authored input should fail loudly, not degrade."""
    points: list[list[float]] = []
    for raw in spec.replace("\n", ";").split(";"):
        token = raw.strip()
        if not token:
            continue
        try:
            nums = [float(n) for n in token.split(",")]
        except ValueError as exc:
            raise ValueError(f"unparseable point {token!r}") from exc
        if len(nums) == 2:
            points.append([nums[0], 0.0, nums[1]])
        elif len(nums) in (3, 4):
            points.append(nums)
        else:
            raise ValueError(
                f"point {token!r} has {len(nums)} numbers (want 2, 3 or 4)")
    if len(points) < 2:
        raise ValueError("a route needs at least 2 points")
    return points


# ── shared polyline helpers ──────────────────────────────────────────────────

def _arc_lengths(points: list[list[float]]) -> list[float]:
    """Cumulative XZ arc length at each point (starts at 0.0)."""
    acc, out = 0.0, [0.0]
    for a, b in zip(points, points[1:]):
        acc += math.hypot(b[0] - a[0], b[2] - a[2])
        out.append(acc)
    return out

def _lerp_point(a: list[float], b: list[float], t: float) -> list[float]:
    """Component-wise lerp incl. elevation; bank lerps too (absent = 0, and
    a resulting 0 bank is dropped to keep the plain-triple convention)."""
    ba, bb = (a[3] if len(a) > 3 else 0.0), (b[3] if len(b) > 3 else 0.0)
    p = [round(a[i] + (b[i] - a[i]) * t, 3) for i in range(3)]
    bank = round(ba + (bb - ba) * t, 3)
    return p + ([bank] if bank != 0.0 else [])

def resample_polyline(points: list[list[float]],
                      max_points: int = MAX_POINTS) -> list[list[float]]:
    """Arc-length-uniform resample down to `max_points` (only when over the
    cap — authored geometry at or under it passes through untouched). A
    closed input (first == last) stays closed: the closing duplicate is one
    of the `max_points`."""
    if len(points) <= max_points:
        return [list(p) for p in points]
    closed = points[0][:3] == points[-1][:3]
    lengths = _arc_lengths(points)
    total = lengths[-1] or 1.0
    n = max_points - 1 if closed else max_points
    step = total / n if closed else total / (n - 1)
    out, seg = [], 0
    for i in range(n):
        target = min(i * step, total)
        while seg < len(points) - 2 and lengths[seg + 1] < target:
            seg += 1
        span = lengths[seg + 1] - lengths[seg]
        t = 0.0 if span <= 0 else (target - lengths[seg]) / span
        out.append(_lerp_point(points[seg], points[seg + 1], t))
    if closed:
        out.append(list(out[0]))
    return out

def close_if_near(points: list[list[float]]) -> tuple[list[list[float]], bool]:
    """Append the closing duplicate when the endpoints sit within
    `_CLOSE_FRACTION` of the total arc length of each other (a hand-drawn
    'closed' loop rarely lands pixel-exact). Returns (points, closed)."""
    if len(points) >= 3 and points[0][:3] == points[-1][:3]:
        return points, True
    total = _arc_lengths(points)[-1]
    gap = math.hypot(points[-1][0] - points[0][0], points[-1][2] - points[0][2])
    if len(points) >= 3 and total > 0 and gap <= total * _CLOSE_FRACTION:
        return points + [list(points[0][:3])], True
    return points, False


def _dp_simplify(points: list[list[float]], epsilon: float) -> list[list[float]]:
    """Iterative Douglas-Peucker over XZ (y/bank ride along with the kept
    points). Deterministic; keeps endpoints."""
    if len(points) < 3:
        return [list(p) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        ax, az = points[lo][0], points[lo][2]
        bx, bz = points[hi][0], points[hi][2]
        dx, dz = bx - ax, bz - az
        seg2 = dx * dx + dz * dz
        best, best_i = -1.0, -1
        for i in range(lo + 1, hi):
            px, pz = points[i][0] - ax, points[i][2] - az
            if seg2 == 0:
                d = math.hypot(px, pz)
            else:
                t = max(0.0, min(1.0, (px * dx + pz * dz) / seg2))
                d = math.hypot(px - t * dx, pz - t * dz)
            if d > best:
                best, best_i = d, i
        if best > epsilon and best_i > 0:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [list(p) for p, k in zip(points, keep) if k]


def simplify_to_cap(points: list[list[float]],
                    max_points: int = MAX_POINTS) -> list[list[float]]:
    """Douglas-Peucker with epsilon doubled until the polyline fits the cap
    (traced image skeletons arrive with hundreds of pixel-spaced points)."""
    if len(points) <= max_points:
        return [list(p) for p in points]
    epsilon = 0.25
    for _ in range(32):
        simplified = _dp_simplify(points, epsilon)
        if len(simplified) <= max_points:
            return simplified
        epsilon *= 2.0
    return resample_polyline(points, max_points)  # pathological fallback


# ── image input ──────────────────────────────────────────────────────────────

def _threshold_pixels(img) -> set[tuple[int, int]]:
    """(x, y) set of 'line' pixels: the minority side of a fixed mid-gray
    threshold, so dark-on-light and light-on-dark drawings both work."""
    w, h = img.size
    # Pillow 12 renames getdata -> get_flattened_data (getdata is deprecated,
    # removed in 14); support both so requirements' pillow>=10 floor holds.
    getter = getattr(img, "get_flattened_data", None) or img.getdata
    data = list(getter())
    dark = {(i % w, i // w) for i, v in enumerate(data) if v < 128}
    if len(dark) > w * h / 2:                      # light line on dark bg
        dark = {(i % w, i // w) for i, v in enumerate(data) if v >= 128}
    return dark

_N8 = ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1))

def _thin(pixels: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Zhang-Suen thinning to a 1-px skeleton (deterministic; operates on a
    sparse set so cost scales with line pixels, not image area)."""
    skel = set(pixels)
    while True:
        changed = False
        for phase in (0, 1):
            to_remove = []
            for (x, y) in skel:
                nb = [(x + dx, y + dy) in skel for dx, dy in _N8]
                count = sum(nb)
                if not (2 <= count <= 6):
                    continue
                transitions = sum(
                    1 for i in range(8) if not nb[i] and nb[(i + 1) % 8])
                if transitions != 1:
                    continue
                p2, p4, p6, p8 = nb[0], nb[2], nb[4], nb[6]
                if phase == 0:
                    if (p2 and p4 and p6) or (p4 and p6 and p8):
                        continue
                else:
                    if (p2 and p4 and p8) or (p2 and p6 and p8):
                        continue
                to_remove.append((x, y))
            if to_remove:
                skel.difference_update(to_remove)
                changed = True
        if not changed:
            return skel

def _walk_skeleton(skel: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Ordered pixel chain: start at an endpoint (exactly 1 skeleton
    neighbor) when one exists — an open course — else at the lexicographic
    minimum (a closed loop); greedily step to the nearest-unvisited
    8-neighbor (lexicographic tie-break). Zhang-Suen leaves staircase
    artifacts (1-px pockets off the main line) that can strand the greedy
    walk mid-loop, so a stuck walk may hop to the nearest unvisited
    skeleton pixel within a 2-px window (Manhattan-distance, then
    lexicographic tie-break — deterministic); a genuinely finished course
    has no such pixel and the walk ends exactly as before."""
    def neighbors(p):
        return [(p[0] + dx, p[1] + dy) for dx, dy in _N8
                if (p[0] + dx, p[1] + dy) in skel]
    endpoints = sorted(p for p in skel if len(neighbors(p)) == 1)
    cur = endpoints[0] if endpoints else min(skel)
    chain, visited = [cur], {cur}
    while True:
        nxt = sorted(n for n in neighbors(cur) if n not in visited)
        if nxt:
            cur = nxt[0]
        else:
            hop = sorted(
                (abs(dx) + abs(dy), (cur[0] + dx, cur[1] + dy))
                for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)
                if (cur[0] + dx, cur[1] + dy) in skel
                and (cur[0] + dx, cur[1] + dy) not in visited)
            if not hop:
                return chain
            cur = hop[0][1]
        chain.append(cur)
        visited.add(cur)


def trace_image(image_path: str, world_size: float = 160.0) -> list[list[float]]:
    """Trace a drawn course in an image file into route points (meters,
    XZ ground plane, y=0). The drawing's larger extent maps to `world_size`
    meters; image y-down flips to world +Z-up. Raises ValueError when no
    line is found; ImportError when Pillow isn't installed."""
    from PIL import Image  # lazy: optional dependency

    img = Image.open(image_path).convert("L")
    if max(img.size) > _MAX_IMAGE_SIDE:
        scale = _MAX_IMAGE_SIDE / max(img.size)
        img = img.resize((max(1, round(img.size[0] * scale)),
                          max(1, round(img.size[1] * scale))),
                         Image.NEAREST)
    pixels = _threshold_pixels(img)
    if len(pixels) < 8:
        raise ValueError(f"no drawn line found in {image_path}")
    chain = _walk_skeleton(_thin(pixels))
    if len(chain) < 8:
        raise ValueError(f"traced line in {image_path} is too short for a route")

    xs = [p[0] for p in chain]
    ys = [p[1] for p in chain]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = world_size / span
    points = [[round((x - min(xs)) * scale, 3), 0.0,
               round((max(ys) - y) * scale, 3)] for x, y in chain]
    points, _closed = close_if_near(points)
    return simplify_to_cap(points)


# ── route file assembly ──────────────────────────────────────────────────────

def build_route(points: list[list[float]], width: float = 10.0,
                close: bool | None = None, source: str = "text") -> dict:
    """Canonical route dict for `--out routes.json` / `unity-create --route`:
    resampled to the 64-point cap, optionally closed (close=None =
    auto-detect near endpoints), then P1-normalized (start line at the
    origin, travel direction +Z)."""
    pts = [list(p) for p in points]
    if close is True and pts[0][:3] != pts[-1][:3]:
        pts.append(list(pts[0][:3]))
        closed = True
    elif close is False:
        closed = pts[0][:3] == pts[-1][:3]
    else:
        pts, closed = close_if_near(pts)
    pts = resample_polyline(pts)
    pts = normalize_route(pts)
    return {
        "path_points": pts,
        "path_width": float(width),
        "closed": closed,
        "source": source,
    }
