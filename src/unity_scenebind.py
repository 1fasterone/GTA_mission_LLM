"""
Authored-scene binding — welds a Scene Compiler scene file
(src/unity_sceneinput.py) onto the generated schema, in Step 5 of the Unity
pipeline, between path normalization and the layout pass.

Two halves, both deterministic:

  BIND   Each authored group is matched to at most one generated entity by
         keyword overlap (entity name > model_hint > sketchfab_query > kind)
         against the model's own filename. The winner's placement is baked
         from the layout: `instance_positions`, `instance_yaws`, `count` and
         `scale`. src/unity_layout.py then treats that group as PREPLACED —
         packed AROUND, never repacked — exactly as it treats a racetrack's
         start grid.

  INJECT Any authored group that matched nothing becomes its own entity, so a
         model the author placed always reaches the scene even when the LLM
         wrote no character for it.

Nothing here invents a schema field, and nothing overwrites generated
*wiring*: scripts, micros, objectives and controllers are untouched. The one
thing an authored group does take over is WHICH MODEL its entity resolves to,
via `schema.meta["authored_models"]` — read as Tier A, ahead of Sketchfab and
Kenney, by src/unity_entity_asset_resolver.py. Entities the layout does not
cover keep resolving through the normal catalog tiers.

Deliberate abstentions (loud, never silent):
  - 2D / 2d_topdown: EntitySpawner builds its own course at spawn time and
    ignores `instance_positions` entirely, so authored PLACEMENT cannot be
    honored there. The model binding still applies and the caller is told.
  - placement == "path" entities own their geometry (an authored polyline,
    e.g. a racetrack ribbon). They bind for MODEL only; their points stand.
"""

from __future__ import annotations

import logging
from typing import Optional

from .unity_asset_catalog import tokenize
from .unity_schema import EntitySpec, UnitySchema

_log = logging.getLogger(__name__)

# Field weights for group<->entity matching. A name hit is worth more than a
# hint, which is worth more than a marketplace search phrase; `kind` alone is
# deliberately below MIN_BIND_SCORE so "vehicle" never binds a random car
# model to every vehicle group in the schema.
_W_NAME = 3.0
_W_HINT = 2.0
_W_QUERY = 1.0
_W_KIND = 1.0
MIN_BIND_SCORE = 2.0
# Two tokens also match when one contains the other and the shorter is at
# least this long — "bike" vs "monobike", "track" vs "racetrack". Below 4 the
# containment rule starts firing on noise ("car" inside "cargo").
_CONTAIN_MIN = 4

# Words that say nothing about WHICH model this is.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "with", "for", "in", "on", "at",
    "group", "groups", "entity", "entities", "object", "objects", "item",
    "items", "main", "new", "layout", "scene", "model", "models", "prefab",
})

_LAYOUT_PLACEMENTS = {"single", "line", "ring", "grid", "scatter"}


def _terms(text: str) -> set[str]:
    """Tokenize to meaningful lowercase words (+ naive singular forms)."""
    out: set[str] = set()
    for t in tokenize(str(text or "")):
        if len(t) < 2 or t in _STOPWORDS:
            continue
        out.add(t)
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            out.add(t[:-1])
    return out


def _overlap(a: set[str], b: set[str]) -> bool:
    """True when the two term sets share a word, allowing containment for
    tokens of at least _CONTAIN_MIN characters."""
    if a & b:
        return True
    for x in a:
        for y in b:
            if len(x) >= _CONTAIN_MIN and len(y) >= _CONTAIN_MIN \
                    and (x in y or y in x):
                return True
    return False


def bind_score(group: dict, entity: EntitySpec) -> float:
    """How strongly this authored model answers to this entity. Deterministic:
    pure set arithmetic over declared text, no RNG."""
    gterms = _terms(group.get("source_file", "") or group.get("file", ""))
    gterms |= _terms(group.get("name", ""))
    if not gterms:
        return 0.0
    score = 0.0
    if _overlap(gterms, _terms(entity.name)):
        score += _W_NAME
    if _overlap(gterms, _terms(getattr(entity, "model_hint", ""))):
        score += _W_HINT
    if _overlap(gterms, _terms(getattr(entity, "sketchfab_query", ""))):
        score += _W_QUERY
    if _overlap(gterms, _terms(entity.kind)):
        score += _W_KIND
    return score


