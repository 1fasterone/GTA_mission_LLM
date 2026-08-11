"""
Playability, graded on the ARTIFACT — the files the generator actually emitted.

`src/unity_playability.py` grades the schema. That is necessary and not
sufficient: the schema is the compiler's intent, and the defect that finally
motivated this module lives entirely in the gap between intent and output.

The DropZone arcade capture, replayed through Steps 4-6:

  * `compute_layout` decides *"Player group 'Player' spawns at the top of the
    corridor (y=2000, attached to 'TurbopropExit')"* — a 2000 m descent.
  * `unity_generator`'s player-merge is the ONLY writer of `player_spawn`, and
    it requires `tag=="Player" and count==1 and kind in _PLAYER_MERGE_KINDS`.
    The capture's player group is `prop / Untagged / count=6` (its Player tag
    was demoted by `EntitySpec._reserve_player_tag`), so the merge is skipped.
  * The emitted scene therefore spawns the avatar at the template default,
    `m_LocalPosition: {x: 0, y: 1, z: 0}` — the world origin, 2000 m below the
    game — while six lookalike instances of the player group spawn at the exit
    altitude as scenery.

No schema field is wrong in that build. The layout's decision is right, the
entity list is right; the decision simply never reaches the scene. Only the
emitted `.unity` says so, which is what this module reads.

Deterministic, read-only, and offline: it takes the `{relative_path: content}`
dict `UnityProjectGenerator.generate()` already returns, so it needs no disk,
no Unity, no network. Findings use the same `Finding` shape as the schema
evaluator and are merged into one scorecard by `evaluate_project()`.

Boundary — three different questions, three different graders:
  * `tests/test_outputs_smoke.py`  — will the project open?
  * `unity_cli.py verify`          — does the C# compile?
  * this module                    — is the player in the game?
"""

from __future__ import annotations

import math
import re
from typing import Mapping, Optional

from .unity_layout_archetype import pick_player_entity
from .unity_playability import (ERROR, WARN, Finding, PlayabilityReport,
                                _is_objective, _objective_names, _ungraded,
                                evaluate)
from .unity_schema import UnitySchema

# How far the emitted avatar may sit from the spawn the layout baked before it
# counts as a different place. The merge writes y = baked_y + 1.0 (the capsule
# pivot is its centre, feet 1 m below), so the comparison is against the lifted
# point and this is pure slack for rounding.
_SPAWN_TOLERANCE_M = 2.0

# Unity YAML: `--- !u!<classID> &<fileID>` opens each object document.
_DOC_RE = re.compile(r"^--- !u!(\d+) &(\d+)", re.M)
_CLASS_GAMEOBJECT = 1
_CLASS_TRANSFORM = 4
_COMPONENT_RE = re.compile(r"^\s*-\s*component:\s*\{fileID:\s*(\d+)\}", re.M)
_VEC3_RE = re.compile(
    r"m_LocalPosition:\s*\{x:\s*(-?[\d.eE+]+),\s*y:\s*(-?[\d.eE+]+),"
    r"\s*z:\s*(-?[\d.eE+]+)\}")
# The bootstrap's per-group spawn calls, 3D and both 2D shapes.
_SPAWN_CALL_RE = re.compile(
    r"\b(?:SpawnGroup|SpawnTiledGroup|PlaceRow2D|PlaceGroup2DTopDown)"
    r"\(\s*\"([^\"]+)\"")


# ── emitted-scene reading ─────────────────────────────────────────────────────

def _documents(scene: str) -> dict[int, tuple[int, str]]:
    """fileID -> (classID, document body). One pass, no YAML dependency: the
    generated scenes are template output, so their shape is known exactly."""
    docs: dict[int, tuple[int, str]] = {}
    marks = list(_DOC_RE.finditer(scene))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(scene)
        docs[int(m.group(2))] = (int(m.group(1)), scene[m.end():end])
    return docs


def _player_gameobjects(scene: str) -> list[tuple[int, str]]:
    """Every GameObject document in the scene carrying tag Player."""
    return [(fid, body) for fid, (cls, body) in _documents(scene).items()
            if cls == _CLASS_GAMEOBJECT and "m_TagString: Player" in body]


