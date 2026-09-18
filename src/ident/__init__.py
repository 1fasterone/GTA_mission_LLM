"""
ident — the one identity substrate every backend derives hashes through.

    chash(tag, payload)   domain-separated hash of a canonical, float-free structure
    fhash(path)           domain-separated hash of a file's exact bytes
    HashRef               a digest that remembers its domain (Merkle edges, logs)
    q(value)              quantize a real to an integer count before hashing it

Tags are a closed set (src/ident/tags.py). Digests that must stay outside this
package are inventoried, with reasons, in src/ident/frozen.py and held to the
tree by src/ident/lint.py. Plan: docs/SCENE_GRAPH_COMPILER.md, Phase 0.
"""
from src.ident.canon import (
    DIGEST_SIZE, Q_POS, Q_ROT, Q_TIME, HashRef, canon_bytes, chash, fhash, q,
)

__all__ = [
    "DIGEST_SIZE", "Q_POS", "Q_ROT", "Q_TIME",
    "HashRef", "canon_bytes", "chash", "fhash", "q",
]
