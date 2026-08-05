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

Nothing here invents a schema field. What an authored group takes over is
WHICH MODEL its entity resolves to, via `schema.meta["authored_models"]` —
read as Tier A, ahead of Sketchfab and Kenney, by
src/unity_entity_asset_resolver.py — and its placement. Entities the layout
does not cover keep resolving through the normal catalog tiers, and are named
in the returned messages so a silent catalog pick is never a surprise.

ROLE (the one wiring exception). A model filename that opens with a role word
— `player_monobike.fbx`, `enemy_drone.fbx`, `pickup_scrap.fbx`, see
unity_sceneinput.ROLE_PREFIXES — declares what the object IS, not just which
mesh it is. That role binds ahead of every keyword signal, vetoes a bind to a
conflicting role, and becomes the bound entity's `kind`, which is what gets it
the controller / AI / objective wiring instead of scenery treatment. Scripts
and micros are still untouched. A layout whose filenames carry no role word
behaves exactly as it did before roles existed.

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
# A declared ROLE (a `player_`/`enemy_`/`pickup_` filename prefix, see
# unity_sceneinput.ROLE_PREFIXES) outweighs every keyword signal combined: the
# author stated what the model IS, which is not a guess to be outvoted by a
# lucky word overlap somewhere else in the schema.
_W_ROLE = 8.0
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

# Roles that are MUTUALLY EXCLUSIVE gameplay jobs: a model the author labelled
# `enemy_` must never be handed to the player's entity just because their names
# happen to share a word. Kinds outside these families (vehicle, character, and
# every scenery kind) are descriptive rather than exclusive — the player IS a
# vehicle in a racing game — so they never veto a keyword bind.
_ROLE_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"player"}),
    frozenset({"enemy", "boss"}),
    frozenset({"npc"}),
    frozenset({"pickup", "collectible", "item", "goal", "objective"}),
)
# Tags with an established meaning in the generated project (declared by
# TagManager.asset.j2). A role outside this map leaves `tag` alone.
_ROLE_TAGS = {
    "player": "Player", "enemy": "Enemy", "boss": "Enemy",
    "pickup": "Collectible", "collectible": "Collectible", "item": "Collectible",
}


def _family(kind: str) -> Optional[frozenset[str]]:
    k = (kind or "").lower()
    for fam in _ROLE_FAMILIES:
        if k in fam:
            return fam
    return None


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


def _answers_to_role(entity: EntitySpec, role: str) -> bool:
    """Does this entity already hold the job the group declares?

    Three signals, any of which is the schema saying so: an exact `kind`, a
    kind in the same exclusive family, or the TAG the role owns. The tag
    matters most for the avatar — the generator's player-merge selects on
    `tag == "Player"`, not on kind, so an entity the pipeline already tagged
    is the avatar whatever the LLM called it or typed it as.
    """
    kind = (entity.kind or "").lower()
    fam = _family(role)
    if kind == role or (fam is not None and kind in fam):
        return True
    tag = _ROLE_TAGS.get(role)
    return bool(tag) and entity.tag == tag


