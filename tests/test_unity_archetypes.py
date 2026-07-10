"""
Tests for unity_archetypes: the deterministic game_type/locomotion/genre
keyword classifier that biases/overrides the LLM's schema output.
"""
from src.unity_archetypes import classify, apply_intent, _normalize_for_matching


# ── Word-boundary matching (no substring bleed into unrelated words) ───────────

class TestWordBoundaryMatching:
    def test_vive_does_not_match_inside_survive(self):
        # Regression (live): "vive" (HTC Vive, a VR game_type keyword) is a
        # substring of the ordinary English word "survive" — a plain `kw in
        # text` check flipped an underwater-survival prompt to game_type=VR.
        intent = classify("they must collect data logs, survive predators, "
                          "and escape before their oxygen runs out")
        assert intent.game_type != "VR"
        assert "game_type" not in intent.matched

    def test_vive_still_matches_as_a_real_word(self):
        intent = classify("A room-scale HTC Vive adventure game")
        assert intent.game_type == "VR"
        assert "vive" in intent.matched.get("game_type", [])

    def test_bare_drift_does_not_trigger_racing_genre(self):
        # Regression (live): bare "drift" collided with non-racing physics
        # language ("buoyancy drift") and flipped genre to racing on a single
        # ambiguous word.
        intent = classify("3D six-axis underwater movement, buoyancy drift, "
                          "current push, sprint bubble burst")
        assert intent.genre != "racing"

    def test_drift_racing_phrase_still_triggers_racing_genre(self):
        intent = classify("An arcade drift racing game with neon cars")
        assert intent.genre == "racing"

    def test_pascalcase_project_title_driftarcade_is_racing(self):
        # Live (DriftArcade run): the title was the only racing signal in the
        # prompt; camel-splitting yields "drift arcade", which must classify as
        # racing or zero genre bias reaches asset selection downstream.
        intent = classify("DriftArcade: sliding cars around tight corners")
        assert intent.genre == "racing"

    def test_racetrack_and_speedway_trigger_racing(self):
        assert classify("Build a racetrack level").genre == "racing"
        assert classify("A stock-car speedway at night").genre == "racing"

    def test_drifting_mood_language_still_not_racing(self):
        # The new phrases must not reopen the bare-drift landmine.
        intent = classify("A calm scene with drifting clouds and a drift of "
                          "snow across the field at sunset")
        assert intent.genre != "racing"

    def test_racing_structure_hint_names_required_entities(self):
        intent = classify("An arcade drift racing game with neon cars")
        hint = intent.structure_hint()
        assert "vehicle" in hint and "track" in hint and "barrier" in hint.lower()


# ── camelCase/PascalCase compound identifiers in structured prompts ────────────

class TestCamelCaseNormalization:
    def test_keyword_glued_into_pascalcase_identifier_still_matches(self):
        # Regression: structured prompts list script names in PascalCase
        # ("SwimmingController") rather than prose. Lowercasing first destroys
        # the case-transition signal a word-boundary split depends on, so
        # normalization must split camelCase/PascalCase BEFORE lowercasing.
        assert _normalize_for_matching("SwimmingController") == "swimming controller"
        assert _normalize_for_matching("O2Manager") == "o2 manager"

    def test_swimming_locomotion_detected_from_class_name_only(self):
        intent = classify("SwimmingController (player) handles movement")
        assert intent.locomotion == "swim"

    def test_all_caps_acronym_untouched(self):
        assert _normalize_for_matching("NPC") == "npc"

    def test_empty_string_untouched(self):
        assert _normalize_for_matching("") == ""

    def test_literal_dimension_tokens_survive_normalization(self):
        # Regression: the camel-boundary split used to fire on ANY digit-then-
        # capital transition, so literal "2D"/"3D"/"2.5D" dimension notation
        # (as a human actually types it) got mangled into "2 D"/"3 D"/"2.5 D"
        # and silently lost every "2d"/"3d"/"2.5d" keyword match below.
        assert _normalize_for_matching("2D") == "2d"
        assert _normalize_for_matching("3D") == "3d"
        assert _normalize_for_matching("2.5D") == "2.5d"

    def test_game_type_2_5d_detected_from_capitalized_prompt(self):
        intent = classify("IRONVEIN is a 2.5D game with parallax layers")
        assert intent.game_type == "2.5D"

    def test_game_type_2d_detected_from_capitalized_prompt(self):
        intent = classify("a side-scrolling 2D platformer for mobile")
        assert intent.game_type == "2D"

    def test_game_type_3d_detected_from_capitalized_prompt(self):
        intent = classify("a fully 3D dinosaur survival game")
        assert intent.game_type == "3D"


