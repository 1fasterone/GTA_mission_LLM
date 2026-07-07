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
