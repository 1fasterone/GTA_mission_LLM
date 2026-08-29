"""
Tests for unity_concept_fidelity: did the words in the brief reach the build?

The metric exists because nothing in the pipeline noticed when every adjective
in a prompt evaporated. `_check_brief_entity_coverage` grades NAMES only, so
"commit node" -> marble-center-butterfly-low.fbx passed: the name survived, the
model is a marble run.
"""
from src.unity_concept_fidelity import (
    check_asset_fidelity, check_fidelity, extract_demands,
    extract_free_modifiers, render_scorecard,
)

HIPPOS = ("Purple hippos with six legs, wearing bowler hats, driving tanks "
          "and hurling custard pies at paper airplanes.")


class _E:
    """Minimal entity stand-in — the module reads free-text fields by name."""
    def __init__(self, **kw):
        self.__dict__.update(
            {"name": "", "kind": "prop", "shape": "", "color_role": "",
             "behavior": "", "model_hint": "", "sketchfab_query": "", "tag": ""})
        self.__dict__.update(kw)


class _S:
    def __init__(self, entities, meta=None):
        self.entities = entities
        self.meta = meta or {}


# ── Extraction ────────────────────────────────────────────────────────────────

class TestDemandExtraction:
    def test_colour_and_count_bind_to_their_noun(self):
        d = {x.noun: x.modifiers for x in extract_demands(HIPPOS, ["Hippo"])}
        assert "purple" in d["Hippo"]
        assert "six" in d["Hippo"]

    def test_plain_nouns_are_modifiers_too(self):
        # An earlier cut accepted only colours, numbers, hyphenates and -ed/-ish.
        # That sounds principled and threw away exactly the words the metric
        # exists to protect.
        d = {x.noun: x.modifiers for x in extract_demands(HIPPOS, ["Pie", "Airplane"])}
        assert "custard" in d["Pie"]
        assert "paper" in d["Airplane"]

    def test_a_trailing_clause_reaches_past_a_comma(self):
        # "wearing bowler hats" sits seven tokens past the noun; a fixed token
        # window silently truncated every trailing clause.
        d = {x.noun: x.modifiers for x in extract_demands(HIPPOS, ["Hippo"])}
        assert "bowler" in d["Hippo"] and "hats" in d["Hippo"]

    def test_a_clause_stops_at_the_next_noun(self):
        # Without a boundary the hats were smeared onto the tanks AND the pies.
        d = {x.noun: x.modifiers for x in extract_demands(HIPPOS, ["Hippo", "Pie"])}
        assert "bowler" not in d["Pie"]

    def test_modifiers_do_not_leak_across_sentences(self):
        brief = "A purple hippo lives here. A tank waits outside."
        d = {x.noun: x.modifiers for x in extract_demands(brief, ["Hippo", "Tank"])}
        assert "purple" not in d["Tank"]

    def test_plural_in_brief_matches_singular_entity(self):
        d = {x.noun: x.modifiers for x in extract_demands("Red ducks swim.", ["Duck"])}
        assert "red" in d["Duck"]

    def test_empty_brief_demands_nothing(self):
        assert extract_demands("", ["Hippo"]) == []

    def test_hyphenated_compounds_are_caught_even_unbound(self):
        # A binding miss must never silently drop an explicit commitment.
        assert "six-legged" in extract_free_modifiers("A six-legged hippo.")


# ── Survival ──────────────────────────────────────────────────────────────────

class TestSurvival:
    def test_a_build_that_kept_the_words_scores_100(self):
        s = _S([_E(name="Hippo", color_role="purple",
                   model_hint="six legs bowler hat hippo")])
        rep = check_fidelity("Purple hippos with six legs, wearing bowler hats.",
                             s, nouns=["Hippo"])
        assert rep.score == 100
        assert rep.gaps == []

    def test_a_build_that_dropped_them_reports_each_one(self):
        s = _S([_E(name="Hippo", model_hint="hippo")])
        rep = check_fidelity(HIPPOS, s, nouns=["Hippo"])
        lost = {g.demand for g in rep.gaps}
        assert any("purple" in x for x in lost)
        assert any("six" in x for x in lost)
        assert rep.score < 50

    def test_a_number_word_is_honoured_by_its_digit(self):
        # A schema that wrote count: 6 honoured "six"; failing it for spelling
        # would be noise, not signal.
        s = _S([_E(name="Hippo", model_hint="hippo with 6 legs")])
        rep = check_fidelity("A hippo with six legs.", s, nouns=["Hippo"])
        assert rep.gaps == []

    def test_the_user_prompt_in_meta_cannot_make_a_check_pass(self):
        # meta.user_prompt is the brief itself; counting it would make every
        # demand trivially present and the metric worthless.
        s = _S([_E(name="Hippo", model_hint="hippo")], meta={"user_prompt": HIPPOS})
        rep = check_fidelity(HIPPOS, s, nouns=["Hippo"])
        assert rep.gaps

    def test_an_empty_brief_scores_100_not_0(self):
        rep = check_fidelity("", _S([]), nouns=[])
        assert rep.score == 100


# ── Asset fidelity ────────────────────────────────────────────────────────────

