"""A4 (docs/SCENE_GRAPH_COMPILER.md): the two digests that hashed an ABSOLUTE
path now derive from content via src/ident.

  * unity_sceneinput._cache_key — the GLB->FBX conversion cache used to key
    on (absolute path, size, mtime): a miss on every other machine or
    worktree, a miss after any checkout touched the mtime, and blind to a
    .gltf's external buffers changing.
  * unity_entity_asset_resolver._register_authored — the tag that
    disambiguates two authored models sharing a stem lands in an EMITTED
    Resources file name, and used to be a hash of the absolute path, so the
    same layout produced different output on different machines.

Two directories under tmp_path stand in for two machines / checkouts.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from src.ident import fhash
from src.unity_entity_asset_resolver import _AUTHORED_RES_ROOT, _register_authored
from src.unity_sceneinput import SceneInputError, _cache_key, convert_gltf_groups

_GLB = b"glTF\x02\x00\x00\x00" + bytes(range(64))


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _gltf(dirpath: Path, buffer_bytes: bytes, *, name: str = "kart") -> Path:
    _write(dirpath / f"{name}.bin", buffer_bytes)
    _write(dirpath / "tex a.png", b"\x89PNG fake")   # referenced percent-encoded below
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"uri": f"{name}.bin", "byteLength": len(buffer_bytes)},
                    {"uri": "data:application/octet-stream;base64,AAAA", "byteLength": 3}],
        "images": [{"uri": "tex%20a.png"}, {"bufferView": 0, "mimeType": "image/png"}],
    }
    return _write(dirpath / f"{name}.gltf", json.dumps(doc).encode("utf-8"))


# ── GLB -> FBX conversion cache key ───────────────────────────────────────────

def test_glb_key_is_the_same_on_every_machine(tmp_path):
    a = _write(tmp_path / "machine_a" / "models" / "kart.glb", _GLB)
    b = _write(tmp_path / "machine_b" / "elsewhere" / "kart.glb", _GLB)
    assert _cache_key(a) == _cache_key(b) == fhash(a)


def test_glb_key_ignores_mtime(tmp_path):
    a = _write(tmp_path / "kart.glb", _GLB)
    before = _cache_key(a)
    os.utime(a, (1_000_000, 1_000_000))
    assert _cache_key(a) == before


def test_glb_key_sees_a_content_change(tmp_path):
    a = _write(tmp_path / "kart.glb", _GLB)
    before = _cache_key(a)
    a.write_bytes(_GLB[:-1] + b"\xff")
    assert _cache_key(a) != before


def test_gltf_key_covers_its_external_buffers(tmp_path):
    g1 = _gltf(tmp_path / "one", b"vertices-v1")
    g2 = _gltf(tmp_path / "two", b"vertices-v1")
    assert _cache_key(g1) == _cache_key(g2)           # path-independent
    (tmp_path / "two" / "kart.bin").write_bytes(b"vertices-v2")
    assert _cache_key(g1) != _cache_key(g2)           # the .gltf is unchanged


def test_gltf_key_covers_percent_encoded_images(tmp_path):
    g = _gltf(tmp_path, b"v")
    before = _cache_key(g)
    (tmp_path / "tex a.png").write_bytes(b"\x89PNG edited")
    assert _cache_key(g) != before


def test_gltf_missing_external_buffer_cannot_be_keyed(tmp_path):
    g = _gltf(tmp_path, b"v")
    (tmp_path / "kart.bin").unlink()
    with pytest.raises(OSError):
        _cache_key(g)


def _fake_converter(calls: list):
    def convert(src, fbx_out, textures_dir, *, blender_cfg=None, category="prop"):
        calls.append(Path(src))
        Path(fbx_out).parent.mkdir(parents=True, exist_ok=True)
        Path(fbx_out).write_bytes(b"Kaydara FBX Binary  \x00" + Path(src).read_bytes())
        return Path(fbx_out)
    return convert


def test_second_checkout_hits_the_conversion_cache(tmp_path, monkeypatch):
    import src.blender.sketchfab_convert as conv
    calls: list = []
    monkeypatch.setattr(conv, "convert_gltf_to_fbx", _fake_converter(calls))
    cache = tmp_path / "cache"

    a = _write(tmp_path / "checkout_a" / "models" / "kart.glb", _GLB)
    scene_a = {"groups": [{"src": str(a), "file": a.name}]}
    assert convert_gltf_groups(scene_a, cache) == ["scene: kart.glb -> FBX (headless Blender)"]

    b = tmp_path / "checkout_b" / "models" / "kart.glb"
    b.parent.mkdir(parents=True)
    shutil.copyfile(a, b)
    os.utime(b, (2_000_000, 2_000_000))
    scene_b = {"groups": [{"src": str(b), "file": b.name}]}
    assert convert_gltf_groups(scene_b, cache) == ["scene: kart.glb -> cached FBX"]
    assert calls == [a]
    assert scene_a["groups"][0]["src"] == scene_b["groups"][0]["src"]


def test_unreadable_source_fails_loudly_with_a_remedy(tmp_path, monkeypatch):
    import src.blender.sketchfab_convert as conv
    calls: list = []
    monkeypatch.setattr(conv, "convert_gltf_to_fbx", _fake_converter(calls))
    g = _gltf(tmp_path / "src", b"v")
    (tmp_path / "src" / "kart.bin").unlink()
    scene = {"groups": [{"src": str(g), "file": g.name}]}
    with pytest.raises(SceneInputError, match=r"could not be read(.|\n)*remedy"):
        convert_gltf_groups(scene, tmp_path / "cache")
    assert calls == []


# ── authored-model disambiguation tag (an EMITTED file name) ──────────────────

def _register_pair(root: Path, second: bytes) -> tuple[str | None, str | None]:
    """Two authored groups named car.fbx from different vendor folders."""
    first = _write(root / "vendor_x" / "car.fbx", b"car-model-x")
    other = _write(root / "vendor_y" / "car.fbx", second)
    used: dict = {}
    return (_register_authored({"src": str(first)}, used, is_2d=False),
            _register_authored({"src": str(other)}, used, is_2d=False))


def test_colliding_stems_get_the_same_emitted_names_on_every_machine(tmp_path):
    on_a = _register_pair(tmp_path / "machine_a", b"car-model-y")
    on_b = _register_pair(tmp_path / "deep" / "machine_b", b"car-model-y")
    assert on_a == on_b
    tag = fhash(tmp_path / "machine_a" / "vendor_y" / "car.fbx")[:8]
    assert on_a == (f"{_AUTHORED_RES_ROOT}/Authored/car", f"{_AUTHORED_RES_ROOT}/Authored/car-{tag}")


def test_identical_bytes_at_a_second_path_are_one_asset(tmp_path):
    first, second = _register_pair(tmp_path, b"car-model-x")
    assert first == second == f"{_AUTHORED_RES_ROOT}/Authored/car"
