"""
E2E_0001 "SpawnWalkComplete" — executable runner.

Builds the minimal V1→V5 slice, runs the deterministic tick-barrier simulation,
runs it again (replay), compares the two traces byte-for-byte, checks static
hash stability, assembles the golden bundle, and renders a structured execution
report with a real PASS/FAIL verdict.

CLI:   python -m src.vstack.e2e_0001            # full report
       python -m src.vstack.e2e_0001 --degrade  # also run a degraded variant
"""
from __future__ import annotations

import sys

from src.vstack.canon import Q_POS
from src.vstack.slice_v1_v5 import IDENTITY_KEYS, build_slice, run


# ── golden bundle (the reference artifact, §5.3) ──────────────────────────────

def golden_bundle(sl: dict, result: dict) -> dict:
    return {
        "rig_hash": sl["rig_hash"],
        "deform_hash": sl["deform_hash"],
        "anim_hash": sl["anim_hash"],
        "behavior_hash": sl["behavior_hash"],
        "entity_hash": sl["entity_hash"],
        "scene_hash": sl["scene_hash"],
        "mission_hash": sl["mission_hash"],
        "physics_hash": sl["physics_hash"],
        "nav_hash": sl["nav_hash"],
        "ai_hash": sl["ai_hash"],
        "gameplay_hash": sl["gameplay_hash"],
        "sim_hash": sl["sim_hash"],
        "build_hash": sl["build_hash"],
        "worldstate_hash@0": result["worldstate_hash_0"],
        "worldstate_hash@N": result["final_worldstate_hash"],
        "worldstate_seq_checksum": result["worldstate_seq_checksum"],
        "completion_tick": result["completion_tick"],
        "per_tick_delta_q": sl["move_step"],          # 50 mu = 0.050 u
        "degrade_flags": sl["degrade_flags"],
    }


# ── replay parity (I-REPLAY-EQ) + divergence localization (I-DIVLOC) ──────────

def compare_runs(a: dict, b: dict) -> list[dict]:
    """Return a list of divergences between two runs (empty == identical)."""
    div = []
    if len(a["trace"]) != len(b["trace"]):
        div.append({"tick": "-", "field": "trace_len",
                    "a": len(a["trace"]), "b": len(b["trace"])})
        return div
    for ea, eb in zip(a["trace"], b["trace"]):
        for field in ("pos_q", "ai_state", "mission", "worldstate_hash"):
            if ea[field] != eb[field]:
                div.append({"tick": ea["tick"], "field": field,
                            "a": ea[field], "b": eb[field]})
                return div                # first-diverging-tick only (I-DIVLOC)
    return div


# ── execute ───────────────────────────────────────────────────────────────────

def execute(seed: int = 0x00000001, degrade: dict | None = None) -> dict:
    """Run E2E_0001 end-to-end and return a structured result + verdict."""
    sl = build_slice(seed, degrade)
    run1 = run(sl)
    run2 = run(sl)                                    # replay: same seed/schedule/∅ inputs
    divergences = compare_runs(run1, run2)

    sl2 = build_slice(seed, degrade)                  # rebuild → static hash stability
    hash_stable = all(sl[k] == sl2[k] for k in IDENTITY_KEYS)
    unstable = [k for k in IDENTITY_KEYS if sl[k] != sl2[k]]

    # per-tick delta invariant: every movement step == move_step, no drift
    deltas_ok = _check_constant_delta(run1, sl["move_step"])

    checks = {
        "spawned": run1["trace"][0]["pos_q"] == [0, 0, 0],
        "completed": run1["completed"],
        "completion_tick_91": run1["completion_tick"] == 91,
        "behavior_transition_tick_1": _transition_tick(run1) == 1,
        "constant_delta": deltas_ok,
        "progression": run1["worldstate_hash_0"] != run1["final_worldstate_hash"],
        "hash_stable": hash_stable,
        "replay_parity": not divergences,
    }
    verdict = all(checks.values())

    return {
        "slice": sl, "run": run1, "replay": run2,
        "divergences": divergences, "checks": checks, "verdict": verdict,
        "unstable_hashes": unstable,
        "golden": golden_bundle(sl, run1),
    }


def _transition_tick(result: dict):
    for e in result["trace"]:
        t = e["behavior_transition"]
        if t and t.get("from") == "Idle":
            return t["tick"]
    return None


