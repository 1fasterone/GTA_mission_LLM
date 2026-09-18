"""src/ident — the shared identity substrate (docs/SCENE_GRAPH_COMPILER.md, Phase 0).

The pinned hex values below were recorded from src/vstack/canon.py BEFORE it
moved to src/ident/canon.py. They are the proof that the promotion changed no
byte of any identity: if one of them moves, every hash derived through the
substrate moved with it, and every cache and golden keyed on those hashes is
silently invalidated. Change them only together with a deliberate, versioned
change to the construction.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.ident import HashRef, canon_bytes, chash, fhash, q
from src.ident import canon as canon_mod
from src.ident.tags import (
    ALL_TAGS, ASSET, ASSET_BIN, BUILD, FILE_TAGS, NODE, PAYLOAD_TAGS, VSTACK_TAGS,
    UnregisteredTagError,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── the move is byte-identical ────────────────────────────────────────────────

def test_chash_outputs_pinned_across_the_move():
    assert chash("build", {}) == "a47b42624e10afc311e533c8eacd90c7"
    assert chash("build", {"b": [1, 2, {"z": None, "a": True}], "a": "x"}) \
        == "59d8c59dba06712dce93816c61879920"
    assert chash("v1.asset", {"verts_q": q(1.0), "bounds_q": [q(0.5), q(1.8), q(0.5)]}) \
        == "03fecdcdd02c716752dfdcebec71d67e"
    assert chash("build", {"s": {3, 1, 2}}) == "c6331009e55c3495e8e9189a85e50bf1"
    assert chash("build", {"a": 1}, size=8) == "42fe9f4ed51e1448"


def test_canon_bytes_pinned():
    assert canon_bytes({"b": 1, "a": [True, None, "é"]}) \
        == '{"a":[true,null,"é"],"b":1}'.encode("utf-8")


def test_vstack_identity_chain_pinned_across_the_move():
    from src.vstack.e2e_0001 import golden_bundle
    from src.vstack.slice_v1_v5 import build_slice, run

    sl = build_slice()
    g = golden_bundle(sl, run(sl))
    assert g["build_hash"] == "bdffb23c384d95eb203b27e8c76dffee"
    assert g["worldstate_hash@N"] == "4b54181ba4bd01e47233810f7dd56de9"

    deg = build_slice(degrade={"V2": "synthetic_capsule"})
    assert golden_bundle(deg, run(deg))["build_hash"] == "e0662615a181651e5c22d2fceb7e59bc"


def test_vstack_no_longer_ships_its_own_canon():
    assert not (_REPO_ROOT / "src" / "vstack" / "canon.py").exists()


# ── canonicalization guarantees ───────────────────────────────────────────────

def test_raw_float_is_rejected():
    with pytest.raises(TypeError, match="quantize"):
        chash(BUILD, {"x": 1.5})


def test_dict_order_does_not_matter_but_list_order_does():
    assert chash(BUILD, {"a": 1, "b": 2}) == chash(BUILD, {"b": 2, "a": 1})
    assert chash(BUILD, [1, 2]) != chash(BUILD, [2, 1])


def test_unknown_type_is_rejected():
    with pytest.raises(TypeError, match="unhashable-in-canon"):
        chash(BUILD, {"p": Path("x")})


def test_same_payload_under_different_tags_differs():
    assert chash(ASSET, {"a": 1}) != chash(NODE, {"a": 1})


def test_hash_is_stable_across_processes_with_different_hash_seeds():
    """Sets and dicts of strings iterate in PYTHONHASHSEED-dependent order; the
    canonical form must erase that entirely."""
    code = ("from src.ident import chash;"
            "print(chash('node', {'tags': {'b', 'a', 'c', 'zz', 'y'}, "
            "'m': {'k2': 1, 'k1': {'x', 'w'}}}))")
    expected = chash(NODE, {"tags": {"b", "a", "c", "zz", "y"},
                            "m": {"k2": 1, "k1": {"x", "w"}}})
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT, env=env,
                             capture_output=True, text=True, check=True)
        assert out.stdout.strip() == expected


# ── the closed tag registry ───────────────────────────────────────────────────

def test_unregistered_tag_is_refused_loudly():
    with pytest.raises(UnregisteredTagError, match=r"\[ident.strict\].*src/ident/tags.py"):
        chash("entity", {"a": 1})


def test_file_tag_is_refused_by_chash():
    with pytest.raises(UnregisteredTagError, match="FILE tag"):
        chash(ASSET_BIN, {"a": 1})


def test_payload_tag_is_refused_by_fhash(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    with pytest.raises(UnregisteredTagError, match="not a FILE tag"):
        fhash(f, tag=ASSET)


def test_every_vstack_tag_the_kernel_uses_is_registered():
    """The kernel runs every chash() it has in build_slice()+run(); this is the
    guard that registering its tags verbatim was complete."""
    from src.vstack.slice_v1_v5 import build_slice, run
    run(build_slice(degrade={"V1": "placeholder_rig", "V2": "synthetic_capsule"}))
    assert VSTACK_TAGS <= ALL_TAGS


def test_registry_partitions():
    assert FILE_TAGS.isdisjoint(PAYLOAD_TAGS)
    assert FILE_TAGS | PAYLOAD_TAGS == ALL_TAGS


# ── fhash: file bytes exactly as found ────────────────────────────────────────

def test_fhash_is_path_independent(tmp_path):
    a = tmp_path / "one" / "model.fbx"
    b = tmp_path / "two" / "renamed.fbx"
    a.parent.mkdir()
    b.parent.mkdir()
    a.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00payload")
    b.write_bytes(a.read_bytes())
    assert fhash(a) == fhash(b)


def test_fhash_sees_one_flipped_byte(tmp_path):
    a = tmp_path / "a.fbx"
    b = tmp_path / "b.fbx"
    data = bytearray(b"\x00" * 4096)
    a.write_bytes(bytes(data))
    data[2048] ^= 0x01
    b.write_bytes(bytes(data))
    assert fhash(a) != fhash(b)


def test_fhash_never_normalizes_line_endings(tmp_path):
    lf = tmp_path / "lf.txt"
    crlf = tmp_path / "crlf.txt"
    lf.write_bytes(b"a\nb\n")
    crlf.write_bytes(b"a\r\nb\r\n")
    assert fhash(lf) != fhash(crlf)


def test_fhash_streaming_matches_across_chunk_boundaries(tmp_path, monkeypatch):
    f = tmp_path / "big.bin"
    f.write_bytes(bytes(range(256)) * 40)
    whole = fhash(f)
    monkeypatch.setattr(canon_mod, "_FILE_CHUNK", 7)
    assert fhash(f) == whole


def test_file_and_payload_domains_never_collide(tmp_path):
    """A file whose bytes ARE a canonical payload still hashes apart from it."""
    payload = {"a": 1}
    f = tmp_path / "p.json"
    f.write_bytes(canon_bytes(payload))
    assert fhash(f) != chash(ASSET, payload)


# ── HashRef ───────────────────────────────────────────────────────────────────

def test_hashref_of_matches_chash():
    ref = HashRef.of(NODE, {"a": 1})
    assert ref.hex == chash(NODE, {"a": 1})
    assert str(ref) == f"node:{ref.hex}"
    assert ref.short == ref.hex[:8]


def test_hashref_of_file(tmp_path):
    f = tmp_path / "m.glb"
    f.write_bytes(b"glTF")
    ref = HashRef.of_file(f)
    assert ref.tag == ASSET_BIN
    assert ref.hex == fhash(f)


def test_hashref_is_a_merkle_edge_inside_payloads():
    parent = HashRef.of(NODE, {"name": "root"})
    child = chash(NODE, {"name": "car", "parent": parent})
    assert child == chash(NODE, {"name": "car", "parent": str(parent)})
    other_parent = HashRef.of(NODE, {"name": "other"})
    assert child != chash(NODE, {"name": "car", "parent": other_parent})


def test_hashref_validates():
    with pytest.raises(UnregisteredTagError):
        HashRef("entity", "ab")
    for bad in ("ABCD", "abc", "", "zz"):
        with pytest.raises(ValueError):
            HashRef(NODE, bad)


def test_hashref_is_hashable_and_ordered():
    a, b = HashRef(NODE, "00"), HashRef(NODE, "ff")
    assert sorted([b, a]) == [a, b]
    assert len({a, HashRef(NODE, "00")}) == 1


# ── Unity GUIDs are a contract, pinned at scheme 1 ────────────────────────────

def test_unity_guid_scheme_one_is_pinned():
    """_make_guid/_make_uuid stay md5 on purpose (src/ident/frozen.py): every
    generated project already carries these GUIDs. A change here means bumping
    GUID_SCHEME and accepting that regenerated projects re-key every .meta."""
    from src.unity_generator import GUID_SCHEME, _make_guid, _make_uuid

    assert GUID_SCHEME == 1
    assert _make_guid("GameManager") == "11085e703dfd10d0160557608cc47a16"
    assert _make_guid("Assets/Scripts/PlayerController.cs") \
        == "f41a9748fc7daee482568757de62c61a"
    assert _make_uuid("DemoProject", "Main") == "d2c396ed-0ba3-4764-a9c5-bc57a3a40cf6"