# ── Full deepdive.txt regression (the exact prompt that surfaced this bug) ─────

class TestDeepDivePromptRegression:
    _PROMPT = (
        "Deep Dive — a 3D underwater exploration and survival game for PC.\n"
        "SwimmingController (player) 3D six-axis underwater movement, buoyancy "
        "drift, current push, sprint bubble burst, surface bob\n"
        "they must collect data logs, survive predators, and escape before "
        "their oxygen runs out.\n"
        "UnderwaterCamera (camera) Cinemachine third-person, lazy follow\n"
        "Vehicle: none\n"
    )

    def test_game_type_is_3d_not_vr(self):
        intent = classify(self._PROMPT)
        assert intent.game_type == "3D"

    def test_locomotion_is_swim(self):
        intent = classify(self._PROMPT)
        assert intent.locomotion == "swim"

    def test_genre_is_not_racing(self):
        intent = classify(self._PROMPT)
        assert intent.genre != "racing"

    def test_apply_intent_overrides_schema_correctly(self):
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="DeepDive", company_name="C", description="",
            game_type="VR", platform="pc", render_pipeline="URP",   # LLM got it wrong
            unity_version="6000.0", color_space="Linear",
            locomotion="walk",
        )
        intent = classify(self._PROMPT)
        apply_intent(schema, intent)
        assert schema.game_type == "3D"
        assert schema.locomotion == "swim"
        assert schema.meta["genre"] != "racing"


# ── 2d_topdown vs top_down (flat 2D vs real-3D dungeon crawler) ────────────────

class Test2DTopdownVsTopDown:
    def test_dimension_neutral_top_down_stays_3d_archetype(self):
        # "top-down"/"overhead view" alone describe a real 3D dungeon crawler
        # just as easily as a flat 2D game — must default to the existing
        # (unchanged) 3D top_down archetype, not the new 2d_topdown one.
        intent = classify("A top-down dungeon crawler with real 3D models and NavMesh enemies.")
        assert intent.game_type == "top_down"

    def test_zelda_like_alone_selects_2d_topdown(self):
        intent = classify("A 2D zelda-like adventure with dungeons and puzzles.")
        assert intent.game_type == "2d_topdown"

    def test_twin_stick_bullet_heaven_selects_2d_topdown(self):
        intent = classify("A twin-stick bullet heaven survival game.")
        assert intent.game_type == "2d_topdown"

    def test_tie_between_top_down_and_2d_topdown_favors_2d_topdown(self):
        # Regression guard: a prompt that hits BOTH tables' keywords with equal
        # score must resolve to the more specific (2D-qualified) archetype, not
        # silently fall back to the 3D one via list-iteration-order tie-break.
        intent = classify("A 2D zelda-like top-down adventure with dungeons and puzzles.")
        assert intent.game_type == "2d_topdown"

    def test_isometric_keywords_unaffected_by_the_split(self):
        intent = classify("An isometric diablo-like loot game.")
        assert intent.game_type == "isometric"


def test_drifting_clouds_is_not_racing():
    # Regression (TerminalHorizon, live): "animated title over drifting clouds"
    # matched the racing keyword "drifting" and concept_hint() then steered the
    # whole LLM concept toward a racing game. Mood/physics uses of drift must
    # never signal the racing genre.
    from src.unity_archetypes import classify
    i = classify("A freefall survival game. MainMenu: animated title over "
                 "drifting clouds. The player dives through jetstreams.")
    assert i.genre != "racing"
    j = classify("A street racing game with drift racing and kart racers")
    assert j.genre == "racing"