def _transform_position(scene: str, go_body: str) -> Optional[tuple[float, ...]]:
    """The world position of a GameObject, read off its Transform component."""
    docs = _documents(scene)
    for cid in (int(c) for c in _COMPONENT_RE.findall(go_body)):
        cls, body = docs.get(cid, (0, ""))
        if cls != _CLASS_TRANSFORM:
            continue
        hit = _VEC3_RE.search(body)
        if hit:
            return tuple(float(g) for g in hit.groups())
    return None


def _scenes(files: Mapping[str, str]) -> dict[str, str]:
    return {p: c for p, c in files.items() if p.endswith(".unity")}


def _primary_scene(schema: UnitySchema,
                   files: Mapping[str, str]) -> tuple[str, str] | tuple[None, None]:
    """The scene the game starts in — the first entry of schema.scenes when it
    is present on disk, else the only/first emitted scene."""
    scenes = _scenes(files)
    if not scenes:
        return None, None
    wanted = next(iter(schema.scenes or ()), "")
    if wanted:
        for path, content in sorted(scenes.items()):
            if path.rsplit("/", 1)[-1] == f"{wanted}.unity":
                return path, content
    path = sorted(scenes)[0]
    return path, scenes[path]


def _spawned_groups(files: Mapping[str, str]) -> Optional[set[str]]:
    """Group names the emitted bootstrap actually spawns. None when no spawner
    was emitted at all (nothing to conclude from)."""
    bootstraps = [c for p, c in files.items()
                  if p.endswith(("EntitySpawner.cs", "SceneSetup.cs"))]
    if not bootstraps:
        return None
    names: set[str] = set()
    for src in bootstraps:
        names.update(_SPAWN_CALL_RE.findall(src))
    return names


# ── checks ────────────────────────────────────────────────────────────────────

def _check_avatar_singleton(scene_name: str, scene: str,
                            rep: PlayabilityReport) -> None:
    """`GameObject.FindWithTag("Player")` returns one arbitrary match, and the
    camera rig, the weapon mount and every AI target read it."""
    objs = _player_gameobjects(scene)
    if not objs:
        rep.findings.append(Finding(
            "avatar_singleton", ERROR,
            f"{scene_name} contains no Player-tagged GameObject — every "
            "FindWithTag(\"Player\") lookup in the build returns null",
            "templates/unity/scene.unity.j2"))
        rep.checks["avatar_singleton"] = False
        return
    if len(objs) > 1:
        rep.findings.append(Finding(
            "avatar_singleton", ERROR,
            f"{scene_name} contains {len(objs)} Player-tagged GameObjects — "
            "player lookups bind to one of them arbitrarily",
            "templates/unity/scene.unity.j2"))
    rep.checks["avatar_singleton"] = len(objs) == 1


def _check_avatar_spawn(schema: UnitySchema, scene_name: str, scene: str,
                        rep: PlayabilityReport) -> None:
    """The layout pass decides where the player starts. This asserts the scene
    agrees — the check the DropZone build needed, where the layout said y=2000
    and the scene said y=1."""
    group, signal = pick_player_entity(schema.entities)
    if group is None:
        _ungraded(rep, "avatar_spawn",
                  "no entity reads as the player's group, so the layout baked "
                  "no spawn to compare the scene against")
        return
    baked = group.instance_positions or []
    if not baked:
        _ungraded(rep, "avatar_spawn",
                  f"the player group {group.name!r} carries no baked position "
                  "— compute_layout has not run or skipped it")
        return
    objs = _player_gameobjects(scene)
    if not objs:
        _ungraded(rep, "avatar_spawn",
                  f"{scene_name} has no Player-tagged GameObject to locate — "
                  "see the `avatar_singleton` finding")
        return
    emitted = _transform_position(scene, objs[0][1])
    if emitted is None:
        _ungraded(rep, "avatar_spawn",
                  f"the Player GameObject in {scene_name} has no Transform "
                  "with an m_LocalPosition")
        return
    bx, by, bz = baked[0][0], baked[0][1], baked[0][2]
    # The scene lifts the capsule to stand ON the baked point.
    want = (bx, by + 1.0, bz)
    drift = math.dist(emitted, want)
    if drift > _SPAWN_TOLERANCE_M:
        rep.findings.append(Finding(
            "avatar_spawn", ERROR,
            f"the avatar spawns at (x={emitted[0]:.1f}, y={emitted[1]:.1f}, "
            f"z={emitted[2]:.1f}) but the layout put the player group "
            f"{group.name!r} ({signal}) at (x={bx:.1f}, y={by:.1f}, z={bz:.1f})"
            f" — {drift:.0f} m away, so the player does not start where the "
            "game was built for them",
            "unity_generator player_spawn (only written by the player-merge)"))
    rep.checks["avatar_spawn"] = drift <= _SPAWN_TOLERANCE_M


