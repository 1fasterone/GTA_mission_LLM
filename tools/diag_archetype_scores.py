"""
Read-only diagnostic for src/unity_archetypes.py.

Runs a fixed corpus (out-of-distribution / in-distribution / control) through the
EXISTING scoring path and dumps the full per-label score vector, to confirm or
refute the "no abstain state -> near-flat vector -> argmax is noise" hypothesis.

Nothing here reimplements scoring. The module's own _normalize_for_matching(),
_score() and classify() are called unchanged; the full vector is recovered by
calling _score() once per label using single-row slices of the module's own
_PERSPECTIVE / _LOCOMOTION / _GENRE tables.

Run:  python tools/diag_archetype_scores.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.unity_archetypes import (  # noqa: E402
    _GENRE,
    _LOCOMOTION,
    _PERSPECTIVE,
    _normalize_for_matching,
    _score,
    classify,
)

OUT_PATH = _REPO_ROOT / "tools" / "out" / "archetype_scores.json"

# axis name -> the module's own table object (not a copy of its contents)
AXES = [
    ("game_type", _PERSPECTIVE),
    ("locomotion", _LOCOMOTION),
    ("genre", _GENRE),
]

CORPUS = [
    ("ood_ring", "make a game where the player throws rings onto a box"),
    ("ood_stack", "a game about stacking plates without them falling"),
    ("ood_sort", "sort falling colored blocks into matching bins"),
    ("ood_balance", "balance a tray of drinks while walking across a room"),
    ("inD_race", "make a racing game"),
    ("inD_shooter", "a first person shooter in a warehouse"),
    ("inD_platform", "a 2D platformer with wall jumps"),
    ("ctrl_empty", ""),
    ("ctrl_noise", "xqz frobnicate wumpus telemetry"),
]

# Real Step-2 output captured by tools/capture_step2_concepts.py (frozen
# artifact, so this script stays deterministic). Any prompt id present there
# overrides the synthetic fixture below and is reported as source="real".
CAPTURED_PATH = _REPO_ROOT / "tools" / "out" / "step2_concepts.json"


def _load_captured() -> dict[str, str]:
    if not CAPTURED_PATH.exists():
        return {}
    blob = json.loads(CAPTURED_PATH.read_text(encoding="utf-8"))
    return {pid: rec["concept"] for pid, rec in blob.get("concepts", {}).items()}


# SYNTHETIC FIXTURES — stand-ins for prompt ids with no captured Step-2 text.
# Ordinary concept prose written for each brief, deliberately NOT tuned to hit
# or dodge the keyword tables. They exercise the 1x concept branch of _score()
# (an `elif` — a concept hit only scores when the prompt missed that keyword).
CONCEPTS = {
    "ood_ring": (
        "The player stands at a carnival stall and lobs wooden rings toward a crate "
        "on a table. Each throw is scored by how cleanly the ring settles over the "
        "box. Rounds get harder as the box is moved further away."
    ),
    "ood_stack": (
        "A steady-hands game about balancing plates on a growing stack. Each new "
        "plate wobbles the tower, and the player must counter the sway before it "
        "topples. One wrong nudge ends the run."
    ),
    "ood_sort": (
        "Colored blocks fall from the top of the screen and the player routes each "
        "one into the bin that matches its color. The pace increases over time, and "
        "a missed block costs a life."
    ),
    "ood_balance": (
        "The player carries a tray of full drinks across a crowded room. Tilting the "
        "tray too far spills a glass. The goal is to reach the far table with "
        "everything still upright."
    ),
    "inD_race": (
        "A street-level circuit racer set in a neon city. The player picks a car, "
        "qualifies on a short track, and runs three laps against rivals. Sliding "
        "through corners builds boost."
    ),
    "inD_shooter": (
        "A first-person shooter set in an abandoned warehouse. The player clears "
        "rooms with a shotgun while enemies flank from cover. Tight corridors, loud "
        "gunplay, no regenerating health."
    ),
    "inD_platform": (
        "A 2D side-scrolling platformer where the hero chains wall jumps to climb "
        "vertical shafts. Precision platforming with tight controls and no combat."
    ),
    "ctrl_empty": "",
    "ctrl_noise": (
        "The wumpus frobnicates telemetry across an xqz lattice. Frobnication yields "
        "wumpus telemetry."
    ),
}


def score_one_prompt(prompt: str, concept: str = "") -> dict:
    """Full label-level score vector via the module's own _score(), one label at a time."""
    p = _normalize_for_matching(prompt)
    c = _normalize_for_matching(concept)

    vector: dict[str, dict[str, int]] = {}
    hits: dict[str, dict[str, list[str]]] = {}
    # score = 3*(prompt hits) + 1*(concept hits) and len(kws) = both, so the
    # 1x contribution is exact arithmetic on _score()'s own return values:
    #   3*len(kws) - score == 2 * (concept-only hits)
    n_kw_1x = 0
    for axis, table in AXES:
        vector[axis] = {}
        hits[axis] = {}
        for label, keywords in table:
            _name, score, kws = _score(p, c, [(label, keywords)])
            vector[axis][label] = score
            if kws:
                hits[axis][label] = kws
                n_kw_1x += (3 * len(kws) - score) // 2

    flat = [
        (axis, label, score)
        for axis, table in AXES
        for label, score in ((lbl, vector[axis][lbl]) for lbl, _kw in table)
    ]

    # argmax uses the module's own tie rule (strict >, first-encountered wins)
    best_axis, best_label, best_score = None, None, 0
    for axis, label, score in flat:
        if score > best_score:
            best_axis, best_label, best_score = axis, label, score

    ordered = sorted((s for _a, _l, s in flat), reverse=True)
    top1 = ordered[0] if ordered else 0
    top2 = ordered[1] if len(ordered) > 1 else 0

    intent = classify(prompt, concept)
    nonzero_axis_count = sum(
        1
        for conf in (
            intent.game_type_confidence,
            intent.locomotion_confidence,
            intent.genre_confidence,
        )
        if conf > 0
    )
    n_matched_keywords = sum(len(kws) for axis in hits for kws in hits[axis].values())

    return {
        "prompt": prompt,
        "concept": concept,
        "normalized_prompt": p,
        "normalized_concept": c,
        "scores": vector,
        "matched_keywords": hits,
        "argmax_axis": best_axis,
        "argmax_label": best_label,
        "top1": top1,
        "top2": top2,
        "margin": top1 - top2,
        "sum_scores": sum(s for _a, _l, s in flat),
        "nonzero_axis_count": nonzero_axis_count,
        "nonzero_label_count": sum(1 for _a, _l, s in flat if s > 0),
        "n_matched_keywords": n_matched_keywords,
        "n_matched_keywords_at_1x": n_kw_1x,
        "classify": {
            "game_type": intent.game_type,
            "locomotion": intent.locomotion,
            "genre": intent.genre,
            "game_type_confidence": intent.game_type_confidence,
            "locomotion_confidence": intent.locomotion_confidence,
            "genre_confidence": intent.genre_confidence,
            "matched": intent.matched,
        },
    }


