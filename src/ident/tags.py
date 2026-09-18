"""
The closed set of hash domain tags — the only strings `chash()` / `fhash()`
accept as a domain separator.

A tag is what keeps two structurally identical payloads in different roles from
sharing an identity: an entity node and an asset whose canonical payloads
happen to match still hash apart. That only holds if every role owns exactly
one tag and no caller improvises one, so the set is CLOSED, like the atom
vocabulary: an unregistered tag is a loud `[ident.strict]` refusal, never a
silent new domain. A new tag lands here in the same change as its first caller
(docs/SCENE_GRAPH_COMPILER.md lists which slice adds which).

Two kinds of tag, and they never mix:
  * PAYLOAD tags — hash a canonical, float-free structure via `chash()`;
  * FILE tags    — hash a file's exact bytes via `fhash()`. Keeping them apart
    means no canonical payload can ever collide with a file that happens to
    contain the same JSON bytes.
"""
from __future__ import annotations

# ── Compiler identity domains (docs/SCENE_GRAPH_COMPILER.md) ──────────────────
ASSET = "asset"                  # an asset's full identity (Track A1)
ASSET_BIN = "asset.bin"          # a file's exact bytes — FILE tag, fhash() only
ASSET_RECIPE = "asset.recipe"    # how an asset was produced (Track A2)
NODE = "node"                    # a scene-graph node (Track B3)
SCENE = "scene"                  # a whole scene graph (Track B6)
LAYOUT = "layout"                # a solved layout (Track C)
WIRING = "wiring"                # a micro-to-micro binding (Track B5)
BUILD = "build"                  # the root of a build's identity chain

COMPILER_TAGS: frozenset[str] = frozenset({
    ASSET, ASSET_BIN, ASSET_RECIPE, NODE, SCENE, LAYOUT, WIRING, BUILD,
})

# ── vstack reference kernel (src/vstack/) — its V1-V5 spec identity chain ─────
# Registered verbatim so the kernel's hashes stay byte-identical. "build" is
# shared with the compiler on purpose: in both it is the same role, the root of
# a Merkle chain over everything beneath it.
VSTACK_TAGS: frozenset[str] = frozenset({
    "v1.asset", "v1.rig", "v2.deform", "retarget.anim",
    "v3.behavior", "v3.entity", "v3.mission", "v3.scene",
    "v4.ai", "v4.gameplay", "v4.nav", "v4.physics",
    "v5.sim", "v5.worldstate_seq", "v5.worldstate_value",
    BUILD,
})

FILE_TAGS: frozenset[str] = frozenset({ASSET_BIN})
ALL_TAGS: frozenset[str] = COMPILER_TAGS | VSTACK_TAGS
PAYLOAD_TAGS: frozenset[str] = ALL_TAGS - FILE_TAGS


class UnregisteredTagError(ValueError):
    """A hash was requested under a domain tag this registry doesn't own."""


def require_payload_tag(tag: str) -> str:
    if tag in PAYLOAD_TAGS:
        return tag
    if tag in FILE_TAGS:
        raise UnregisteredTagError(
            f"[ident.strict] {tag!r} is a FILE tag — it hashes a file's exact "
            "bytes and is reserved for fhash(); use a payload tag with chash()")
    raise UnregisteredTagError(
        f"[ident.strict] unregistered hash domain tag {tag!r} — register it in "
        "src/ident/tags.py in the same change as its first caller")


def require_file_tag(tag: str) -> str:
    if tag in FILE_TAGS:
        return tag
    raise UnregisteredTagError(
        f"[ident.strict] {tag!r} is not a FILE tag — fhash() hashes raw file "
        f"bytes and accepts only {sorted(FILE_TAGS)}; hash a structure with "
        "chash() instead")