def _check_avatar_not_duplicated(schema: UnitySchema,
                                 files: Mapping[str, str],
                                 rep: PlayabilityReport) -> None:
    """When the merge consumes the player group, the bootstrap stops spawning
    it. When the merge is skipped, the group spawns anyway — so the scene holds
    a controllable capsule AND `count` inert lookalikes of the avatar standing
    somewhere else, which is what the DropZone build shipped."""
    group, _signal = pick_player_entity(schema.entities)
    spawned = _spawned_groups(files)
    if group is None or spawned is None:
        _ungraded(rep, "avatar_not_duplicated",
                  "no player group resolved, or the build emitted no entity "
                  "bootstrap to read")
        return
    if group.name in spawned:
        rep.findings.append(Finding(
            "avatar_not_duplicated", WARN,
            f"the bootstrap still spawns the player group {group.name!r} "
            f"({group.count} instance(s)) alongside the scene's own avatar — "
            "the player-merge did not consume it, so the design's avatar model "
            "stands in the world as scenery while the player drives a capsule",
            "unity_generator player-merge gate (tag/count/kind)"))
    rep.checks["avatar_not_duplicated"] = group.name not in spawned


def _check_objectives_spawned(schema: UnitySchema, files: Mapping[str, str],
                              rep: PlayabilityReport) -> None:
    """An objective in the schema that the bootstrap never spawns is content the
    player is asked to reach and cannot see."""
    spawned = _spawned_groups(files)
    if spawned is None:
        _ungraded(rep, "objectives_spawned",
                  "the build emitted no entity bootstrap to read")
        return
    refs = frozenset(_objective_names(schema))
    player, _sig = pick_player_entity(schema.entities)
    player_name = player.name if player is not None else None
    missing = [e.name for e in schema.entities
               if _is_objective(e, refs) and e.name != player_name
               and e.name not in spawned]
    if missing:
        rep.findings.append(Finding(
            "objectives_spawned", ERROR,
            f"{len(missing)} objective group(s) are in the schema but never "
            f"spawned by the build ({', '.join(sorted(missing)[:6])}) — the "
            "goal names content that does not exist at runtime",
            "templates/unity/EntitySpawner.cs.j2 / entity_model_paths skip:"))
    rep.checks["objectives_spawned"] = not missing


# ── entry points ──────────────────────────────────────────────────────────────

def grade_artifact(schema: UnitySchema, files: Mapping[str, str],
                   rep: PlayabilityReport) -> PlayabilityReport:
    """Add the artifact findings to an existing schema report, in place."""
    scene_path, scene = _primary_scene(schema, files)
    if scene is None or scene_path is None:
        for name in ("avatar_singleton", "avatar_spawn"):
            _ungraded(rep, name, "the build emitted no .unity scene")
    else:
        scene_name = scene_path.rsplit("/", 1)[-1]
        _check_avatar_singleton(scene_name, scene, rep)
        _check_avatar_spawn(schema, scene_name, scene, rep)
    _check_avatar_not_duplicated(schema, files, rep)
    _check_objectives_spawned(schema, files, rep)
    return rep


def evaluate_project(schema: UnitySchema,
                     files: Mapping[str, str]) -> PlayabilityReport:
    """Grade the schema AND the files it produced, as one scorecard."""
    return grade_artifact(schema, files, evaluate(schema))
