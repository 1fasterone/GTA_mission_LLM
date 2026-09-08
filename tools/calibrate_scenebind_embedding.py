"""
Calibrate scenebind.embedding_weight (src/unity_scenebind.py's bind_score
embedding blend, docs/COMPILER_AUDIT.md Gap E / solution #7) against real
group/entity text, rather than shipping the guessed 0.5 placeholder in
config.example.yaml unverified.

Unlike tools/calibrate_catalog_embedding.py, this is not primarily about
"does the signal point the right way" — bind_score has NO hard pre-gate the
way unity_entity_asset_resolver._score does (see src/unity_scenebind.py's
_DEFAULT_EMBED_WEIGHT comment): its terms are summed directly and compared
against MIN_BIND_SCORE. This script's real job is computing a PROVABLY SAFE
upper bound for the weight against the exact case
test_kind_alone_is_below_the_threshold locks in — a bare kind-word overlap
(_W_KIND = 1.0) must never reach MIN_BIND_SCORE (2.0) on embedding similarity
alone:

    _W_KIND + embedding_weight * sim < MIN_BIND_SCORE
    embedding_weight < (MIN_BIND_SCORE - _W_KIND) / sim

Mirrors tools/calibrate_semantic_score.py's methodology otherwise: print a
table, then state a number derived from what was actually measured, not
guessed — and say so plainly if no safe, useful weight exists.

Read-only, no network beyond the bge-small model's first-run download
(cached from then on). Needs no Sketchfab client, no catalog, no schema
generation — synthetic EntitySpec/group pairs are enough to exercise the
real bind_score() code path.

Run:  python tools/calibrate_scenebind_embedding.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.unity_schema import EntitySpec  # noqa: E402
from src.unity_scenebind import MIN_BIND_SCORE, _W_KIND, bind_score  # noqa: E402
from src.unity_semantic_embed import text_similarity  # noqa: E402
from src.unity_semantic_match import entity_embedding_text, group_embedding_text  # noqa: E402


def _group(name: str, source_file: str, role: str = "") -> dict:
    return {"name": name, "source_file": source_file, "file": source_file, "role": role}


# ── Case 1 (the one that matters): the exact kind-alone scenario
# test_kind_alone_is_below_the_threshold locks in — no name/hint/query
# overlap at all, only the bare kind word "vehicle" shared between the
# group's own name and the entity's kind.
KIND_ALONE_GROUP = _group("Vehicle", "vehicle.fbx")
KIND_ALONE_ENTITY = EntitySpec(name="Rivals", kind="vehicle", count=3)

# ── Case 2: a positive control — zero token overlap, genuinely a good
# match, to confirm the feature earns its keep (not just that it's safe).
POSITIVE_GROUP = _group("VictorianMonobike", "victorian_monobike.fbx")
POSITIVE_ENTITY = EntitySpec(name="Racer", kind="vehicle", count=1,
                             model_hint="high-speed two-wheeler")

# ── Case 3: informational only, not a number to solve for — the role veto
# is a hard `return 0.0` BEFORE bind_score ever reaches the embedding term
# (src/unity_scenebind.py, confirmed by test_a_conflicting_role_is_vetoed_
# not_merely_outscored), so no weight/similarity combination can rescue it.
# Measured here anyway as a live sanity check, not because it changes the
# calibrated weight.
VETO_GROUP = _group("EnemyDrone", "enemy_drone.fbx", role="enemy")
VETO_ENTITY = EntitySpec(name="Hero", kind="player", count=1, tag="Player")


def main() -> None:
    print("=== Case 1: kind-alone (the case the weight must stay safe against) ===")
    sim = text_similarity(group_embedding_text(KIND_ALONE_GROUP),
                          entity_embedding_text(KIND_ALONE_ENTITY, KIND_ALONE_ENTITY.kind))
    if sim is None or sim <= 0.0:
        print("  similarity is None/<=0 — the embedding term never fires for this "
              "pair regardless of weight; any positive weight is trivially safe here.")
        safe_bound = None
    else:
        safe_bound = (MIN_BIND_SCORE - _W_KIND) / sim
        print(f"  text_similarity = {sim:.3f}")
        print(f"  bind_score today (embed_weight=0.0) = "
              f"{bind_score(KIND_ALONE_GROUP, KIND_ALONE_ENTITY):.3f}  "
              f"(< {MIN_BIND_SCORE} = correctly unbound)")
        print(f"  SAFE UPPER BOUND: embedding_weight < {safe_bound:.3f} keeps this "
              f"pair below MIN_BIND_SCORE ({MIN_BIND_SCORE}) on embedding alone")
        for w in (0.25, 0.5, 0.75, 1.0):
            s = bind_score(KIND_ALONE_GROUP, KIND_ALONE_ENTITY, embed_weight=w)
            verdict = "SAFE (stays unbound)" if s < MIN_BIND_SCORE else "UNSAFE — crosses the threshold"
            print(f"    embed_weight={w:.2f} -> bind_score={s:.3f}  [{verdict}]")

    print("\n=== Case 2: positive control (zero keyword overlap, semantically strong) ===")
    sim2 = text_similarity(group_embedding_text(POSITIVE_GROUP),
                           entity_embedding_text(POSITIVE_ENTITY, POSITIVE_ENTITY.kind))
    base = bind_score(POSITIVE_GROUP, POSITIVE_ENTITY)  # embed_weight=0.0 -> today's score
    print(f"  text_similarity = {sim2:.3f}" if sim2 is not None else "  similarity = n/a")
    print(f"  bind_score today (embed_weight=0.0) = {base:.3f}  "
          f"(< {MIN_BIND_SCORE} = today: correctly UNBOUND despite being a good match)")
    if safe_bound:
        for w in (0.25, 0.5, min(0.75, safe_bound)):
            s = bind_score(POSITIVE_GROUP, POSITIVE_ENTITY, embed_weight=w)
            print(f"    embed_weight={w:.2f} -> bind_score={s:.3f}  "
                  f"[{'now binds' if s >= MIN_BIND_SCORE else 'still unbound'}]")

    print("\n=== Case 3: role veto (informational — provably immune, not calibrated) ===")
    sim3 = text_similarity(group_embedding_text(VETO_GROUP),
                           entity_embedding_text(VETO_ENTITY, VETO_ENTITY.kind))
    print(f"  text_similarity = {sim3:.3f}" if sim3 is not None else "  similarity = n/a")
    for w in (0.5, 10.0):
        s = bind_score(VETO_GROUP, VETO_ENTITY, embed_weight=w)
        print(f"    embed_weight={w:.2f} -> bind_score={s:.3f}  "
              f"[{'VETOED, as required' if s == 0.0 else 'BUG: veto did not hold'}]")

    print("\nPick scenebind.embedding_weight below Case 1's safe upper bound, and "
          "confirm in Case 2 that the chosen weight is still small enough that "
          "the positive control needs its own keyword signal (not embedding "
          "similarity alone) to actually bind — the embedding term is meant to "
          "help among candidates keywords already partially found, not to "
          "replace keyword evidence outright. If Case 1's safe bound is smaller "
          "than what Case 2 needs to demonstrate real value, say so — that is a "
          "genuine finding, not a number to split the difference on.")


if __name__ == "__main__":
    main()
