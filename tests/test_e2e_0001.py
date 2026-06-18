"""
E2E_0001 "SpawnWalkComplete" — the minimal deterministic V1→V5 vertical slice.

These are *real* tests against the runnable reference kernel in src/vstack/ (not
the LLM pipeline): they assert the identity chain is stable, the simulation
completes deterministically at the spec's canonical tick, replay is byte-
identical, and the degrade-never-fail contract holds (a degraded build still
completes but yields a distinct, reproducible golden bundle).

Spec: docs/specs/E2E_0001_SpawnWalkComplete.md
"""
from __future__ import annotations

from src.vstack.e2e_0001 import execute, golden_bundle
from src.vstack.slice_v1_v5 import IDENTITY_KEYS, build_slice, run


def test_identity_chain_is_complete_and_nonempty():
    sl = build_slice()
    for k in IDENTITY_KEYS:
        assert isinstance(sl[k], str) and len(sl[k]) == 32, k   # 16-byte blake2b hex


def test_static_hashes_are_stable_across_rebuilds():
    a, b = build_slice(), build_slice()
    for k in IDENTITY_KEYS:
        assert a[k] == b[k], f"unstable hash: {k}"


def test_run_completes_at_canonical_tick_91():
    r = run(build_slice())
    assert r["completed"] is True
    assert r["completion_tick"] == 91           # 90 movement ticks + 1 transition tick
    assert r["final_pos_q"] == [0, 0, 4500]     # z = 4.500 u, inside the target volume


def test_behavior_transition_at_tick_1():
    r = run(build_slice())
    t1 = r["trace"][1]
    assert t1["tick"] == 1
    assert t1["behavior_transition"] == {"from": "Idle", "to": "WalkTo", "tick": 1}
    assert t1["pos_q"] == [0, 0, 0]             # transition tick → no movement yet


def test_constant_quantized_per_tick_delta():
    r = run(build_slice())
    prev = r["trace"][1]["pos_q"][2]            # z @ tick1 == 0
    for e in r["trace"][2:]:
        assert e["pos_q"][2] - prev == 50       # 0.050 u, exact, no drift
        prev = e["pos_q"][2]


def test_worldstate_hash_progresses_and_is_distinct_per_tick():
    r = run(build_slice())
    seq = r["worldstate_seq"]
    assert seq[0] != seq[-1]                    # progression
    assert len(set(seq)) == len(seq)            # distinct per tick (tick counter folded in)


def test_replay_is_byte_identical():
    sl = build_slice()
    a, b = run(sl), run(sl)
    assert a["worldstate_seq"] == b["worldstate_seq"]
    assert a["worldstate_seq_checksum"] == b["worldstate_seq_checksum"]
    assert [e["pos_q"] for e in a["trace"]] == [e["pos_q"] for e in b["trace"]]


def test_end_to_end_verdict_is_pass():
    res = execute()
    assert res["verdict"] is True, res["checks"]
    assert not res["divergences"]


def test_mission_reaches_complete_terminal():
    r = run(build_slice())
    assert r["trace"][-1]["mission"]["O1"] == "complete"
    # exactly one Idle→WalkTo and one →Arrived/complete transition
    events = [e["mission_event"] for e in r["trace"] if e["mission_event"]]
    assert "O1:inactive->active" in events
    assert "O1:active->complete" in events


# ── degrade-never-fail (§7) ──────────────────────────────────────────────────

def test_degrade_v2_still_completes_with_distinct_golden():
    clean = build_slice()
    degraded = build_slice(degrade={"V2": "synthetic_capsule"})
    rc, rd = run(clean), run(degraded)

    # still spawns, still completes at the same canonical tick, still progresses
    assert rd["completed"] and rd["completion_tick"] == rc["completion_tick"] == 91

    # but it is a DISTINCT, reproducible golden bundle (degrade folded into hashes)
    gc = golden_bundle(clean, rc)
    gd = golden_bundle(degraded, rd)
    assert gd["deform_hash"] != gc["deform_hash"]
    assert gd["build_hash"] != gc["build_hash"]
    assert gd["worldstate_hash@N"] != gc["worldstate_hash@N"]
    assert gd["degrade_flags"] == ["V2=synthetic_capsule"]

    # the degraded run is itself deterministic (replay-stable)
    assert run(degraded)["worldstate_seq"] == rd["worldstate_seq"]


def test_multiple_degrades_still_complete():
    for deg in ({"V1": "placeholder_rig"}, {"V4_AI": "no_ai"},
                {"V4_PHYS": "no_physics"}, {"V5_SIM": "tick_counter_only"},
                {"ANIM": "walk_missing"}, {"V3_SCENE": "spawn_missing"}):
        r = run(build_slice(degrade=deg))
        assert r["completed"], deg
        assert r["completion_tick"] == 91, deg
