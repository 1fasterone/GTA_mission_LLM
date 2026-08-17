"""
Brief-declared mission rescue — the progression the schema-fill collapsed.

`inject_missing_concept_entities` restores the cast. This is its sibling for the
PROGRESSION, and it exists because the structured-brief format let an author
declare scenes, entities, scripts, narrative and audio — and not the thing the
player actually does. Three consecutive live runs of a brief laying out three
ordered phases each produced one mission, so `steps_from_missions`,
`board_domain_for_missions` and SEAM 9 — all of which read missions plural —
could never be reached from a brief at all.

Offline and deterministic — no LLM, no Unity, no network.
"""

from __future__ import annotations

from src.unity_concept_missions import (extract_declared_missions,
                                        inject_missing_concept_missions)
from src.unity_schema import (EntitySpec, MissionObjective, MissionSpec,
                              UnitySchema)

BRIEF = """\
TITLE: Platform Four Lost Property

ENTITIES
- LostUmbrellas: five wet umbrellas (kind: pickup)

MISSIONS
1. Clear the Counter — every lost thing goes on the shelf that matches it.
   - Shelve the umbrellas on the umbrella shelf (collect: LostUmbrellas x5)
   - Shelve the suitcases on the suitcase shelf (collect: LostSuitcases x3)
2. Book the Night In — write down what you shelved.
   - Record each kind in the claim log (interact: ClaimLog)
3. Close the Office — stamp the log and go home.
   - Stamp the night's claim log

AUDIO
- office_hum   ambient  looping  a quiet back office
"""


def _schema(**kw) -> UnitySchema:
    base = dict(
        project_name="LostProperty", game_type="isometric", platform="pc",
        render_pipeline="URP", locomotion="none", description="Sort and log.",
        entities=[EntitySpec(name="LostUmbrellas", kind="pickup", count=5)],
    )
    base.update(kw)
    s = UnitySchema(**base)
    s.meta = {**(s.meta or {}), "user_prompt": kw.pop("brief", BRIEF)}
    return s


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_every_declared_mission_is_read_in_order():
    ms = extract_declared_missions(BRIEF)
    assert [m["title"] for m in ms] == ["Clear the Counter", "Book the Night In",
                                        "Close the Office"]
    assert ms[0]["description"].startswith("every lost thing goes on the shelf")


def test_the_annotation_carries_type_target_and_count():
    first = extract_declared_missions(BRIEF)[0]["objectives"][0]
    assert first["type"] == "collect"
    assert first["target_ref"] == "LostUmbrellas"
    assert first["count"] == 5
    assert first["description"] == "Shelve the umbrellas on the umbrella shelf."


def test_an_unannotated_objective_is_left_for_the_target_resolver():
    """The brief says what to do; the compiler works out what it is about."""
    last = extract_declared_missions(BRIEF)[2]["objectives"][0]
    assert last["type"] == "reach"
    assert last["target_ref"] == ""
    assert last["description"] == "Stamp the night's claim log."


def test_the_section_ends_at_the_next_header():
    """Without this the AUDIO rows below would be read as objectives of the
    last mission."""
    ms = extract_declared_missions(BRIEF)
    assert [len(m["objectives"]) for m in ms] == [2, 1, 1]
    assert not any("office_hum" in o["description"]
                   for m in ms for o in m["objectives"])


def test_a_wrapped_bullet_keeps_its_annotation():
    """Briefs arrive hard-wrapped at ~80 columns. Unfolded, a target_ref that
    wrapped onto the next line vanishes while the file still looks right."""
    wrapped = (
        "MISSIONS\n"
        "1. Clear the Counter — sort it all.\n"
        "   - Shelve every last one of the umbrellas onto the tall narrow\n"
        "     umbrella shelf behind the counter (collect: LostUmbrellas x5)\n")
    o = extract_declared_missions(wrapped)[0]["objectives"][0]
    assert o["target_ref"] == "LostUmbrellas" and o["count"] == 5


def test_a_word_outside_the_ir_vocabulary_is_not_silently_clamped():
    """`(sort: X)` is not a MissionObjType. Treating it as an annotation would
    clamp it to 'reach' and eat the author's sentence with it."""
    o = extract_declared_missions(
        "MISSIONS\n1. Go — do it.\n   - Sort the pile (sort: LostUmbrellas)\n"
    )[0]["objectives"][0]
    assert o["target_ref"] == ""
    assert "(sort: LostUmbrellas)" in o["description"]


