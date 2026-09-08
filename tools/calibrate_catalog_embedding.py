"""
Calibrate assets.embedding_weight (src/unity_entity_asset_resolver.py's
catalog-tier embedding blend, docs/COMPILER_AUDIT.md Gap E / solution #7)
against real catalog entries, rather than shipping the guessed 0.5
placeholder in config.example.yaml unverified.

Mirrors tools/calibrate_semantic_score.py's methodology exactly: measure a
few real, already-documented cases (both a cross-domain near-miss the
keyword scorer's own docstrings name as a live incident, and a few
genuinely-correct pairs), print a table, and look for a clear score gap.
The suggested weight is read off that gap, not a formula — if no such gap
exists, that is the finding to report, not a number to invent.

Read-only against the already-committed lookups/unity/asset_catalog.json —
no Sketchfab, no live search. Needs only the bge-small text embedder (network
on the FIRST run only, to fetch it into the fastembed cache — cached from
then on, same as the RAG embedder and tools/calibrate_semantic_score.py's
CLIP pair).

Run:  python tools/calibrate_catalog_embedding.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.unity_asset_catalog import load_catalog  # noqa: E402
from src.unity_semantic_embed import text_similarity  # noqa: E402
from src.unity_semantic_match import catalog_entry_embedding_text  # noqa: E402

# (entity_text, note, filename_substring_to_look_up) — entity_text mirrors
# entity_embedding_text()'s "name kind model_hint sketchfab_query" shape by
# hand, so this script exercises the exact string the real resolver would
# build. `note` labels the printed report; it is not part of the scoring.
CASES: list[tuple[str, str, str]] = [
    ("ScoreBoard ui score board ui", "CROSS-DOMAIN NEAR-MISS: "
     "_passes_kind_scope's own docstring names this exact live incident — "
     "a UI scoreboard entity shares the token 'board' with skateboard.fbx "
     "(category vehicle). That guard already blocks it structurally; this "
     "measures whether embedding similarity would ALSO have flagged it as "
     "a poor match, or — worse — read it as deceptively similar.",
     "skateboard"),
    ("Trees prop pine tree", "control — genuinely correct pairing", "pine"),
    ("RaceCar vehicle sports car racing", "control — genuinely correct pairing", "race_car"),
    ("Crate prop wooden crate", "control — genuinely correct pairing", "crate"),
    ("AtmosphericClouds prop sky clouds vertical", "historical near-miss "
     "(pre-_STRONG_HIT-gate): _score's own docstring names 'AtmosphericClouds' "
     "grabbing steps.fbx via the description word 'vertical' — the keyword "
     "gate already closed this; measuring it anyway as a sanity check that "
     "embeddings don't reopen it.", "stair"),
]


def main() -> None:
    catalog = load_catalog(str(_REPO_ROOT / "lookups"))
    assets = [a for a in catalog.get("assets", []) if a.get("source") != "sketchfab"]
    if not assets:
        print("No (non-Sketchfab) catalog entries found under lookups/unity/ — "
              "run `python main.py unity-catalog` first.")
        return

    all_sims: list[float] = []
    for entity_text, note, substr in CASES:
        print(f"\n=== {entity_text!r} — {note} ===")
        matches = [a for a in assets if substr in a.get("filename", "").lower()][:4]
        if not matches:
            print(f"  (no catalog entry with {substr!r} in its filename — skipping)")
            continue
        for a in matches:
            entry_text = catalog_entry_embedding_text(a)
            sim = text_similarity(entity_text, entry_text)
            if sim is None:
                print(f"  sim=  n/a   {a['filename']!r} (embedder unavailable)")
                continue
            all_sims.append(sim)
            print(f"  sim={sim:.3f}   {a['filename']!r}  (entry text: {entry_text[:70]!r})")

    if all_sims:
        s = sorted(all_sims)
        print(f"\nsimilarity range: {s[0]:.3f} .. {s[-1]:.3f} (median {s[len(s)//2]:.3f})")
    print("\nRead the per-pair table above: the cross-domain ScoreBoard/skateboard "
          "row should sit measurably BELOW the genuinely-correct control rows. "
          "assets.embedding_weight is additive on top of an ALREADY keyword-"
          "qualified candidate (see _final_score's `if s > 0.0:` guard) and is "
          "gated separately by _passes_kind_guard/_passes_kind_scope, so — "
          "unlike scenebind's weight — there is no threshold-crossing risk here "
          "to solve for; this run is about confirming the term adds signal in "
          "the right direction, not about finding a safety ceiling. Pick a "
          "weight that meaningfully separates the controls from the near-miss "
          "in the reordering it would produce; if every row lands in the same "
          "narrow band, say so rather than picking a number that doesn't "
          "actually discriminate.")


if __name__ == "__main__":
    main()
