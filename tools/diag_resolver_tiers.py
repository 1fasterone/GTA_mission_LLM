"""
Read-only instrumentation of the per-entity asset resolver.

For every entity in the three captured OOD schemas, reports which TIER fired and
what EVIDENCE won: the query-token weights the entity produced, the score of the
model it got, and the runner-up candidates it beat.

No behaviour change. Tier attribution is done with a line tracer scoped to
resolve_entity_assets' own code object, mapped against the `# -- Tier X` markers
in its source; scores come from wrapping the module's own _score(). The resolver
runs unmodified and its result is reported as-is.

Writes tools/out/resolver_tiers.json.

Run:  python tools/diag_resolver_tiers.py
"""

from __future__ import annotations

import bisect
import json
import re
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# tier names come from source comments containing box-drawing chars; console is cp1252
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src import unity_entity_asset_resolver as R  # noqa: E402
from src.unity_generator import UnityProjectGenerator  # noqa: E402
from src.unity_schema import UnitySchema  # noqa: E402

STEP3_PATH = _REPO_ROOT / "tools" / "out" / "step3_schemas.json"
OUT_PATH = _REPO_ROOT / "tools" / "out" / "resolver_tiers.json"

_TIER_RE = re.compile(r"#\s*[-─]{2,}\s*Tier\s+(\S+?)\s*:?\s*(.*?)\s*[-─]*\s*$")


def tier_markers() -> tuple[list[int], list[str]]:
    """Line numbers of the `# -- Tier X` headers inside the resolver source."""
    src_path = Path(R.__file__)
    lines = src_path.read_text(encoding="utf-8").splitlines()
    starts, names = [], []
    for i, line in enumerate(lines, start=1):
        m = _TIER_RE.search(line.strip())
        if m and line.strip().startswith("#"):
            starts.append(i)
            names.append(f"Tier {m.group(1)}" + (f" ({m.group(2)})" if m.group(2) else ""))
    return starts, names


def loop_end_line() -> int:
    """First line of the post-loop tail of resolve_entity_assets.

    The per-entity `for e in ...` loop variable stays bound after the loop ends,
    so every line of the tail (the `if unmatched:` raise, the genre-coherence
    check, the return) would otherwise be attributed to the LAST entity — and
    the tail sits past the Tier 4 marker, which silently reported the final
    entity of every schema as "Tier 4". Lines at or after this one are ignored.
    """
    lines = Path(R.__file__).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines, start=1):
        if re.match(r"^    if unmatched:", line):
            return i
    raise RuntimeError("could not locate the post-loop tail of resolve_entity_assets")


class Tracer:
    """Attributes executed lines of resolve_entity_assets to (entity, tier)."""

    def __init__(self, starts: list[int], names: list[str], end: int):
        self.starts, self.names, self.end = starts, names, end
        self.current_entity: str | None = None
        self.tiers: dict[str, list[str]] = {}
        self._code = R.resolve_entity_assets.__code__

    def tier_for(self, lineno: int) -> str | None:
        idx = bisect.bisect_right(self.starts, lineno) - 1
        return self.names[idx] if idx >= 0 else None

    def __call__(self, frame, event, arg):
        if frame.f_code is not self._code:
            return None
        if event == "call":
            return self.__call__
        if event == "line":
            if frame.f_lineno >= self.end:   # post-loop tail: `e` is stale
                return self.__call__
            ent = frame.f_locals.get("e")
            name = getattr(ent, "name", None)
            if name:
                self.current_entity = name
                tier = self.tier_for(frame.f_lineno)
                if tier:
                    seq = self.tiers.setdefault(name, [])
                    if not seq or seq[-1] != tier:
                        seq.append(tier)
        return self.__call__


