"""
Calibrate sketchfab.semantic_pool_size (docs/COMPILER_AUDIT.md Gap I /
solution #13) against real Sketchfab search results, rather than shipping
the default-preserving 6 in config.example.yaml with no live evidence
either way.

semantic_filter()/semantic_filter_frozen() (src/unity_semantic_embed.py)
only ever score the first `max_candidates` (default 6) entries of a
keyword-ranked Sketchfab search result list — SketchfabClient.search()
itself already fetches up to 24 per query at no extra network cost, so
positions 7-24 are keyword-ranked but never semantically reconsidered. This
script measures, for a handful of real queries, whether the
BEST-semantically-matching candidate in the full up-to-24 pool ever sits
outside the top 6 keyword-ranked positions — the concrete, live evidence
Gap I's "a search engine that can't read sentences" narrative predicts, but
that no offline/synthetic data can demonstrate (unlike
tools/calibrate_semantic_score.py, tools/calibrate_catalog_embedding.py, and
tools/calibrate_scenebind_embedding.py, which all calibrate against local or
synthetic data — this is the first calibration script here that needs the
LIVE Sketchfab search API, since the phenomenon under test is inherently
about a live third-party search engine's own ranking behavior).

For each query: keyword-rank the full licensed result pool the same way
cli/unity_assets.py's own display-table `_rank_key` does (a raw-dict-shaped
ranking, distinct from sketchfab_freeze._rank()'s synthetic-entry-shaped
one), then compute BOTH image_similarity() and text_similarity() for EVERY
candidate (not just the first 6) so the printed table shows, per query, the
keyword rank of the actual best semantic match. If every query's best
semantic match already sits inside today's top 6, that is itself the
finding: say so, and leave the default at 6 rather than inventing a larger
number with no evidence behind it.

Read-only against Sketchfab (search only, no download/convert/freeze — this
never writes to lookups/unity/sketchfab_catalog.json). Needs the same
SketchfabClient the resolver uses — same constraint as
tools/calibrate_semantic_score.py, not something new here: a working
Sketchfab client (playwright + chromium installed) and either
sketchfab.api_token in config.yaml or SKETCHFAB_API_TOKEN in the
environment/.env. SketchfabClient.available() gates ALL calls, including
search, on a configured token even though Sketchfab's own public /search
endpoint does not actually require one.

Degrades gracefully, never crashes: a query with zero licensed results is
reported and skipped; a candidate whose thumbnail can't be fetched or whose
embedder is unavailable is reported as unmeasured (n/a), not treated as a
zero score.

Run:  python tools/calibrate_sketchfab_rerank_pool.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()  # SKETCHFAB_API_TOKEN, same as cli/common.py

from src.sketchfab_client import SketchfabClient  # noqa: E402
from src.unity_asset_catalog import tokenize  # noqa: E402
from src.unity_semantic_embed import image_similarity, text_similarity  # noqa: E402
from src.unity_sketchfab_vision import _thumbnail_url  # noqa: E402

CONFIG_PATH = _REPO_ROOT / "config.yaml"

# (query, note) — queries chosen to plausibly expose keyword-search-buries-
# a-good-match: broad/generic category words (many near-synonym competitors
# a keyword scorer can't rank well), plus the historical paratrooper case
# re-used from calibrate_semantic_score.py as a continuity check (its
# top-ranked keyword hit was already confirmed wrong — worth knowing whether
# a BETTER semantic match was sitting lower in the very same pool that run
# never looked past).
QUERIES: list[tuple[str, str]] = [
    ("realistic paratrooper",
     "continuity case from calibrate_semantic_score.py — was a better "
     "semantic match sitting outside the pool that run scored?"),
    ("traffic cone", "generic prop — many near-synonym competitors expected"),
    ("wooden crate", "control query — no known failure"),
    ("medieval sword", "generic weapon — many stylistically-mixed competitors expected"),
    # Widened 2026-09-10 at the user's request, to see past the first, small
    # 4-query sample — spans the asset categories this compiler actually
    # resolves for generated games (vehicle/weapon/character/structure/
    # nature/urban-prop), not just the two original incident-driven queries.
    ("police car", "vehicle — common GTA-style mission asset"),
    ("motorcycle", "vehicle — many near-identical stock competitors expected"),
    ("skateboard ramp", "prop — matches this repo's own skate-park test prompts"),
    ("street lamp", "generic urban prop — many stylistically-mixed competitors expected"),
    ("shipping container", "industrial prop — common level-dressing asset"),
    ("pine tree", "nature — matches this repo's own test fixtures' domain"),
    ("zombie", "character — high stylistic variance expected across results"),
    ("assault rifle", "weapon — common GTA-style mission asset"),
    ("warehouse building", "structure — large, stylistically varied category"),
    ("drone", "vehicle/prop — matches this repo's own enemy_drone.fbx naming convention"),
]

POOL_SIZE = 24  # matches SketchfabClient.search()'s own default `count`
CURRENT_DEFAULT = 6  # src.unity_semantic_embed.semantic_filter's own default


def _rank_key(query: str, m: dict) -> float:
    """Same keyword-overlap idea as sketchfab_freeze._rank(), reproduced
    directly against the raw search-result dict's own name/tags — mirrors
    cli/unity_assets.py's own local `_rank_key` (its own display-table
    ranker, kept separate from _rank()'s synthetic-entry-shaped contract for
    the exact same reason)."""
    kw = {t.lower() for t in tokenize(m.get("name") or "")}
    kw |= {str(t.get("name", "")).lower() for t in (m.get("tags") or []) if isinstance(t, dict)}
    return float(len(kw & {t.lower() for t in tokenize(query)}))


def main() -> None:
    if not CONFIG_PATH.is_file():
        print(f"{CONFIG_PATH} not found — copy config.example.yaml to "
              "config.yaml first (see README).")
        return
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    sk_cfg = dict(cfg.get("sketchfab") or {})
    sk_cfg.setdefault("enabled", True)
    client = SketchfabClient(sk_cfg)
    if not client.available():
        print("SketchfabClient.available() is False — no token reachable "
              "(set sketchfab.api_token or SKETCHFAB_API_TOKEN). Nothing to "
              "calibrate against; exiting.")
        return

    rescue_found = False

    for query, note in QUERIES:
        print(f"\n=== {query!r} — {note} ===")
        results = client.filter_licensed(client.search(query, count=POOL_SIZE))
        if not results:
            print("  (no licensed search results — skipping)")
            continue

        ranked = sorted(results, key=lambda m: _rank_key(query, m), reverse=True)

        rows = []  # (keyword_rank, name, img_sim, txt_sim)
        for i, model in enumerate(ranked, start=1):
            name = model.get("name") or "?"
            description = str(model.get("description") or name)
            url = _thumbnail_url(model)
            img_bytes = client.fetch_image_bytes(url) if url else None
            img_sim = image_similarity(query, img_bytes) if img_bytes else None
            txt_sim = text_similarity(query, description)
            rows.append((i, name, img_sim, txt_sim))
            img_s = f"{img_sim:.3f}" if img_sim is not None else "  n/a"
            txt_s = f"{txt_sim:.3f}" if txt_sim is not None else "  n/a"
            marker = ("  <-- within today's pool" if i <= CURRENT_DEFAULT
                     else "  (outside today's pool)")
            print(f"  #{i:>2} image={img_s}  text={txt_s}   {name!r}{marker}")

        measured = [r for r in rows if r[2] is not None]
        if not measured:
            print("  (nothing measurable for this query — embedder/thumbnails "
                  "unavailable; skipping the rank summary)")
            continue
        best_rank, best_name, best_img, _best_txt = max(measured, key=lambda r: r[2])
        print(f"  Best image_similarity candidate: #{best_rank} {best_name!r} "
              f"(image={best_img:.3f})")
        if best_rank > CURRENT_DEFAULT:
            rescue_found = True
            print(f"  ==> RESCUE CASE: best semantic match ranked #{best_rank} by "
                  f"keyword score — outside today's semantic_pool_size="
                  f"{CURRENT_DEFAULT}, only reachable with a wider pool.")
        else:
            print(f"  ==> best semantic match already ranked #{best_rank} — today's "
                  f"semantic_pool_size={CURRENT_DEFAULT} already reaches it.")

    print("\n" + "=" * 70)
    if rescue_found:
        print("At least one query's best semantic match sat outside the "
              f"current semantic_pool_size={CURRENT_DEFAULT} default — real "
              "rescue value demonstrated. Pick a new default that is the "
              "smallest pool size covering the rank positions seen above, "
              "not an arbitrarily large one (cost scales ~linearly with pool "
              "size — see config.example.yaml's sketchfab.semantic_pool_size "
              "comment).")
    else:
        print(f"No query's best semantic match sat outside the current "
              f"semantic_pool_size={CURRENT_DEFAULT} default across this "
              "query set. That is itself a finding: either widen the query "
              "set and re-run, or leave the default at 6 rather than "
              "inventing a larger number with no evidence behind it.")


if __name__ == "__main__":
    main()
