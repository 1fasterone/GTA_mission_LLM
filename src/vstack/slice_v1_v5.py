"""
The minimal V1 → V5 vertical slice for E2E_0001 "SpawnWalkComplete".

`build_slice()` compiles the static identity chain (V1 rig → V2 deform → Retarget
→ V3 entity/scene/mission → V4 physics/nav/ai → V5 sim spec → build_hash).
`run()` executes the deterministic tick-barrier simulation and emits a
worldstate_hash@N at every tick. Both are pure functions of their inputs.

Canonical run (non-degraded):
  * 30 Hz tick; walk 1.500 u/s → 0.050 u/tick (= 50 milliunits, exact).
  * spawn (0,0,0) → target centre (0,0,5.000), radius 0.500 → enter at z = 4.500.
  * tick 0 spawn/idle; tick 1 mission begins + Idle→WalkTo (transition, no move);
    ticks 2..91 advance +50 mu; enter target & complete at tick N = 91.

See docs/specs/E2E_0001_SpawnWalkComplete.md and docs/specs/V1..V5.
"""
from __future__ import annotations

import copy

from src.vstack.canon import Q_ROT, Q_TIME, chash, q

# Static identity-chain keys, in canonical order (used for stability checks).
IDENTITY_KEYS = [
    "asset_hash", "rig_hash", "deform_hash", "anim_hash", "behavior_hash",
    "entity_hash", "scene_hash", "mission_hash", "physics_hash", "nav_hash",
    "ai_hash", "gameplay_hash", "sim_hash", "build_hash",
]


# ══════════════════════════════════════════════════════════════════════════════
# BUILD — the static identity chain (V1 → V5 spec)
# ══════════════════════════════════════════════════════════════════════════════