def bind_score(group: dict, entity: EntitySpec, *, use_role: bool = True) -> float:
    """How strongly this authored model answers to this entity. Deterministic:
    pure set arithmetic over declared text, no RNG.

    A group that declares a ROLE is judged on that first: it scores _W_ROLE
    against an entity already holding that job and is VETOED outright against
    a conflicting one. Keyword overlap still runs — among three enemies the
    best-named one should still win — but it can no longer hand
    `enemy_drone.fbx` to the player.

    `use_role=False` scores keywords only. `_plan` uses it for SPILL, where
    the veto must not apply: a rival taking a spare bike from a `player_`
    group shares the machine, not the job, and vetoing that would break the
    starting grid the spill mechanism exists for.
    """
    role = (group.get("role") or "").lower() if use_role else ""
    if role:
        gfam, efam = _family(role), _family(entity.kind)
        if gfam is not None and efam is not None and gfam is not efam:
            return 0.0

    gterms = _terms(group.get("source_file", "") or group.get("file", ""))
    gterms |= _terms(group.get("name", ""))
    if not gterms and not role:
        return 0.0
    score = 0.0
    if role and _answers_to_role(entity, role):
        score += _W_ROLE
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
    # Spill is scored role-BLIND (see `bind_score`): sharing a machine is not
    # taking a job, so a `player_` grid may still feed the named rivals.
    spill_scores: dict[tuple[int, int], float] = {}
    for gi, g in enumerate(groups):
        for ei, e in enumerate(entities):
            s = bind_score(g, e)
            if s >= MIN_BIND_SCORE:
                scores[(gi, ei)] = s
            s2 = bind_score(g, e, use_role=False)
            if s2 >= MIN_BIND_SCORE:
                spill_scores[(gi, ei)] = s2
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
                ((s, ei) for (g2, ei), s in spill_scores.items()
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
    """Kind for an INJECTED entity.

    A DECLARED role (a `pickup_`/`enemy_` filename prefix) is the author
    stating what this object is, so it becomes the kind — that is the whole
    point of declaring it, and it is what gets the model wired as an objective
    or an opponent instead of set dressing.

    Without one, the default stays "prop": the authored model is ground truth
    for what the object LOOKS like (Tier A), and guessing a semantic kind from
    a mesh would only mislead the playability evaluator and the color/tag
    defaults.
    """
    return (group.get("role") or "").lower() or "prop"


def _apply_role(entity: EntitySpec, role: str) -> Optional[str]:
    """Stamp a declared role onto the entity that answers to it. Returns a log
    line when something actually changed.

    This is the one place an authored scene reaches past models and transforms
    into WIRING — deliberately, and only when the author asked for it by
    naming the file. The Pipeline LLM's `kind` is a guess from prose; a role
    prefix is a statement, so it wins, exactly as the authored model wins over
    the catalog pick.

    Applied to the PRIMARY bind only. Spill entities (the rivals a starting
    grid feeds, see `_plan`) keep their own kind — they share the machine, not
    the job.
    """
    was = (entity.kind or "").lower()
    entity.kind = role
    # The tag is set even when the kind already agreed: an entity the LLM
    # typed `enemy` but left Untagged is still invisible to everything that
    # finds its targets by tag.
    tag = _ROLE_TAGS.get(role, "")
    tagged = bool(tag) and entity.tag in ("", "Untagged")
    if tagged:
        entity.tag = tag
    if was == role and not tagged:
        return None
    parts = []
    if was != role:
        parts.append(f"kind {was or 'prop'!r} -> {role!r}")
    if tagged:
        parts.append(f"tag -> {entity.tag!r}")
    return (f"scene: '{entity.name}' declared role '{role}' by filename — "
            + ", ".join(parts))


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
            _kind = _kind_for(g)
            e = EntitySpec(
                name=name,
                kind=_kind,
                # A declared role brings its tag: the generator's player-merge
                # selects on `tag == "Player"`, so an authored `player_` model
                # that matched no existing entity still becomes the avatar
                # rather than a second, inert body beside it.
                tag=_ROLE_TAGS.get((g.get("role") or "").lower(), "Untagged"),
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

        role = (g.get("role") or "").lower()
        if role and ei is not None:
            changed = _apply_role(e, role)
            if changed:
                msgs.append(changed)

        # Instance split: the primary keeps the FIRST slots in layout order
        # (pole position), each spill entity takes one of the trailing ones.
        keep = g["count"] - len(spill)
        # The avatar must stay a singleton: the generator's player-merge only
        # fires at count == 1, and a player holding a whole starting grid drives
        # an invisible capsule while the bikes sit there (the failure `_plan`'s
        # spill was built for). A declared `player_` role makes that collision
        # likely rather than accidental, so the surplus is re-homed instead —
        # never dropped, because a model the author placed always reaches the
        # scene.
        player_extra: tuple[list, list] = ([], [])
        if role == "player" and keep > 1:
            player_extra = (g["positions"][1:keep], g["yaws"][1:keep])
            msgs.append(
                f"scene: '{g['file']}' declares role 'player' but places "
                f"{keep} of them — '{e.name}' keeps pole position and the "
                f"other {keep - 1} become scenery. Rename those copies "
                f"`enemy_*`/`rival_*` (or name the rivals in your brief) to "
                f"have them wired as opponents.")
            keep = 1

        _place(e, g, g["positions"][:keep], g["yaws"][:keep])
        # Spill indices are counted from the UNCLAMPED split, so a player clamp
        # above cannot shift a rival onto the pole slot.
        spill_base = g["count"] - len(spill)
        for n, sei in enumerate(spill):
            se = schema.entities[sei]
            _place(se, g, g["positions"][spill_base + n:spill_base + n + 1],
                   g["yaws"][spill_base + n:spill_base + n + 1])
            n_spill += 1

        if player_extra[0]:
            extra = EntitySpec(
                name=_unique_entity_name(f"{g['name']}Extra", schema),
                kind="prop", count=len(player_extra[0]),
                scale=g["scale"], placement="scatter",
                model_hint=" ".join(sorted(_terms(g["source_file"]))[:6]),
            )
            schema.entities.append(extra)
            n_injected += 1
            _place(extra, g, player_extra[0], player_extra[1])
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

    # Everything the layout did NOT cover keeps resolving through the catalog
    # tiers (Sketchfab, then Kenney) — by design, but silently, which is why
    # "the pipeline output globs of models I did not choose" reads as a bug
    # rather than as a list of entities to add to the layout. Name them.
    uncovered = [e for e in schema.entities if e.name not in authored]
    if uncovered:
        msgs.append(
            f"scene: {len(uncovered)} entity(s) are NOT in your layout and will "
            f"resolve from the asset catalog — "
            + ", ".join(f"{e.name}({e.kind}"
                        + (f" x{e.count}" if e.count > 1 else "") + ")"
                        for e in uncovered[:12])
            + (f", +{len(uncovered) - 12} more" if len(uncovered) > 12 else "")
            + ". Place a model for each in the Scene Compiler to author it "
              "instead.")

    meta = schema.meta if isinstance(schema.meta, dict) else {}
    meta["authored_models"] = authored
    meta["uncovered_entities"] = [
        {"name": e.name, "kind": e.kind, "count": e.count} for e in uncovered]
    meta["authored_scene"] = {
        "layout": scene.get("layout", ""),
        "layout_path": scene.get("layout_path", ""),
        "groups": len(groups),
        "bound": n_bound,
        "injected": n_injected,
        "model_only": n_model_only,
        "skipped": n_skipped,
        "spill": n_spill,
        "uncovered": len(uncovered),
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
