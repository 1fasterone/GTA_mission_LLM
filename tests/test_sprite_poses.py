"""Pose-conditioned sprite generation.

Regression for the 2026-11-10 cuphead_style_brawler run: every frame was
img2img'd off one reference at 0.72 denoise and the seed repeated per frame
index across actions, so all 24 player frames (8 actions) and all 15 enemy
frames showed the reference's stance. Poses now come from deterministic
OpenPose skeletons fed to a ControlNet.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image

from src.micro.sprite_animator import _ACTIONS_BY_ROLE
from src.sprites import content
from src.sprites.comfyui_backend import (
    ComfyUIBackend, _build_posed_workflow,
)
from src.sprites.content import SpriteCue, generate_sprite_bank
from src.sprites.poses import ACTION_KEYFRAMES, keypoints, params_for, render_pose_png


# ── skeleton data ────────────────────────────────────────────────────────────

def test_every_animator_action_has_a_skeleton():
    for actions in _ACTIONS_BY_ROLE.values():
        for action in actions:
            assert action in ACTION_KEYFRAMES, action


@pytest.mark.parametrize("frames", [1, 2, 3, 4, 6, 8])
def test_every_frame_stays_on_canvas(frames):
    for action in ACTION_KEYFRAMES:
        for i in range(frames):
            pts = [p for p in keypoints(params_for(action, i, frames)) if p]
            assert len(pts) == 17
            for x, y in pts:
                assert 0.02 <= x <= 0.98 and 0.02 <= y <= 0.98, (action, i, frames, x, y)


# Live 2026-11-11: idle's keyframes moved joints ~0.011 of the canvas and all
# three idle frames rendered as one stance. 0.04 (~20 px at 512) is the
# smallest step every other action already clears.
MIN_FRAME_MOTION = 0.04


@pytest.mark.parametrize("frames", [2, 3])
def test_every_action_moves_visibly_between_frames(frames):
    """At the schema default (frames_per_action=3) and below. More frames
    interpolate the same keyframe span in smaller steps, by design."""
    import math

    for action in ACTION_KEYFRAMES:
        pts = [keypoints(params_for(action, i, frames)) for i in range(frames)]
        for i in range(frames - 1):
            step = max(math.dist(p, q) for p, q in zip(pts[i], pts[i + 1]) if p and q)
            assert step >= MIN_FRAME_MOTION, (action, frames, i, round(step, 3))


def test_pose_png_is_deterministic_and_sized():
    a = render_pose_png("run", 1, 3, 512, 512)
    assert a == render_pose_png("run", 1, 3, 512, 512)
    img = Image.open(io.BytesIO(a))
    assert img.size == (512, 512) and img.mode == "RGB"
    assert img.getpixel((0, 0)) == (0, 0, 0)        # OpenPose maps are black-backed


def test_actions_render_distinct_skeletons():
    """The bug was one stance for every action; the control maps must differ."""
    digests = {
        hashlib.sha1(render_pose_png(a, 0, 3, 256, 256)).hexdigest()
        for a in ACTION_KEYFRAMES
    }
    assert len(digests) == len(ACTION_KEYFRAMES)


def test_frames_within_an_action_differ():
    for action in ACTION_KEYFRAMES:
        frames = {render_pose_png(action, i, 3, 128, 128) for i in range(3)}
        assert len(frames) == 3, action


# ── ComfyUI graph ────────────────────────────────────────────────────────────

def _graph(**kw):
    base = dict(checkpoint="ck.safetensors", prompt="p", negative="n", seed=7,
                width=512, height=512, steps=16, cfg=6.5,
                controlnet="SD1.5/control_v11p_sd15_openpose.pth",
                pose_filename="pose.png", strength=1.0)
    base.update(kw)
    return _build_posed_workflow(**base)


def test_posed_graph_routes_conditioning_through_the_controlnet():
    g = _graph()
    apply = next(n for n in g.values() if n["class_type"] == "ControlNetApplyAdvanced")
    loader = next(n for n in g.values() if n["class_type"] == "ControlNetLoader")
    assert loader["inputs"]["control_net_name"] == "SD1.5/control_v11p_sd15_openpose.pth"
    ks = g["3"]["inputs"]
    apply_id = next(k for k, n in g.items() if n["class_type"] == "ControlNetApplyAdvanced")
    assert ks["positive"] == [apply_id, 0] and ks["negative"] == [apply_id, 1]
    assert apply["inputs"]["strength"] == 1.0
    assert g["5"]["class_type"] == "EmptyLatentImage" and ks["denoise"] == 1.0


def test_posed_graph_with_reference_starts_from_the_reference_latent():
    g = _graph(ref_filename="ref.png", denoise=0.9)
    ks = g["3"]["inputs"]
    assert ks["denoise"] == 0.9
    src = g[ks["latent_image"][0]]
    assert src["class_type"] == "VAEEncode"
    assert g[src["inputs"]["pixels"][0]]["inputs"]["image"] == "ref.png"


def test_pose_support_requires_the_controlnet_on_the_server(monkeypatch):
    be = ComfyUIBackend({"sprites": {"pose_controlnet": "SD1.5/control_v11p_sd15_openpose.pth"}})
    monkeypatch.setattr(be, "available", lambda: True)
    monkeypatch.setattr(be, "_get_json", lambda _path: {"ControlNetLoader": {"input": {
        "required": {"control_net_name": [["SD1.5/control_v11p_sd15_openpose.pth"]]}}}})
    assert be.supports_pose() is True

    be2 = ComfyUIBackend({"sprites": {"pose_controlnet": "missing.pth"}})
    monkeypatch.setattr(be2, "available", lambda: True)
    monkeypatch.setattr(be2, "_get_json", lambda _path: {"ControlNetLoader": {"input": {
        "required": {"control_net_name": [["other.pth"]]}}}})
    assert be2.supports_pose() is False


# ── orchestration ────────────────────────────────────────────────────────────

class _PosedBackend:
    """Records what generate_sprite_bank asks for."""

    name = "fake_posed"

    def __init__(self, pose_ok: bool = True):
        self.pose_ok = pose_ok
        self.calls: list[dict] = []

    def supports_pose(self) -> bool:
        return self.pose_ok

    def generate_posed(self, prompt: str, *, seed: int, pose_png_bytes: bytes,
                       width: int = 512, height: int = 512,
                       reference_png_bytes: Optional[bytes] = None) -> Optional[bytes]:
        self.calls.append({"mode": "posed", "prompt": prompt, "seed": seed,
                           "pose": hashlib.sha1(pose_png_bytes).hexdigest(),
                           "ref": reference_png_bytes is not None})
        return _png(len(self.calls))

    def generate(self, prompt: str, *, seed: int, width: int = 512,
                 height: int = 512) -> Optional[bytes]:
        self.calls.append({"mode": "txt2img", "seed": seed})
        return _png(len(self.calls))

    def generate_from_reference(self, prompt: str, *, seed: int,
                                reference_png_bytes: bytes) -> Optional[bytes]:
        self.calls.append({"mode": "img2img", "seed": seed})
        return _png(len(self.calls))

    def free_vram(self) -> bool:
        return True


def _png(n: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (n % 255, 10, 10, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _cues(actions, frames=3):
    return [SpriteCue(entity_id="Player", action=a, frame_index=i, prompt=f"hero, {a}")
            for a in actions for i in range(frames)]


@pytest.fixture
def patch_backend(monkeypatch):
    def _install(backend):
        monkeypatch.setattr(content, "_make_backend", lambda _cfg: backend)
        monkeypatch.setattr(content, "_NATIVELY_TRANSPARENT_BACKENDS",
                            frozenset({backend.name}))
        return backend
    return _install


def test_posed_bank_gives_every_frame_its_own_skeleton(tmp_path, patch_backend):
    be = patch_backend(_PosedBackend())
    actions = ["idle", "run", "punch_light", "punch_heavy"]
    manifest = generate_sprite_bank(_cues(actions), tmp_path, {})
    assert manifest["pose_conditioned"] is True
    assert all(c["mode"] == "posed" for c in be.calls)
    assert len({c["pose"] for c in be.calls}) == len(actions) * 3
    # One seed for the whole entity: identity rides on seed + reference,
    # pose rides on the skeleton (seed + frame_index made frame N of every
    # action share a look).
    assert len({c["seed"] for c in be.calls}) == 1
    assert be.calls[0]["ref"] is False and all(c["ref"] for c in be.calls[1:])


def test_bank_without_controlnet_falls_back_loudly(tmp_path, patch_backend):
    be = patch_backend(_PosedBackend(pose_ok=False))
    manifest = generate_sprite_bank(_cues(["idle", "run"]), tmp_path, {})
    assert manifest["pose_conditioned"] is False
    assert {c["mode"] for c in be.calls} == {"txt2img", "img2img"}
    assert any("pose ControlNet unavailable" in w for w in manifest["warnings"])


def test_manifest_from_a_second_animator_merges_instead_of_overwriting(tmp_path):
    from src.micro.sprite_animator import merge_sprite_manifest

    path = Path(tmp_path) / "sprite_manifest.json"
    first = {"seed": 0, "backend": "comfyui", "frame_count": 24, "generated": 24,
             "placeholders": 0, "pose_conditioned": True,
             "entities": {"Player": {"idle": 3}}, "warnings": ["a"]}
    second = {"seed": 0, "backend": "comfyui", "frame_count": 15, "generated": 12,
              "placeholders": 3, "pose_conditioned": True,
              "entities": {"Enemy": {"attack": 3}}, "warnings": ["b"]}
    path.write_text(json.dumps(first), encoding="utf-8")
    merged = merge_sprite_manifest(path, second)
    assert merged["entities"] == {"Player": {"idle": 3}, "Enemy": {"attack": 3}}
    assert merged["frame_count"] == 39 and merged["generated"] == 36
    assert merged["placeholders"] == 3 and merged["warnings"] == ["a", "b"]


def test_stale_sprite_resources_from_a_crashed_run_are_cleared(tmp_path):
    """Run 1 died at the syntax gate before the overlay step removed
    Micros/sprite_animator/Resources, so its EnemyGroup placeholders were
    still there in run 2 and the manifest merge would have counted them."""
    from src.micro.sprite_animator import clear_stale_sprite_resources

    assets = Path(tmp_path) / "Assets"
    stale = assets / "Micros" / "sprite_animator" / "Resources"
    (stale / "Sprites" / "EnemyGroup" / "idle").mkdir(parents=True)
    (stale / "sprite_manifest.json").write_text("{}", encoding="utf-8")
    keep = assets / "Micros" / "sprite_animator" / "Scripts"
    keep.mkdir(parents=True)
    assert clear_stale_sprite_resources(assets) is True
    assert not stale.exists() and keep.exists()
    assert clear_stale_sprite_resources(assets) is False