def build_slice(seed: int = 0x00000001, degrade: dict | None = None) -> dict:
    """Compile the minimal slice; return all chain hashes + runtime constants.

    `degrade` injects degrade-never-fail fallbacks (§7) keyed by layer, e.g.
    {"V2": "synthetic_capsule"}. A degraded build is still valid and deterministic
    but yields a DISTINCT golden bundle (degrade flags fold into the hashes).
    """
    degrade = dict(degrade or {})

    # ── V1: asset + rig ───────────────────────────────────────────────────────
    asset = {"kind": "mesh", "verts_q": q(1.0), "bounds_q": [q(0.5), q(1.8), q(0.5)]}
    asset_hash = chash("v1.asset", asset)

    rig = {
        "class": ["humanoid", 1],
        "joints": ["root", "pelvis", "spine", "head",
                   "thigh_l", "calf_l", "foot_l",
                   "thigh_r", "calf_r", "foot_r"],
        "hierarchy": "single_root_acyclic",
        "bind_pose_q": [0, 0, 0],
        "sockets": ["root", "pelvis", "foot_l", "foot_r"],
        "asset_ref": asset_hash,
    }
    if degrade.get("V1") == "placeholder_rig":
        rig["class"] = ["biped_placeholder", 1]
        rig["flagged"] = True
    rig_hash = chash("v1.rig", rig)

    # ── V2: deformation stack (scaling → twist → correctives → IK → proxies) ───
    proxy = "body_capsule"
    synthetic = False
    if degrade.get("V2") == "synthetic_capsule":
        proxy, synthetic = "synthetic_capsule_from_bounds", True
    deform = {
        "stack_order": ["scaling", "twist", "correctives", "ik", "proxies"],
        "scaling": "identity",
        "twist": [],
        "correctives": [],
        "ik_chains": ["leg_l", "leg_r"],
        "proxies": [{"name": proxy, "synthetic": synthetic, "shape": "capsule",
                     "radius_q": q(0.3), "height_q": q(1.8)}],
        "rig_ref": rig_hash,
        "asset_ref": asset_hash,
    }
    deform_hash = chash("v2.deform", deform)

    # ── Retarget: clip set + foot-plant event tracks ──────────────────────────
    anim = {
        "clips": [
            {"role": "idle", "name": "Idle", "events": []},
            {"role": "walk", "name": "Walk_Forward",
             "events": ["foot_plant_l", "foot_plant_r"]},
        ],
        "rig_ref": rig_hash,
    }
    if degrade.get("ANIM") == "walk_missing":
        anim["clips"][1] = {"role": "walk", "name": "Idle", "events": [],
                            "degraded": "pose_hold"}
    anim_hash = chash("retarget.anim", anim)

    substrate_fp = {"rig": rig_hash, "deform": deform_hash,
                    "anim": anim_hash, "asset": asset_hash}

    # ── V3-P1: behavior closure (Idle → WalkTo) ───────────────────────────────
    behavior = {
        "closure": ["loco.biped_walk", "ai.bt_root"],
        "fsm": [["Idle", "WalkTo", "mission.O1==active"]],
        "anim_slots": ["idle", "walk"],
        "substrate_fp": substrate_fp,
    }
    behavior_hash = chash("v3.behavior", behavior)

    # ── V3-P2: entity manifest ────────────────────────────────────────────────
    entity = {
        "class": ["humanoid", 1],
        "substrate_fp": substrate_fp,
        "behaviors": [behavior_hash],
        "anim_map": {"idle": "Idle", "walk": "Walk_Forward"},
        "ik_bindings": ["leg_l", "leg_r"],
        "collision": [proxy],
        "sockets": ["root", "pelvis", "foot_l", "foot_r"],
        "interface": {"events": ["on_reach"], "state": ["ai_state"]},
    }
    entity_hash = chash("v3.entity", entity)

    # ── V3-P3: scene (1 cell, 1 spawn, 1 target volume) ───────────────────────
    spawn = {"id": "P_spawn", "pos_q": [0, 0, 0]}
    target = {"id": "T_target", "center_q": [0, 0, q(5.0)], "radius_q": q(0.5)}
    scene = {
        "partition": {"type": "grid", "cell_q": q(64.0), "origin_q": [0, 0, 0]},
        "instances": [{"entity": entity_hash, "instance_id": "guard_0",
                       "transform_q": [0, 0, 0]}],
        "spawn_points": [spawn],
        "objective_volumes": [target],
        "nav": {"surface": "flat", "tiles": 1},
    }
    if degrade.get("V3_SCENE") == "spawn_missing":
        scene["spawn_points"] = []
        scene["spawn_fallback"] = "origin"
    scene_hash = chash("v3.scene", scene)

    # ── V3-P4: mission (one obj.reach) ────────────────────────────────────────
    mission = {
        "nodes": [{"id": "O1", "type": "obj.reach", "target": "T_target",
                   "bind": [entity_hash, "on_reach"]}],
        "edges": [["start", "O1", "always"], ["O1", "complete", "success"]],
        "terminals": ["complete", "fail"],
        "state_slots": [{"name": "O1", "default": "inactive"}],
        "scene_refs": [scene_hash],
        "entity_refs": [entity_hash],
    }
    mission_hash = chash("v3.mission", mission)

    # ── V4: physics / nav / ai / gameplay ─────────────────────────────────────
    physics = {
        "body": "phys.character_controller",
        "proxy_ref": proxy,
        "kinematic": True,
        "gravity_q": q(9.81),
        "step_limit_q": q(0.3),
        "solver": {"dt_q": 33, "iterations": 4},   # dt = 1/30 s ≈ 0.033 s (quantized)
        "gp_fp": substrate_fp,
    }
    if degrade.get("V4_PHYS") == "no_physics":
        physics["body"] = "kinematic_move_only"
        physics["degraded"] = True
    physics_hash = chash("v4.physics", physics)

    nav = {"tiles": 1, "surface": "flat",
           "agent": {"radius_q": q(0.3), "height_q": q(1.8)},
           "physics_world": physics_hash}
    nav_hash = chash("v4.nav", nav)

    ai = {
        "paradigm": "state_machine",
        "states": ["Idle", "WalkTo", "Arrived"],
        "transitions": [["Idle", "WalkTo", "mission.O1==active"]],
        "action": {"state": "WalkTo", "act": "move_to", "target": "T_target"},
        "tick_hz": 30,
        "realizes": behavior_hash,
        "entity": entity_hash,
    }
    if degrade.get("V4_AI") == "no_ai":
        ai = {"paradigm": "scripted_linear",
              "action": "linear_walk_to(T_target)", "degraded": True,
              "realizes": behavior_hash, "entity": entity_hash}
    ai_hash = chash("v4.ai", ai)

    gameplay_hash = chash("v4.gameplay", {
        "physics": physics_hash, "nav": nav_hash, "ai": ai_hash,
        "entity": entity_hash, "scene": scene_hash, "mission": mission_hash,
    })

    # ── V5: simulation spec ───────────────────────────────────────────────────
    sim_spec = {
        "clock": {"hz": 30, "calendar": ["hour", "day"]},
        "tick_barrier": "double_buffer_read_Nminus1_write_N",
        "systems": [],   # no factions/ecology/economy/weather in the minimal slice
        "lod": "agent",
        "gp_fp": gameplay_hash,
    }
    if degrade.get("V5_SIM") == "tick_counter_only":
        sim_spec["tick_barrier"] = "fixed_tick_counter"
        sim_spec["degraded"] = True
    sim_hash = chash("v5.sim", sim_spec)

    # ── build hash (composes the chain + seed + degrade flags) ────────────────
    degrade_flags = sorted(f"{k}={v}" for k, v in degrade.items())
    build_hash = chash("build", {
        "rig": rig_hash, "deform": deform_hash, "anim": anim_hash,
        "behavior": behavior_hash, "entity": entity_hash, "scene": scene_hash,
        "mission": mission_hash, "gameplay": gameplay_hash, "sim": sim_hash,
        "seed": seed, "degrade_flags": degrade_flags,
    })

    # ── runtime constants (quantized, integer milliunits) ─────────────────────
    speed_q = q(1.5)                  # 1.500 u/s → 1500 mu/s
    move_step = speed_q // 30         # 50 mu/tick = 0.050 u   (exact integer)
    enter_z = target["center_q"][2] - target["radius_q"]   # 5000 − 500 = 4500 mu

    return {
        "seed": seed, "degrade": degrade, "degrade_flags": degrade_flags,
        "asset_hash": asset_hash, "rig_hash": rig_hash, "deform_hash": deform_hash,
        "anim_hash": anim_hash, "behavior_hash": behavior_hash,
        "entity_hash": entity_hash, "scene_hash": scene_hash,
        "mission_hash": mission_hash, "physics_hash": physics_hash,
        "nav_hash": nav_hash, "ai_hash": ai_hash, "gameplay_hash": gameplay_hash,
        "sim_hash": sim_hash, "build_hash": build_hash,
        "move_step": move_step, "enter_z": enter_z, "tick_budget": 182,
        "target": target, "entity_id": "guard_0",
    }


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME — deterministic tick-barrier simulation (V5)
# ══════════════════════════════════════════════════════════════════════════════

