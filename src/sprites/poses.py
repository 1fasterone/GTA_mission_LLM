"""Deterministic OpenPose skeletons per sprite action.

Why this exists: chaining every frame off one reference image (img2img at
0.72 denoise) kept the reference's silhouette, so all 24 player frames and all
15 enemy frames of the 2026-11-10 cuphead_style_brawler run showed the same
stance whatever the pose text said. A ControlNet OpenPose input fixes the pose
independently of identity: the reference image (or shared seed) carries the
character, the skeleton carries the pose.

The skeletons are DATA, not model output. Each action is two keyframes of
joint angles; frame i of n interpolates between them. Rendered as a standard
OpenPose COCO-18 map (black background, the canonical limb/joint colors) so a
stock `control_v11p_sd15_openpose` model reads it without any preprocessor
node. Deterministic: same (action, frame_index, frames, size) -> same PNG
bytes, so Step 6 stays reproducible.

Conventions (character faces +x, image y grows downward):
  * coordinates are normalized to the canvas (0..1)
  * limb angles are degrees from straight DOWN; positive swings FORWARD (+x)
  * elbow bend adds to the upper-arm angle (forearm swings up/forward)
  * knee bend subtracts from the thigh angle (shin swings back)
  * `lean` tilts the torso forward (+) or back (-) about the hips
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, fields
from typing import Optional

# ── segment lengths (fraction of canvas height) ──────────────────────────────
_TORSO = 0.30
_UPPER_ARM = 0.13
_FOREARM = 0.12
_THIGH = 0.18
_SHIN = 0.18
_HEAD = 0.08
_SHOULDER_DX = 0.05
_HIP_DX = 0.035
_ROOT = (0.50, 0.52)          # hip centre of the neutral stance

# COCO-18 joint order used by OpenPose.
NOSE, NECK, R_SHO, R_ELB, R_WRI, L_SHO, L_ELB, L_WRI = range(8)
R_HIP, R_KNEE, R_ANK, L_HIP, L_KNEE, L_ANK, R_EYE, L_EYE, R_EAR, L_EAR = range(8, 18)

# OpenPose's canonical limb pairs and colors (what the ControlNet was trained on).
_LIMBS = [
    (NECK, R_SHO), (NECK, L_SHO), (R_SHO, R_ELB), (R_ELB, R_WRI), (L_SHO, L_ELB),
    (L_ELB, L_WRI), (NECK, R_HIP), (R_HIP, R_KNEE), (R_KNEE, R_ANK), (NECK, L_HIP),
    (L_HIP, L_KNEE), (L_KNEE, L_ANK), (NECK, NOSE), (NOSE, R_EYE), (R_EYE, R_EAR),
    (NOSE, L_EYE), (L_EYE, L_EAR),
]
_COLORS = [
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
    (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
    (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
    (255, 0, 255), (255, 0, 170), (255, 0, 85),
]


@dataclass(frozen=True)
class PoseParams:
    """One keyframe. Front = the limb nearer the camera (OpenPose 'left')."""

    lean: float = 0.0
    dx: float = 0.0
    dy: float = 0.0
    front_sho: float = -10.0
    front_elb: float = 20.0
    back_sho: float = 10.0
    back_elb: float = 20.0
    front_hip: float = 5.0
    front_knee: float = 0.0
    back_hip: float = -5.0
    back_knee: float = 0.0


_N = PoseParams()

# Two keyframes per action. Names match sprite_animator._ACTIONS_BY_ROLE.
ACTION_KEYFRAMES: dict[str, tuple[PoseParams, PoseParams]] = {
    "idle": (
        # Rubber-hose breathing bounce. The first version moved joints ~0.011
        # of the canvas (~6 px at 512) — below what the ControlNet resolves,
        # so all idle frames rendered as one stance (seen live 2026-11-11).
        # MIN_FRAME_MOTION in the tests keeps every action above 0.04.
        PoseParams(front_sho=-20, front_elb=25, back_sho=15, back_elb=25,
                   front_hip=8, front_knee=0, back_hip=-8, back_knee=0),
        PoseParams(lean=6, dy=0.045, front_sho=10, front_elb=60, back_sho=-15,
                   back_elb=55, front_hip=28, front_knee=45, back_hip=12, back_knee=40),
    ),
    "run": (
        PoseParams(lean=12, front_sho=-50, front_elb=80, back_sho=50, back_elb=80,
                   front_hip=40, front_knee=60, back_hip=-30, back_knee=30),
        PoseParams(lean=12, dy=-0.02, front_sho=50, front_elb=80, back_sho=-50,
                   back_elb=80, front_hip=-30, front_knee=30, back_hip=40, back_knee=60),
    ),
    "jump": (
        # Airborne reads from tucked legs + raised arms; the engine moves the
        # sprite vertically, so the skeleton stays inside the canvas.
        PoseParams(lean=5, dy=0.02, front_sho=125, front_elb=20, back_sho=110,
                   back_elb=25, front_hip=55, front_knee=95, back_hip=35, back_knee=105),
        PoseParams(lean=5, front_sho=100, front_elb=30, back_sho=85,
                   back_elb=35, front_hip=25, front_knee=45, back_hip=8, back_knee=55),
    ),
    "punch_light": (
        PoseParams(lean=5, front_sho=40, front_elb=100, back_sho=30, back_elb=110,
                   front_hip=20, front_knee=10, back_hip=-20, back_knee=5),
        PoseParams(lean=12, front_sho=90, front_elb=0, back_sho=30, back_elb=110,
                   front_hip=22, front_knee=12, back_hip=-22, back_knee=5),
    ),
    "punch_heavy": (
        PoseParams(lean=-10, front_sho=40, front_elb=90, back_sho=-60, back_elb=90,
                   front_hip=25, front_knee=15, back_hip=-25, back_knee=5),
        PoseParams(lean=20, dx=0.03, front_sho=10, front_elb=100, back_sho=95,
                   back_elb=0, front_hip=35, front_knee=20, back_hip=-35, back_knee=5),
    ),
    "throw": (
        PoseParams(lean=-12, dy=0.05, front_sho=70, front_elb=20, back_sho=200, back_elb=40,
                   front_hip=25, front_knee=10, back_hip=-25, back_knee=5),
        PoseParams(lean=18, dx=0.02, dy=0.05, front_sho=20, front_elb=40, back_sho=100,
                   back_elb=0, front_hip=30, front_knee=20, back_hip=-30, back_knee=5),
    ),
    "hurt": (
        PoseParams(lean=-20, dx=-0.03, front_sho=70, front_elb=40, back_sho=120,
                   back_elb=30, front_hip=10, front_knee=20, back_hip=-15, back_knee=10),
        PoseParams(lean=-28, dx=-0.06, front_sho=90, front_elb=50, back_sho=140,
                   back_elb=20, front_hip=15, front_knee=30, back_hip=-20, back_knee=15),
    ),
    "death": (
        PoseParams(lean=-40, front_sho=130, front_elb=0, back_sho=150,
                   back_elb=0, front_hip=30, front_knee=40, back_hip=-10, back_knee=60),
        PoseParams(lean=-85, dy=0.17, front_sho=170, front_elb=0, back_sho=175,
                   back_elb=0, front_hip=85, front_knee=0, back_hip=80, back_knee=0),
    ),
    "attack": (
        PoseParams(lean=15, front_sho=80, front_elb=30, back_sho=60, back_elb=50,
                   front_hip=45, front_knee=30, back_hip=-35, back_knee=10),
        PoseParams(lean=25, dx=0.04, front_sho=95, front_elb=0, back_sho=85,
                   back_elb=10, front_hip=50, front_knee=35, back_hip=-40, back_knee=10),
    ),
}


def has_pose(action: str) -> bool:
    return action in ACTION_KEYFRAMES


def _lerp(a: PoseParams, b: PoseParams, t: float) -> PoseParams:
    return PoseParams(**{
        f.name: getattr(a, f.name) + (getattr(b, f.name) - getattr(a, f.name)) * t
        for f in fields(PoseParams)
    })


def params_for(action: str, frame_index: int, frames: int) -> PoseParams:
    """Keyframe A at frame 0, B at the last frame, linear in between."""
    a, b = ACTION_KEYFRAMES[action]
    t = 0.0 if frames <= 1 else min(1.0, max(0.0, frame_index / (frames - 1)))
    return _lerp(a, b, t)


def _swing(origin: tuple[float, float], length: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    return origin[0] + length * math.sin(r), origin[1] + length * math.cos(r)


def keypoints(p: PoseParams) -> list[Optional[tuple[float, float]]]:
    """COCO-18 normalized keypoints for one pose (None = not drawn)."""
    hip = (_ROOT[0] + p.dx, _ROOT[1] + p.dy)
    lean = math.radians(p.lean)
    up = (math.sin(lean), -math.cos(lean))            # torso direction
    side = (math.cos(lean), math.sin(lean))           # forward, perpendicular to torso
    neck = (hip[0] + _TORSO * up[0], hip[1] + _TORSO * up[1])

    def off(base: tuple[float, float], fwd: float, along: float) -> tuple[float, float]:
        return (base[0] + fwd * side[0] + along * up[0],
                base[1] + fwd * side[1] + along * up[1])

    f_sho = off(neck, _SHOULDER_DX, -0.01)
    b_sho = off(neck, -_SHOULDER_DX, -0.01)
    f_hip = off(hip, _HIP_DX, 0.0)
    b_hip = off(hip, -_HIP_DX, 0.0)
    f_elb = _swing(f_sho, _UPPER_ARM, p.front_sho)
    f_wri = _swing(f_elb, _FOREARM, p.front_sho + p.front_elb)
    b_elb = _swing(b_sho, _UPPER_ARM, p.back_sho)
    b_wri = _swing(b_elb, _FOREARM, p.back_sho + p.back_elb)
    f_knee = _swing(f_hip, _THIGH, p.front_hip)
    f_ank = _swing(f_knee, _SHIN, p.front_hip - p.front_knee)
    b_knee = _swing(b_hip, _THIGH, p.back_hip)
    b_ank = _swing(b_knee, _SHIN, p.back_hip - p.back_knee)
    head = off(neck, 0.0, _HEAD)
    nose = off(head, 0.035, 0.0)
    eye_f = off(head, 0.035, 0.012)
    eye_b = off(head, 0.018, 0.014)
    ear_b = off(head, -0.02, 0.0)

    pts: list[Optional[tuple[float, float]]] = [None] * 18
    pts[NOSE], pts[NECK] = nose, neck
    pts[R_SHO], pts[R_ELB], pts[R_WRI] = b_sho, b_elb, b_wri
    pts[L_SHO], pts[L_ELB], pts[L_WRI] = f_sho, f_elb, f_wri
    pts[R_HIP], pts[R_KNEE], pts[R_ANK] = b_hip, b_knee, b_ank
    pts[L_HIP], pts[L_KNEE], pts[L_ANK] = f_hip, f_knee, f_ank
    pts[R_EYE], pts[L_EYE], pts[R_EAR] = eye_b, eye_f, ear_b
    return pts                                        # L_EAR hidden in side view


def render_pose_png(action: str, frame_index: int, frames: int,
                    width: int = 512, height: int = 512) -> bytes:
    """OpenPose control map for one frame as PNG bytes. Deterministic."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    pts = keypoints(params_for(action, frame_index, frames))
    px = [None if p is None else (p[0] * width, p[1] * height) for p in pts]
    stick = max(2, round(min(width, height) / 64))
    for i, (a, b) in enumerate(_LIMBS):
        pa, pb = px[a], px[b]
        if pa is None or pb is None:
            continue
        r, g, bl = _COLORS[i]
        draw.line([pa, pb], fill=(int(r * 0.6), int(g * 0.6), int(bl * 0.6)),
                  width=stick)
    rad = max(2, round(min(width, height) / 128))
    for i, p in enumerate(px):
        if p is None:
            continue
        draw.ellipse([p[0] - rad, p[1] - rad, p[0] + rad, p[1] + rad], fill=_COLORS[i])
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()