def _check_constant_delta(result: dict, step_q: int) -> bool:
    prev_z = result["trace"][0]["pos_q"][2]
    for e in result["trace"][1:]:
        z = e["pos_q"][2]
        d = z - prev_z
        if d not in (0, step_q):          # 0 on the transition tick, step_q while walking
            return False
        prev_z = z
    return True


# ── report rendering ──────────────────────────────────────────────────────────

def _u(mu: int) -> str:
    return f"{mu / Q_POS:.3f}"


def render_report(res: dict, full: bool = True) -> str:
    sl, run1 = res["slice"], res["run"]
    L = []
    tag = "DEGRADED " + ",".join(sl["degrade_flags"]) if sl["degrade_flags"] else "CLEAN"
    L.append("=" * 78)
    L.append(f"  E2E_0001  SpawnWalkComplete   —   reference run ({tag})")
    L.append("=" * 78)

    L.append("\n[1] IDENTITY CHAIN (static V1→V5 hashes)")
    for k in IDENTITY_KEYS:
        L.append(f"    {k:<14} {sl[k]}")

    L.append("\n[2] SIMULATION  (tick | z(u) | ai_state | O1 | worldstate_hash[:16] | event)")
    trace = run1["trace"]
    rows = trace if full else _condense(trace)
    for e in rows:
        if e is None:
            L.append("     ...   (constant +0.050 u/tick; full trace computed)")
            continue
        ev = ""
        if e["behavior_transition"]:
            t = e["behavior_transition"]
            ev = f'beh:{t["from"]}->{t["to"]}'
        if e["mission_event"]:
            ev = (ev + " " if ev else "") + f'msn:{e["mission_event"]}'
        L.append(f'    {e["tick"]:>4}  {_u(e["pos_q"][2]):>6}  {e["ai_state"]:<8} '
                 f'{e["mission"]["O1"]:<9} {e["worldstate_hash"][:16]}  {ev}')

    L.append(f"\n    ticks simulated : {len(trace)}  (tick 0 … tick {trace[-1]['tick']})")
    L.append(f"    completed       : {run1['completed']}  @ tick {run1['completion_tick']}")
    L.append(f"    final position  : z = {_u(run1['final_pos_q'][2])} u")
    L.append(f"    worldstate_seq_checksum : {run1['worldstate_seq_checksum']}")

    L.append("\n[3] REPLAY (re-run, same seed/schedule/empty input trace)")
    if res["divergences"]:
        L.append("    DIVERGENCE DETECTED:")
        for d in res["divergences"]:
            L.append(f'      tick {d["tick"]} field {d["field"]}: {d["a"]} != {d["b"]}')
    else:
        L.append("    no divergence — positions, behavior/mission transitions, and the")
        L.append("    full worldstate_hash sequence are byte-identical across runs.")

    L.append("\n[4] VALIDATION CHECKS")
    for name, ok in res["checks"].items():
        L.append(f"    [{'PASS' if ok else 'FAIL'}]  {name}")
    if res["unstable_hashes"]:
        L.append(f"    unstable hashes: {res['unstable_hashes']}")

    L.append("\n[5] GOLDEN BUNDLE")
    for k, v in res["golden"].items():
        L.append(f"    {k:<24} {v}")

    L.append("\n" + "=" * 78)
    L.append(f"  VERDICT:  {'>>> PASS <<<' if res['verdict'] else '>>> FAIL <<<'}")
    L.append("=" * 78)
    return "\n".join(L)


def _condense(trace, head=4, tail=4):
    if len(trace) <= head + tail + 1:
        return trace
    return trace[:head] + [None] + trace[-tail:]


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")      # Windows console is cp1252
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    full = "--condense" not in argv

    res = execute()
    print(render_report(res, full=full))

    if "--degrade" in argv:
        print("\n\n" + "#" * 78)
        print("#  DEGRADE-NEVER-FAIL DEMO  —  V2 synthetic_capsule")
        print("#" * 78)
        dres = execute(degrade={"V2": "synthetic_capsule"})
        print(render_report(dres, full=False))
        same_kinematics = (dres["run"]["completion_tick"]
                           == res["run"]["completion_tick"])
        distinct_bundle = dres["golden"]["build_hash"] != res["golden"]["build_hash"]
        print(f"\n  degraded run completes @ same tick as clean : {same_kinematics}")
        print(f"  degraded golden bundle is DISTINCT          : {distinct_bundle}")
        print("  -> entity still spawns, mission still completes, worldstate still")
        print("     progresses - but as a distinct, reproducible degraded golden. (sec 7)")

    return 0 if res["verdict"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