def main() -> int:
    if not STEP3_PATH.exists():
        print(f"missing {STEP3_PATH}")
        return 1
    schemas = json.loads(STEP3_PATH.read_text(encoding="utf-8"))["schemas"]
    config = yaml.safe_load((_REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    pipe_cfg = config.get("unity_pipeline", {})
    asset_cfg = config.get("assets", {}) or {}
    sk_cfg = config.get("sketchfab", {}) or {}
    lookups_dir = pipe_cfg.get("lookups_dir", "lookups")

    starts, names = tier_markers()
    end = loop_end_line()
    print(f"tier markers: {len(starts)} -> {', '.join(names)}\n")

    gen = UnityProjectGenerator(template_dir=pipe_cfg.get("unity_template_dir", "templates/unity"))
    payload: dict = {
        "_provenance": {
            "min_match_score": float(asset_cfg.get("min_match_score", R._DEFAULT_MIN_SCORE)),
            "strong_hit": R._STRONG_HIT,
            "strict": bool(asset_cfg.get("strict")),
            "sketchfab_enabled": bool(sk_cfg.get("enabled")),
            "tier_markers": names,
            "note": "Read-only. Resolver run unmodified; tiers traced, scores wrapped. Uses the post-fixer schema each project generated from.",
        },
        "runs": {},
    }

    # Prefer the FINAL (post-fixer) schema each run actually generated from —
    # Step 5 backfills model_hint from entity names, so the Step-3 schema would
    # score with empty hints and diverge from what really happened.
    final_schemas = {
        "ood_stack":   Path("C:/u/PlateStacker.schema.json"),
        "ood_sort":    Path("C:/u/BlockSorter/BlockSorter.schema.json"),
        "ood_balance": Path("C:/u/TrayBalance/TrayBalance.schema.json"),
    }

    for pid, rec in schemas.items():
        if not rec.get("validated"):
            continue
        fin = final_schemas.get(pid)
        if fin and fin.exists():
            schema = UnitySchema.model_validate(json.loads(fin.read_text(encoding="utf-8")))
            src_label = f"post-fixer ({fin.name})"
        else:
            schema = UnitySchema.model_validate(rec["schema"])
            src_label = "step-3 (pre-fixer)"
        print(f"[schema source: {src_label}]")
        print("=" * 78)
        print(f"{pid} | {rec['prompt']}")
        print("=" * 78)

        # wrap the module's own _score to capture every candidate it scored
        real_score = R._score
        seen: dict[str, dict[str, float]] = {}
        tracer = Tracer(starts, names, end)

        def scored(entry, qw, kind, _real=real_score):
            s = _real(entry, qw, kind)
            ent = tracer.current_entity
            if ent and s > 0:
                seen.setdefault(ent, {})[entry.get("filename", "?")] = round(s, 4)
            return s

        R._score = scored
        sys.settrace(tracer)
        try:
            used_models, emp = gen._resolve_all_models(
                schema, lookups_dir, None, None, None,
                bool(asset_cfg.get("strict")), asset_cfg, sk_cfg,
            )
            err = None
        except Exception as exc:
            used_models, emp, err = {}, {}, f"{type(exc).__name__}: {exc}"
        finally:
            sys.settrace(None)
            R._score = real_score

        entry_out = {"prompt": rec["prompt"], "error": err, "entities": {}}
        catalog = R.load_catalog(str(lookups_dir))
        by_file = {a.get("filename"): a for a in catalog.get("assets", [])}
        scene_words = set()

        for e in schema.entities:
            qw = R._query_weights(e, scene_words)
            strong = {t: w for t, w in qw.items() if w >= R._STRONG_HIT}
            path = emp.get(e.name)
            chosen_file = Path(path).name if path else None
            cand = seen.get(e.name, {})
            top = sorted(cand.items(), key=lambda kv: -kv[1])[:5]
            chosen_score = None
            for fn, s in cand.items():
                if chosen_file and Path(fn).stem == Path(chosen_file).stem:
                    chosen_score = s
            tier_seq = tracer.tiers.get(e.name, [])
            entry_out["entities"][e.name] = {
                "kind": e.kind,
                "model_hint": e.model_hint,
                "sketchfab_query": getattr(e, "sketchfab_query", ""),
                "strong_tokens": dict(sorted(strong.items(), key=lambda kv: -kv[1])),
                "resolved_path": path,
                "chosen_score": chosen_score,
                "tier_sequence": tier_seq,
                "tier_fired": tier_seq[-1] if tier_seq else None,
                "top_candidates": top,
                "n_scored_above_zero": len(cand),
            }
            print(f"\n  {e.name}  (kind={e.kind}, hint={e.model_hint!r})")
            print(f"    tier fired : {tier_seq[-1] if tier_seq else '?'}")
            print(f"    resolved   : {path}")
            print(f"    score      : {chosen_score}")
            print(f"    strong toks: {dict(list(strong.items())[:8])}")
            if top:
                print(f"    top cands  : {top}")
        if err:
            print(f"\n  [resolver raised] {err}")

        payload["runs"][pid] = entry_out
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True, default=str) + "\n",
            encoding="utf-8", newline="\n",
        )

    print(f"\nwrote {OUT_PATH.relative_to(_REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