def _plan(scene: dict, schema: UnitySchema
          ) -> list[tuple[int, Optional[int], list[int]]]:
    """Assign groups to entities. Returns
    [(group_index, primary_entity_index | None, spill_entity_indices)] in
    group order.

    PRIMARY is a greedy one-to-one match, ranked by score, then group order,
    then entity order — a total order with no ties left to chance, so the same
    scene + schema always plans identically.

    SPILL exists because "one group, one entity" is wrong for the commonest
    authored shape there is: several copies of the same machine on a starting
    grid, facing a schema that names each racer separately. Handing all three
    monobikes to `PlayerMonobike` gave it count=3, which silently disqualifies
    it from the generator's player-merge (that path requires count == 1) — the
    player then drives an invisible capsule while three bikes sit on the grid.
    Verified live on the Monaco layout before this existed.

    So a multi-instance group also feeds any OTHER entity that scored for it,
    is still unbound, and is a SINGLETON in the schema (`count == 1`) — a
    named individual, not a crowd. Group entities (count > 1) never take
    spill: a 20-barrel pile is one pile, not 19 plus a loose barrel. The
    primary always keeps at least one instance, and takes the first ones in
    layout order (pole position for a grid).
    """
    groups = scene.get("groups", [])
    entities = list(schema.entities)
    scores: dict[tuple[int, int], float] = {}
    for gi, g in enumerate(groups):
        for ei, e in enumerate(entities):
            s = bind_score(g, e)
            if s >= MIN_BIND_SCORE:
                scores[(gi, ei)] = s
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))

    taken_g: dict[int, int] = {}
    taken_e: set[int] = set()
    for (gi, ei), _s in ranked:
        if gi in taken_g or ei in taken_e:
            continue
        taken_g[gi] = ei
        taken_e.add(ei)

    plan: list[tuple[int, Optional[int], list[int]]] = []
    for gi in range(len(groups)):
        primary = taken_g.get(gi)
        spill: list[int] = []
        room = int(groups[gi].get("count", 1)) - 1
        if primary is not None and room > 0:
            cands = sorted(
                ((s, ei) for (g2, ei), s in scores.items()
                 if g2 == gi and ei not in taken_e
                 and getattr(entities[ei], "count", 1) == 1),
                key=lambda c: (-c[0], c[1]))
            for _s, ei in cands[:room]:
                spill.append(ei)
                taken_e.add(ei)
        plan.append((gi, primary, spill))
    return plan


def _unique_entity_name(base: str, schema: UnitySchema) -> str:
    taken = {e.name for e in schema.entities}
    if base not in taken:
        return base
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


def _kind_for(group: dict) -> str:
    """Kind for an INJECTED entity. Deliberately the schema default "prop" —
    the authored model is the ground truth for what this object looks like
    (Tier A), so guessing a semantic kind here would only mislead the
    playability evaluator and the color/tag defaults."""
    return "prop"