def initial_snapshot(sl: dict) -> dict:
    """worldstate @ tick 0: entity spawned at P_spawn, Idle, mission inactive."""
    return {
        "tick": 0,
        "pos_q": [0, 0, 0],
        "rot_q": [0, 0, 0, q(1.0, Q_ROT)],   # identity quaternion (w=1), facing +Z
        "ai_state": "Idle",
        "controller": "grounded",
        "mission": {"O1": "inactive"},
        "mission_state": "running",
    }


def worldstate_hash(sl: dict, snap: dict) -> str:
    """V5 runtime snapshot hash (worldstate_value_hash@N).

    Binds the build + degrade flags + tick + dynamic state, so it is distinct per
    tick (the tick counter guarantees progression even if state were static).
    """
    return chash("v5.worldstate_value", {
        "build": sl["build_hash"],
        "degrade_flags": sl["degrade_flags"],
        "tick": snap["tick"],
        "pos_q": snap["pos_q"],
        "rot_q": snap["rot_q"],
        "ai_state": snap["ai_state"],
        "controller": snap["controller"],
        "mission": snap["mission"],
    })


def step(sl: dict, prev: dict):
    """Tick-barrier: read prev (N-1), write a fresh next (N). Pure + deterministic.

    Returns (next_snapshot, behavior_transition_or_None).
    """
    s = copy.deepcopy(prev)              # read N-1 (immutable), produce N
    s["tick"] = prev["tick"] + 1
    n = s["tick"]
    transition = None

    # 1. mission begins at tick 1
    if n == 1 and s["mission"]["O1"] == "inactive":
        s["mission"]["O1"] = "active"

    # 2. AI FSM: Idle→WalkTo on mission active (transition tick, no move);
    #    else advance while WalkTo (kinematic physics integration).
    if s["ai_state"] == "Idle" and s["mission"]["O1"] == "active":
        s["ai_state"] = "WalkTo"
        transition = {"from": "Idle", "to": "WalkTo", "tick": n}
    elif s["ai_state"] == "WalkTo" and s["mission"]["O1"] != "complete":
        s["pos_q"] = [s["pos_q"][0], s["pos_q"][1], s["pos_q"][2] + sl["move_step"]]

    # 3. reach check → objective/mission completion (publishes on_reach)
    if s["ai_state"] == "WalkTo" and s["pos_q"][2] >= sl["enter_z"]:
        s["mission"]["O1"] = "complete"
        s["mission_state"] = "complete"
        s["ai_state"] = "Arrived"
        transition = transition or {"from": "WalkTo", "to": "Arrived",
                                    "tick": n, "event": "on_reach"}

    return s, transition


def run(sl: dict) -> dict:
    """Run the slice to mission completion (or tick budget). Pure + deterministic."""
    trace = []
    snap = initial_snapshot(sl)
    prev_mission = dict(snap["mission"])
    trace.append(_entry(sl, snap, None, None))

    while snap["mission"]["O1"] != "complete" and snap["tick"] < sl["tick_budget"]:
        snap, transition = step(sl, snap)
        mission_event = None
        if snap["mission"]["O1"] != prev_mission.get("O1"):
            mission_event = f'O1:{prev_mission.get("O1")}->{snap["mission"]["O1"]}'
        prev_mission = dict(snap["mission"])
        trace.append(_entry(sl, snap, transition, mission_event))

    completed = snap["mission"]["O1"] == "complete"
    seq = [e["worldstate_hash"] for e in trace]
    return {
        "trace": trace,
        "completed": completed,
        "completion_tick": snap["tick"] if completed else None,
        "final_pos_q": snap["pos_q"],
        "worldstate_seq": seq,
        "worldstate_seq_checksum": chash("v5.worldstate_seq", seq),
        "final_worldstate_hash": seq[-1],
        "worldstate_hash_0": seq[0],
    }


def _entry(sl, snap, transition, mission_event) -> dict:
    return {
        "tick": snap["tick"],
        "pos_q": list(snap["pos_q"]),
        "ai_state": snap["ai_state"],
        "mission": dict(snap["mission"]),
        "worldstate_hash": worldstate_hash(sl, snap),
        "behavior_transition": transition,
        "mission_event": mission_event,
    }