class TestAssetFidelity:
    def test_an_unrelated_model_is_flagged(self):
        # Live: 'commit node' -> marble-center-butterfly-low.fbx, and the
        # resolver reported success.
        s = _S([_E(name="CommitNode", model_hint="commit node")])
        gaps = check_asset_fidelity(
            s, {"CommitNode": "Models/marble-center-butterfly-low.fbx"})
        assert len(gaps) == 1 and gaps[0].kind == "asset"

    def test_a_matching_model_is_not_flagged(self):
        s = _S([_E(name="Hippo", model_hint="hippo")])
        assert check_asset_fidelity(s, {"Hippo": "Models/hippo-low.fbx"}) == []

    def test_filename_noise_alone_never_counts_as_a_match(self):
        # Every Kenney asset ends in "-low"/"detailed"; matching on those would
        # make the check pass on literally any pair.
        s = _S([_E(name="Detailed", model_hint="low poly")])
        gaps = check_asset_fidelity(s, {"Detailed": "Models/fence-low-detailed.fbx"})
        assert len(gaps) == 1

    def test_ui_built_elements_are_never_scored(self):
        # ui: paths are BUILT to spec by EntitySpawner, not matched.
        s = _S([_E(name="ClearancePad", model_hint="button")])
        assert check_asset_fidelity(s, {"ClearancePad": "ui:button"}) == []

    def test_default_tier_paths_are_never_scored(self):
        # Live (DeadReckoningProtocol): a `player`-kind entity named
        # "Operative" resolves to the curated default player pack by ROLE,
        # not by name — Tier B of unity_entity_asset_resolver.py never
        # claims a name match, so it must not be held to one.
        s = _S([_E(name="Operative", kind="player", model_hint="operative")])
        assert check_asset_fidelity(
            s, {"Operative": "Models/Default/Player/Player"}) == []

    def test_asset_gaps_sort_above_modifier_gaps(self):
        s = _S([_E(name="Pie", model_hint="pie")])
        rep = check_fidelity(HIPPOS, s, nouns=["Pie"],
                             entity_model_paths={"Pie": "Models/fence-low.fbx"})
        assert rep.gaps and "[asset]" in render_scorecard(rep).splitlines()[1]


# ── The report must never break the run it reports on ─────────────────────────

class TestReportSafety:
    def test_scorecard_is_ascii_only(self):
        # Box-drawing characters crashed a plain print() under the Windows
        # console's cp1252 codec. A report that kills the build is worse than
        # no report.
        s = _S([_E(name="Pie", model_hint="pie")])
        card = render_scorecard(check_fidelity(HIPPOS, s, nouns=["Pie"]))
        card.encode("cp1252")

    def test_a_schema_missing_every_optional_field_does_not_raise(self):
        class Bare:
            pass
        rep = check_fidelity(HIPPOS, Bare(), nouns=["Hippo"])
        assert rep.gaps


# ── Vibe prompts: the case the metric was silently useless on ─────────────────

class TestVibePromptFallback:
    def test_a_prose_brief_still_grades(self):
        # `extract_named_entities` only finds entities in a STRUCTURED brief. On
        # a vibe prompt it returns none, which gave 0 demands, 0 losses and a
        # triumphant 100/100 measuring nothing — the silent-default failure this
        # module exists to catch, reproduced inside the instrument.
        s = _S([_E(name="Hippo", model_hint="hippo")])
        rep = check_fidelity(HIPPOS, s)          # no nouns= passed
        assert rep.checked > 0
        assert rep.gaps

    def test_pascalcase_entity_binds_to_a_two_word_brief_phrase(self):
        # "CessnaCaravan" tokenised to one word and never matched the brief's
        # "Cessna Caravan", so every entity bound zero modifiers and the report
        # read a confident 100/100. Pre-lowercasing destroyed the case
        # transition the camel-split depends on.
        d = {x.noun: x.modifiers
             for x in extract_demands("The player exits a red Cessna Caravan.",
                                      ["CessnaCaravan"])}
        assert "red" in d["CessnaCaravan"]

    def test_nothing_to_check_is_reported_as_zero_not_as_a_pass(self):
        rep = check_fidelity("...", _S([]))
        assert rep.checked == 0          # caller must abstain, never print 100


# ── The score's denominator ───────────────────────────────────────────────────

class TestScoreAccounting:
    def test_graded_assets_count_toward_the_denominator(self):
        # Leaving them out produced "100/100" printed next to a list of losses.
        s = _S([_E(name="Hippo", model_hint="hippo")])
        rep = check_fidelity("A hippo.", s,
                             entity_model_paths={"Hippo": "Models/hippo-low.fbx"})
        assert rep.assets_checked == 1

    def test_ui_paths_are_not_counted_as_graded_assets(self):
        s = _S([_E(name="Pad", model_hint="button")])
        rep = check_fidelity("A pad.", s, entity_model_paths={"Pad": "ui:button"})
        assert rep.assets_checked == 0

    def test_an_asset_only_loss_never_reports_a_perfect_score(self):
        s = _S([_E(name="CommitNode", model_hint="commit node")])
        rep = check_fidelity(
            "A commit node.", s,
            entity_model_paths={"CommitNode": "Models/marble-butterfly-low.fbx"})
        assert rep.gaps and rep.score < 100

    def test_a_brief_that_commits_to_nothing_grades_nothing(self):
        # "make me a game" against a build full of entities: the schema-entity
        # fallback bound Player/Camera, every one trivially "survived", and a
        # brief that committed to NOTHING scored 100/100. A noun the brief never
        # says is not a demand. (Pinned from the other side by
        # test_unity_playability's TestConceptFidelity, which requires this case
        # to come back UNGRADED rather than passed.)
        s = _S([_E(name="Player", model_hint="player"),
                _E(name="MainCamera", model_hint="camera")])
        assert check_fidelity("make me a game", s).checked == 0