def apply_scene(schema: UnitySchema, scene: dict) -> list[str]:
    """Bind `scene` onto `schema` in place. Returns fixer-style log messages.

    Call AFTER the concept-entity rescue and path normalization (so the entity
    list is final and world coordinates are settled) and BEFORE
    src/unity_layout.py's compute_layout (so it sees the authored groups as
    occupied space).
    """
    groups = scene.get("groups") or []
    if not groups:
        return []

    msgs: list[str] = []
    is_2d = schema.game_type in ("2D", "2d_topdown")
    authored: dict[str, dict] = {}
    n_bound = n_injected = n_model_only = n_skipped = n_spill = 0

    def _place(e: EntitySpec, g: dict, positions: list, yaws: list) -> None:
        """Give `e` this model and (in 3D) this slice of the group's baked
        transforms."""
        authored[e.name] = {
            "src": g["src"],
            "file": g["file"],
            "source_file": g["source_file"],
            "type": g["type"],
            "scale": g["scale"],
            "group": g["name"],
        }
        nonlocal n_model_only
        if is_2d:
            n_model_only += 1
            return
        if e.placement == "path" and e.path_points:
            n_model_only += 1
            msgs.append(
                f"scene: '{e.name}' keeps its authored path geometry — "
                f"{g['file']} bound as its model only")
            return
        e.instance_positions = [list(p) for p in positions]
        e.instance_yaws = list(yaws)
        e.count = len(positions)
        e.scale = g["scale"]
        if e.placement not in _LAYOUT_PLACEMENTS:
            e.placement = "single" if e.count == 1 else "scatter"
        # Groups whose instances all sit off the ground carry that as the
        # group elevation too — the spawner's own fallbacks never see the
        # baked array, and Step 7 reads `elevation` when grading reachability.
        ys = [p[1] for p in positions]
        if ys and min(ys) == max(ys) and ys[0]:
            e.elevation = round(float(ys[0]), 3)

    for gi, ei, spill in _plan(scene, schema):
        g = groups[gi]

        if g.get("type") == "sprite" and not is_2d:
            # A flat PNG in a 3D scene: the resolver's format gate rejects
            # sprites outside 2D games, so there is nothing to bind OR place.
            # Checked before the injection branch — injecting an entity that
            # can never resolve to a model would leave a hole in the scene.
            n_skipped += 1
            msgs.append(
                f"scene: '{g['file']}' is a sprite but this is a 3D project — "
                f"skipped (3D scenes take models only)")
            continue

        if ei is None:
            name = _unique_entity_name(g["name"], schema)
            e = EntitySpec(
                name=name,
                kind=_kind_for(g),
                count=g["count"],
                scale=g["scale"],
                placement="single" if g["count"] == 1 else "scatter",
                # The authored model is the whole point — say so, so that if
                # Tier A is ever unavailable the catalog tiers at least aim at
                # the right object instead of the shared default prop.
                model_hint=" ".join(sorted(_terms(g["source_file"]))[:6]),
            )
            schema.entities.append(e)
            n_injected += 1
        else:
            e = schema.entities[ei]
            n_bound += 1

        # Instance split: the primary keeps the FIRST slots in layout order
        # (pole position), each spill entity takes one of the trailing ones.
        keep = g["count"] - len(spill)
        _place(e, g, g["positions"][:keep], g["yaws"][:keep])
        for n, sei in enumerate(spill):
            se = schema.entities[sei]
            _place(se, g, g["positions"][keep + n:keep + n + 1],
                   g["yaws"][keep + n:keep + n + 1])
            n_spill += 1
        if spill:
            msgs.append(
                f"scene: '{g['file']}' has {g['count']} authored instances and "
                f"{len(spill) + 1} entities answer to it — "
                f"{e.name} keeps {keep}, one each to "
                f"{', '.join(schema.entities[s].name for s in spill)}")

    if is_2d:
        msgs.append(
            f"scene: game_type '{schema.game_type}' builds its own course at "
            f"spawn time — authored POSITIONS cannot be honored; "
            f"{len(groups)} model binding(s) applied without placement")

    meta = schema.meta if isinstance(schema.meta, dict) else {}
    meta["authored_models"] = authored
    meta["authored_scene"] = {
        "layout": scene.get("layout", ""),
        "layout_path": scene.get("layout_path", ""),
        "groups": len(groups),
        "bound": n_bound,
        "injected": n_injected,
        "model_only": n_model_only,
        "skipped": n_skipped,
        "spill": n_spill,
    }
    schema.meta = meta

    msgs.insert(0, (
        f"scene: {len(groups)} authored group(s) from "
        f"'{scene.get('layout') or 'layout'}' — {n_bound} bound to generated "
        f"entities, {n_injected} injected, {n_spill} shared out, "
        f"{sum(g['count'] for g in groups)} instance(s) placed"))
    return msgs


def authored_models(schema: UnitySchema) -> dict[str, dict]:
    """The Tier A table: {entity_name: {src, file, type, scale, ...}}.

    Lives on schema.meta so BOTH generate() and write() see it through the
    same `_resolve_all_models` call — the two must agree or the emitted .meta
    files and the copied binaries desync.
    """
    meta = getattr(schema, "meta", None)
    if not isinstance(meta, dict):
        return {}
    table = meta.get("authored_models")
    return table if isinstance(table, dict) else {}