def _print_table(title: str, results: dict, pass_key: str, show_1x: bool) -> None:
    header = (
        f"{'prompt_id':<13}| {'argmax_label':<14}| {'top1':>4} | {'top2':>4} | "
        f"{'margin':>6} | {'sum':>4} | {'nz_axes':>7} | {'n_kw':>4}"
    )
    if show_1x:
        header += f" | {'n_kw@1x':>7} | {'src':<9}"
    print(title)
    print(header)
    print("-" * len(header))
    for pid, _text in CORPUS:
        r = results[pid][pass_key]
        row = (
            f"{pid:<13}| {str(r['argmax_label']):<14}| {r['top1']:>4} | {r['top2']:>4} | "
            f"{r['margin']:>6} | {r['sum_scores']:>4} | {r['nonzero_axis_count']:>7} | "
            f"{r['n_matched_keywords']:>4}"
        )
        if show_1x:
            row += f" | {r['n_matched_keywords_at_1x']:>7} | {results[pid]['concept_source']:<9}"
        print(row)


def main() -> int:
    captured = _load_captured()
    results = {}
    for pid, text in CORPUS:
        concept = captured.get(pid, CONCEPTS[pid])
        results[pid] = {
            "concept_source": "real" if pid in captured else "synthetic",
            "prompt_only": score_one_prompt(text),
            "with_concept": score_one_prompt(text, concept),
        }
    n_real = sum(1 for r in results.values() if r["concept_source"] == "real")
    print(f"concepts: {n_real} real (captured Step-2), {len(results) - n_real} synthetic\n")

    _print_table("PASS 1 - prompt only (3x)", results, "prompt_only", show_1x=False)
    print()
    _print_table(
        "PASS 2 - prompt (3x) + synthetic Step-2 concept (1x)",
        results, "with_concept", show_1x=True,
    )

    print()
    print("DELTA - pass 2 vs pass 1")
    dh = f"{'prompt_id':<13}| {'argmax':<20}| {'d_top1':>6} | {'d_margin':>8} | {'d_sum':>5}"
    print(dh)
    print("-" * len(dh))
    for pid, _text in CORPUS:
        a, b = results[pid]["prompt_only"], results[pid]["with_concept"]
        shift = (
            f"{a['argmax_label']}"
            if a["argmax_label"] == b["argmax_label"]
            else f"{a['argmax_label']}->{b['argmax_label']}"
        )
        print(
            f"{pid:<13}| {shift:<20}| {b['top1'] - a['top1']:>+6} | "
            f"{b['margin'] - a['margin']:>+8} | {b['sum_scores'] - a['sum_scores']:>+5}"
        )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(results, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    OUT_PATH.write_text(payload, encoding="utf-8", newline="\n")

    digest = hashlib.blake2b(payload.encode("utf-8")).hexdigest()
    print()
    print(f"wrote {OUT_PATH.relative_to(_REPO_ROOT).as_posix()}")
    print(f"blake2b({OUT_PATH.name}) = {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
