"""Tests for the brief->layout spatial-facts sidecar (src/unity_spatial_facts.py
+ the consumption rule in src/unity_layout_archetype.py's `_schema_height`).

The bug being closed: the climbing brief says "The summit is a nerve cluster
900m up", schema-fill compressed the number away, and the layout built its 60 m
default wall. Layout behaved correctly with what reached it — the number had no
way past Step 3.

Four contracts are locked here:
  1. Extraction is deterministic and UNIT-GATED: distances in, counts out.
  2. The facts survive transport (JSON round-trip) and cannot be spoofed by an
     LLM writing its own `meta.spatial_facts`.
  3. Dimension resolution is a three-tier rule — schema figure, then a brief
     fact whose referent names THIS dimension, then the named default. Tier 1
     still wins when both exist, and a material disagreement is logged.
  4. A schema with no facts lays out byte-identically to before this existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.unity_layout import compute_layout
from src.unity_layout_archetype import (
    corridor_height,
    surface_height,
    volume_height,
)
from src.unity_schema import UnitySchema
from src.unity_spatial_facts import (
    META_KEY,
    SpatialFact,
    attach_spatial_facts,
    extract_spatial_facts,
    match_facts,
    spatial_facts_of,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def _load(name: str) -> UnitySchema:
    return UnitySchema.model_validate(
        json.loads((_FIXTURES / name).read_text(encoding="utf-8")))


def _brief(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8")


def _values(text: str) -> list[float]:
    return [f.value_m for f in extract_spatial_facts(text)]


# ── 1. Extraction ────────────────────────────────────────────────────────────

class TestExtraction:
    def test_the_climbing_brief_yields_its_summit_height(self):
        """The live failure, in one assertion."""
        facts = extract_spatial_facts(_brief("the_wall_that_wakes.txt"))
        assert [f.value_m for f in facts] == [900.0], facts
        fact = facts[0]
        assert "summit" in fact.referent_text.lower(), fact.referent_text
        assert "900m" in fact.evidence_sentence, fact.evidence_sentence

    def test_a_non_spatial_number_is_not_a_spatial_fact(self):
        """The unit is the whole gate. Without it, a brief's counts, seeds,
        resolutions and version numbers all read as distances."""
        assert _values("The player has 3 lives and fights 5 enemies.") == []
        assert _values("Procedural rock, resolution 64. UNITY VERSION: 6000.0") == []
        assert _values("Five waves of 12 drones, 3 checkpoints, seed 90210.") == []

    @pytest.mark.parametrize("text,metres", [
        ("a 900 m wall",        900.0),
        ("a 900m wall",         900.0),
        ("a 900 metre wall",    900.0),
        ("a 900 meters wall",   900.0),
        ("a 1.5 km approach",   1500.0),
        ("exit at 4000 ft",     1219.2),
        ("exit at 4000 feet",   1219.2),
        ("a 12 kft jump run",   3657.6),
        ("exit at 4,000 ft",    1219.2),
        ("a 1,200 m ceiling",   1200.0),
    ])
    def test_every_unit_normalizes_to_metres(self, text, metres):
        assert _values(text) == [pytest.approx(metres)]

    def test_a_speed_is_not_a_distance(self):
        """"200 km/h" is a unit followed by "/" — the trailing guard rejects
        it, the same way the schema-side altitude parser always has."""
        assert _values("Karts reach 200 km/h on the back straight.") == []

    def test_a_unit_letter_inside_a_word_is_not_a_unit(self):
        assert _values("900 mountains ring the valley.") == []
        assert _values("The 60 foothills below the ridge.") == []

    def test_a_decimal_point_is_not_a_sentence_boundary(self):
        """Splitting sentences on a bare "." turns "4.5 m" into "5 m" — the
        fact comes out an order of magnitude wrong and nothing flags it."""
        assert _values("Holds sit 4.5 m apart. Then the ledge.") == [4.5]

    def test_the_evidence_is_the_sentence_the_number_came_from(self):
        facts = extract_spatial_facts(
            "The yard is fenced. The tower is 40 m tall; climb it. Done.")
        assert len(facts) == 1
        assert facts[0].evidence_sentence == "The tower is 40 m tall"

    def test_a_hard_wrapped_sentence_still_reaches_its_referent(self):
        """Briefs are hard-wrapped, so the referent and its number routinely
        land on different LINES of the same sentence."""
        facts = extract_spatial_facts(
            "The summit is a nerve\ncluster 900m up; reaching it ends it.")
        assert [f.value_m for f in facts] == [900.0]
        assert "cluster" in facts[0].referent_text

    def test_the_referent_window_stops_short_of_a_neighbouring_number(self):
        """A number must not borrow the noun phrase of the one beside it, or
        every dimension in a brief matches every other dimension."""
        facts = extract_spatial_facts(
            "The wall is 300 m tall. The dive is to 80 m of depth.")
        wall = next(f for f in facts if f.value_m == 300.0)
        assert "depth" not in wall.referent_text.lower(), wall.referent_text

    def test_extraction_is_deterministic_and_deduplicated(self):
        text = _brief("dropzone.txt")
        assert extract_spatial_facts(text) == extract_spatial_facts(text)
        dup = "Hard deck at 600 m. Hard deck at 600 m."
        assert _values(dup) == [600.0]

    def test_no_numbers_at_all_yields_no_facts(self):
        assert extract_spatial_facts(
            "A quiet walking game in a fenced depot yard.") == []
        assert extract_spatial_facts("") == []


# ── 2. Transport ─────────────────────────────────────────────────────────────

class TestTransport:
    @staticmethod
    def _schema() -> UnitySchema:
        return UnitySchema(
            project_name="Sidecar", company_name="C", description="d",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear", locomotion="walk",
            features=[], scenes=["Main"], packages=[], entities=[])

    def test_facts_survive_a_json_round_trip(self):
        """The generator re-runs compute_layout from the serialized schema, so
        a sidecar that does not survive JSON makes the layout non-idempotent."""
        s = self._schema()
        attach_spatial_facts(s, extract_spatial_facts("The wall is 300 m tall."))
        reloaded = UnitySchema.model_validate(json.loads(s.model_dump_json()))
        assert [f.value_m for f in spatial_facts_of(reloaded)] == [300.0]

    def test_the_key_is_always_written_even_when_empty(self):
        """An empty list is a statement — "the brief named no distances" — and
        writing it unconditionally is what stops an LLM-authored
        meta.spatial_facts from being believed."""
        s = self._schema()
        attach_spatial_facts(s, [])
        assert s.meta[META_KEY] == []

    def test_an_llm_authored_sidecar_is_overwritten_not_merged(self):
        s = self._schema()
        s.meta[META_KEY] = [{"value_m": 99999.0, "referent_text": "summit",
                             "evidence_sentence": "invented"}]
        attach_spatial_facts(s, extract_spatial_facts("no distances here"))
        assert spatial_facts_of(s) == []

    def test_a_malformed_entry_degrades_to_no_fact(self):
        """A bad sidecar entry must fall through to the named default, not
        break a build."""
        s = self._schema()
        s.meta[META_KEY] = ["nonsense", {"referent_text": "summit"},
                            {"value_m": "tall", "referent_text": "wall"},
                            {"value_m": 300.0, "referent_text": "the wall",
                             "evidence_sentence": "The wall is 300 m tall"}]
        assert [f.value_m for f in spatial_facts_of(s)] == [300.0]

    def test_a_schema_with_no_meta_key_reads_as_no_facts(self):
        assert spatial_facts_of(self._schema()) == []


# ── 3. Matching ──────────────────────────────────────────────────────────────

class TestMatching:
    _FACTS = [
        SpatialFact(300.0, "the cliff rises 300 m from", "s1"),
        SpatialFact(900.0, "the summit sits 900 m above", "s2"),
        SpatialFact(80.0, "a dive to 80 m of depth", "s3"),
        SpatialFact(2.0, "each hold is 2 m from the last", "s4"),
    ]

    def test_only_facts_naming_the_dimension_match(self):
        got = match_facts(self._FACTS, ("summit", "cliff"), 20.0, 12_000.0)
        assert [f.value_m for f in got] == [900.0, 300.0]

    def test_the_largest_match_comes_first(self):
        got = match_facts(self._FACTS, ("summit", "cliff"), 20.0, 12_000.0)
        assert got[0].value_m == 900.0

    def test_out_of_range_facts_are_ignored(self):
        """A 2 m hold spacing is a real distance and is not a wall height."""
        assert match_facts(self._FACTS, ("hold",), 20.0, 12_000.0) == []

    def test_matching_is_word_bounded(self):
        assert match_facts([SpatialFact(300.0, "the topology graph", "s")],
                           ("top",), 20.0, 12_000.0) == []


# ── 4. Consumption — the three-tier rule ─────────────────────────────────────

class TestSurfaceHeightTiers:
    """The living-wall regression. This is the REAL schema-fill output
    (living_wall_llm.schema.json), captured from a live run, paired with the
    REAL brief it was generated from."""

    @staticmethod
    def _wall(with_facts: bool = True) -> UnitySchema:
        s = _load("living_wall_llm.schema.json")
        if with_facts:
            attach_spatial_facts(
                s, extract_spatial_facts(_brief("the_wall_that_wakes.txt")))
        return s

    def test_without_the_sidecar_the_wall_defaults_and_warns(self):
        """The bug, preserved: this schema carries no height anywhere."""
        h, why, defaulted = surface_height(self._wall(with_facts=False))
        assert (h, defaulted) == (60.0, True)
        assert "_SURFACE_HEIGHT_DEFAULT" in why

    def test_the_brief_fact_sizes_the_wall_instead_of_the_default(self):
        h, why, defaulted = surface_height(self._wall())
        assert h == 900.0
        assert defaulted is False
        assert "summit" in why.lower(), why

    def test_the_layout_logs_layout_facts_with_the_evidence(self, caplog):
        with caplog.at_level("INFO"):
            msgs = compute_layout(self._wall())
        # The tag lives in the LOG only — Rich eats "[layout.facts]" out of any
        # message the pipeline console-prints.
        assert not any("[layout.facts]" in m for m in msgs), msgs
        facts_lines = [r.getMessage() for r in caplog.records
                       if "[layout.facts]" in r.getMessage()]
        assert facts_lines, caplog.text
        assert "900 m" in facts_lines[0], facts_lines
        assert "summit" in facts_lines[0].lower(), facts_lines
        assert any("H=900 m" in m for m in msgs), msgs

    def test_no_default_warning_when_the_brief_supplied_the_height(self, caplog):
        with caplog.at_level("WARNING"):
            compute_layout(self._wall())
        assert not any("named default wall height" in r.message
                       for r in caplog.records), caplog.text

    def test_the_wall_reaches_the_briefed_height(self):
        """Sourcing the dimension is the whole change — the wall the layout
        builds is 900 m of face, not 60."""
        s = self._wall()
        compute_layout(s)
        tops = [p[1] for e in s.entities for p in e.instance_positions]
        assert max(tops) > 300.0, max(tops)

    def test_a_depth_fact_never_sizes_a_wall(self):
        """Referent vocabulary is per dimension: one brief can state a wall
        height, a dive depth and a ceiling without them being swappable."""
        s = self._wall(with_facts=False)
        attach_spatial_facts(s, extract_spatial_facts(
            "The trench bottoms out at 400 m of depth below the ceiling."))
        h, _why, defaulted = surface_height(s)
        assert (h, defaulted) == (60.0, True)


class TestTierPrecedence:
    @staticmethod
    def _skydive() -> UnitySchema:
        return _load("dropzone_skydive.schema.json")

    def test_the_skydiving_fixture_resolves_from_the_schema_not_a_default(self):
        """Tier 1: this fixture states its own pull altitude, so the brief's
        facts change nothing about it."""
        s = self._skydive()
        before = corridor_height(s)
        attach_spatial_facts(s, extract_spatial_facts(_brief("dropzone.txt")))
        after = corridor_height(s)
        assert before == after
        assert (after[0], after[2]) == (1500.0, False)

    def test_the_skydiving_fixtures_placement_is_unchanged_by_the_sidecar(self):
        bare, with_facts = self._skydive(), self._skydive()
        attach_spatial_facts(with_facts,
                             extract_spatial_facts(_brief("dropzone.txt")))
        compute_layout(bare)
        compute_layout(with_facts)
        assert [e.model_dump() for e in with_facts.entities] == \
               [e.model_dump() for e in bare.entities]

    def test_the_schema_figure_wins_over_a_disagreeing_brief_fact(self, caplog):
        s = self._skydive()
        attach_spatial_facts(s, [
            SpatialFact(4000.0, "exit altitude of 4000 m over", "brief line")])
        with caplog.at_level("WARNING"):
            h, why, defaulted = corridor_height(s)
        assert (h, defaulted) == (1500.0, False)      # tier 1 still wins
        assert "brief disagrees" in why, why
        assert any("[layout.facts]" in r.getMessage()
                   for r in caplog.records), caplog.text

    def test_agreement_within_the_tolerance_is_not_reported(self, caplog):
        s = self._skydive()
        attach_spatial_facts(s, [
            SpatialFact(1600.0, "pull altitude of 1600 m", "brief line")])
        with caplog.at_level("WARNING"):
            _h, why, _d = corridor_height(s)
        assert "disagrees" not in why, why

    def test_all_matching_candidates_are_logged_not_just_the_winner(self, caplog):
        s = self._skydive()
        s.entities, s.scripts, s.missions, s.story_beats = [], [], [], []
        s.description = "A skydiving game with a parachute canopy."
        attach_spatial_facts(s, [
            SpatialFact(3000.0, "exit altitude of 3000 m", "high line"),
            SpatialFact(600.0, "hard deck at 600 m", "low line")])
        with caplog.at_level("INFO"):
            h, why, defaulted = corridor_height(s)
        assert (h, defaulted) == (3000.0, False)      # largest wins for heights
        # Every candidate is logged WITH its own referent and evidence, so a
        # run that sized itself from the wrong figure can be diagnosed from the
        # log alone.
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "exit altitude of 3000 m" in blob and "high line" in blob, blob
        assert "hard deck at 600 m" in blob and "low line" in blob, blob
        # The message the fixer prints stays compact — the winner in full, the
        # runners-up as values.
        assert "largest of 2 matching facts" in why and "600 m" in why, why


class TestDefaultTierIsUnchanged:
    """Tier 3 must behave exactly as it did before the sidecar existed."""

    @staticmethod
    def _stripped() -> UnitySchema:
        s = _load("dropzone_skydive.schema.json")
        s.description = ("A skydiving game: the player exits the aircraft, "
                         "freefalls, and deploys a parachute canopy.")
        s.scripts, s.missions, s.story_beats = [], [], []
        for e in s.entities:
            e.instance_positions, e.path_points, e.elevation = [], [], 0.0
        return s

    def test_a_brief_with_no_numbers_falls_through_to_the_default(self, caplog):
        s = self._stripped()
        attach_spatial_facts(s, extract_spatial_facts(
            "A skydiving game about falling out of a plane with friends."))
        assert s.meta[META_KEY] == []
        h, why, defaulted = corridor_height(s)
        assert (h, defaulted) == (4000.0, True)
        assert "_CORRIDOR_HEIGHT_DEFAULT" in why
        with caplog.at_level("WARNING"):
            compute_layout(s)
        assert any("named default corridor height" in r.message
                   for r in caplog.records), caplog.text

    def test_an_empty_sidecar_is_indistinguishable_from_no_sidecar(self):
        """"Byte-identical where no spatial facts apply" — the assertion."""
        bare, empty = self._stripped(), self._stripped()
        attach_spatial_facts(empty, [])
        assert corridor_height(bare) == corridor_height(empty)
        compute_layout(bare)
        compute_layout(empty)
        assert [e.model_dump() for e in empty.entities] == \
               [e.model_dump() for e in bare.entities]

    @pytest.mark.parametrize("fixture,resolve", [
        ("skyline_glider.schema.json", volume_height),
        ("lagoon_survey.schema.json", volume_height),
        ("living_wall_llm.schema.json", surface_height),
        ("dropzone_arcade_llm.schema.json", corridor_height),
    ])
    def test_every_fixture_resolves_identically_with_an_empty_sidecar(
            self, fixture, resolve):
        bare, empty = _load(fixture), _load(fixture)
        attach_spatial_facts(empty, [])
        assert resolve(bare) == resolve(empty)
        compute_layout(bare)
        compute_layout(empty)
        assert [e.model_dump() for e in empty.entities] == \
               [e.model_dump() for e in bare.entities]