def test_a_mid_sentence_parenthetical_is_prose():
    o = extract_declared_missions(
        "MISSIONS\n1. Go — do it.\n   - Shelve the umbrellas (all five) neatly\n"
    )[0]["objectives"][0]
    assert o["description"] == "Shelve the umbrellas (all five) neatly."


def test_a_mission_with_no_objectives_is_not_restored():
    """The schema drops one anyway — the objectives are the whole point."""
    ms = extract_declared_missions("MISSIONS\n1. Clear the Counter — nothing.\n")
    assert ms == []


def test_no_missions_section_is_the_ordinary_case():
    assert extract_declared_missions("TITLE: X\n\nENTITIES\n- A: a thing\n") == []


# ── Injection ────────────────────────────────────────────────────────────────

def test_the_collapsed_progression_is_restored():
    schema = _schema()
    msgs = inject_missing_concept_missions(schema)
    assert [m.title for m in schema.missions] == [
        "Clear the Counter", "Book the Night In", "Close the Office"]
    assert sum(len(m.objectives) for m in schema.missions) == 4
    assert any("restored from the brief's own MISSIONS section" in m for m in msgs)


def test_what_the_llm_wrote_is_kept_and_never_overwritten():
    """Append-only, exactly like the entity rescue. A mission schema-fill got
    right is the schema-fill's, and it keeps its own objective ids."""
    kept = MissionSpec(id="m1", title="Clear the Counter",
                       description="the LLM's own words",
                       objectives=[MissionObjective(
                           id="obj_1", type="collect", target_ref="LostUmbrellas",
                           description="Shelve them.", count=5)])
    schema = _schema(missions=[kept])
    inject_missing_concept_missions(schema)
    assert schema.missions[0] is kept
    assert schema.missions[0].description == "the LLM's own words"
    assert [m.title for m in schema.missions] == [
        "Clear the Counter", "Book the Night In", "Close the Office"]


def test_restored_objective_ids_are_unique_against_what_exists():
    schema = _schema(missions=[MissionSpec(
        id="m1", title="Something Else", objectives=[MissionObjective(
            id="obj_1", description="Do it.", type="reach")])])
    inject_missing_concept_missions(schema)
    ids = [o.id for m in schema.missions for o in m.objectives]
    assert len(ids) == len(set(ids))


def test_a_bare_objective_is_reported_not_quietly_left_blank():
    msgs = inject_missing_concept_missions(_schema())
    assert any("name no target" in m and "unity_objective_targets" in m
               for m in msgs)


def test_it_is_deterministic():
    a, b = _schema(), _schema()
    assert inject_missing_concept_missions(a) == inject_missing_concept_missions(b)
    assert ([(m.id, m.title, [o.id for o in m.objectives]) for m in a.missions]
            == [(m.id, m.title, [o.id for o in m.objectives]) for m in b.missions])


# ── The point: the restored missions reach the composer ──────────────────────

def test_the_restored_progression_becomes_the_step_list():
    from src.micro.rules import steps_from_missions
    schema = _schema()
    inject_missing_concept_missions(schema)
    steps = steps_from_missions(schema.missions)
    assert len(steps) == 4
    assert steps[0]["title"].startswith("1 / 4")
    assert "CLEAR THE COUNTER" in steps[0]["title"]
    assert "CLOSE THE OFFICE" in steps[3]["title"]


def test_the_bare_objective_gets_a_target_from_its_own_sentence():
    """The two passes meeting: the brief declares the step, the resolver works
    out what it is about — "Stamp the night's claim log" -> `ClaimLog`."""
    from src.unity_objective_targets import resolve_objective_targets
    schema = _schema(entities=[
        EntitySpec(name="LostUmbrellas", kind="pickup", count=5),
        EntitySpec(name="ClaimLog", kind="prop", count=1),
        EntitySpec(name="UmbrellaShelf", kind="prop", count=1)])
    inject_missing_concept_missions(schema)
    resolve_objective_targets(schema)
    bare = schema.missions[2].objectives[0]
    assert bare.target_ref == "ClaimLog"
