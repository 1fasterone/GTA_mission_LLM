"""
Calibrate sketchfab.min_image_similarity / min_text_similarity against real
Sketchfab search results, rather than shipping the guessed 0.20 / 0.30
placeholders in config.example.yaml unverified.

For a small fixed list of queries — including the live DropZone incident
that motivated src/unity_semantic_embed.py ("realistic paratrooper"
resolving to a Sketchfab model titled "Cartoon Character") plus a few
queries expected to resolve cleanly — this fetches the real top-ranked
candidates, scores each with image_similarity()/text_similarity(), and
prints a table. Look for a clear score gap between the queries' own
best-faith intended match and known-bad candidates like the paratrooper
case; the suggested defaults are the midpoint of that gap, not a formula.

Read-only against Sketchfab (search only, no download/convert/freeze — this
never writes to lookups/unity/sketchfab_catalog.json). Needs:
  - a working Sketchfab client (playwright + chromium installed, and either
    sketchfab.api_token in config.yaml or SKETCHFAB_API_TOKEN in the
    environment/.env)
  - network access to huggingface.co on the FIRST run only, to download the
    CLIP ONNX models (~150-300MB) into the fastembed cache — cached from
    then on, same as the RAG embedder.

Run:  python tools/calibrate_semantic_score.py
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
from src.unity_semantic_embed import image_similarity, text_similarity  # noqa: E402
from src.unity_sketchfab_vision import _thumbnail_url  # noqa: E402

CONFIG_PATH = _REPO_ROOT / "config.yaml"

# (query, note) — `note` is just a label for the printed report, not part of
# the scoring. The paratrooper case is the live, confirmed-wrong DropZone
# resolution this whole feature exists to catch; the others are ordinary
# queries with no known failure, included so the report shows what a
# healthy score range looks like too.
QUERIES: list[tuple[str, str]] = [
    ("realistic paratrooper",
     "LIVE INCIDENT: DropZone's Player entity resolved this to a Sketchfab "
     "model titled 'Cartoon Character' — expect a visibly low score on at "
     "least one candidate near the top of the keyword ranking."),
    ("realistic parachute", "control query — no known failure"),
    ("wooden crate", "control query — no known failure"),
    ("skydiving helmet", "control query — historical near-miss (baseball helmet)"),
]

TOP_N = 4


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

    rows: list[tuple[str, str, float | None, float | None]] = []
    for query, note in QUERIES:
        print(f"\n=== {query!r} — {note} ===")
        results = client.filter_licensed(client.search(query))[:TOP_N]
        if not results:
            print("  (no licensed search results)")
            continue
        for model in results:
            name = model.get("name") or "?"
            description = str(model.get("description") or name)
            url = _thumbnail_url(model)
            img_bytes = client.fetch_image_bytes(url) if url else None
            img_sim = image_similarity(query, img_bytes) if img_bytes else None
            txt_sim = text_similarity(query, description)
            rows.append((query, name, img_sim, txt_sim))
            img_s = f"{img_sim:.3f}" if img_sim is not None else "  n/a"
            txt_s = f"{txt_sim:.3f}" if txt_sim is not None else "  n/a"
            print(f"  image={img_s}  text={txt_s}   {name!r}")

    scored = [(i, t) for _q, _n, i, t in rows if i is not None]
    if scored:
        images = sorted(i for i, _t in scored)
        texts = sorted(t for _i, t in scored)
        print(f"\nimage_similarity range: {images[0]:.3f} .. {images[-1]:.3f} "
              f"(median {images[len(images)//2]:.3f})")
        print(f"text_similarity  range: {texts[0]:.3f} .. {texts[-1]:.3f} "
              f"(median {texts[len(texts)//2]:.3f})")
    print("\nRead the per-candidate table above: pick min_image_similarity /"
          " min_text_similarity so the paratrooper case's 'Cartoon "
          "Character' candidate (and anything equally wrong) falls BELOW "
          "the floor, while the control queries' genuinely-correct top "
          "candidates stay ABOVE it. If no such gap exists, the floors need "
          "a real gap to separate on, not a formula — say so rather than "
          "picking a number that doesn't actually discriminate.")


if __name__ == "__main__":
    main()
