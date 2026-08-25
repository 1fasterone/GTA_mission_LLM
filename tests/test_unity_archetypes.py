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


# ── The declaration tier: locomotion="none" ───────────────────────────────────

class TestNonEmbodiedDeclaration:
    """`locomotion` had four values and no way to say "this game has no body".

    A brief could spend four paragraphs explaining that the player never moves
    and still had to be scored as one of walk/drive/fly/swim, because the axis
    could not represent the answer. That is a missing value, not a tuning
    problem — these tests pin the value in.
    """

    def _tower_control(self):
        with open("prompts/tower_control.txt", encoding="utf-8") as f:
            return f.read()

    def test_tower_control_is_not_a_flying_game(self):
        # Regression (live): prompts/tower_control.txt is an approach-control
        # console whose prose says "you are never in an aircraft" and "no
        # character, no vehicle, no camera to fly". It classified
        # locomotion='fly' at confidence 6 — "flight" matched `flight strips`
        # (paper on a desk) and "aircraft" matched the sentence denying it.
        intent = classify(self._tower_control())
        assert intent.locomotion == "none"

    def test_the_suppressed_movement_words_are_named_not_dropped(self):
        # A signal the declaration outranks must stay legible in the log — the
        # silent drop is the defect, not the override.
        intent = classify(self._tower_control())
        assert intent.matched["locomotion_suppressed"] == ["flight", "aircraft"]

    def test_board_card_and_word_games_declare_none_on_one_phrase(self):
        for text in ("A board game where ducks play monopoly in a bowl of spaghetti.",
                     "A card game about haunted tarot decks.",
                     "A word game about anagrams; you click letter tiles."):
            assert classify(text).locomotion == "none", text

    def test_point_and_click_declares_none(self):
        assert classify("A point-and-click jigsaw puzzle with drag and drop "
                        "pieces.").locomotion == "none"

    def test_one_supporting_phrase_alone_never_declares_none(self):
        # "no character customization" is ordinary RPG copy. A single SUPPORTING
        # phrase scores 3 against a bar of 6, so it corroborates and never
        # decides — an embodied game must not lose its avatar to one sentence.
        intent = classify("An open world action RPG. There is no character "
                          "customization at launch; you play a fixed hero who "
                          "walks and rides horses.")
        assert intent.locomotion != "none"

    def test_a_real_flight_sim_outranks_an_incidental_board_game(self):
        # The tiebreak in the other direction: the declaration wins only when it
        # is at least as strong as the movement vocabulary it displaces.
        intent = classify("A flight sim about jet dogfights and aerial combat "
                          "over the ocean; the briefing room has a board game "
                          "on the table.")
        assert intent.locomotion == "fly"

    def test_jets_over_iraq_stay_embodied(self):
        intent = classify("A flight sim: jets in the Iraq war, dogfighting and "
                          "strafing ground troops from the air.")
        assert intent.locomotion == "fly"


# ── Denial-aware keyword matching ─────────────────────────────────────────────

class TestDenialAwareMatching:
    def test_a_denied_keyword_does_not_score(self):
        # "you are never in an aircraft" is the brief saying this is NOT a
        # flying game, and it scored 3 for `fly`.
        intent = classify("You work a radar desk. You are never in an aircraft.")
        assert intent.locomotion != "fly"

    def test_denial_suppresses_on_every_axis_not_just_locomotion(self):
        intent = classify("A flat interface game. No 3D scenery of any kind.")
        assert intent.game_type != "3D"

    def test_a_word_asserted_elsewhere_still_counts(self):
        # Only a keyword whose EVERY occurrence sits inside a denial is
        # suppressed — one denial must not silence a genuine assertion later.
        intent = classify("There is no underwater section. This is a driving "
                          "game about rally cars and street racers.")
        assert intent.locomotion == "drive"

    def test_denial_does_not_run_past_the_clause(self):
        # A "no" early in a sentence must not silence the rest of a paragraph.
        intent = classify("There is no tutorial, and the game is a rally "
                          "driving game with cars and vehicular combat.")
        assert intent.locomotion == "drive"


# ── The hints actually injected into Steps 2 and 3 ────────────────────────────

class TestHintHonesty:
    def test_structure_hint_names_what_it_matched(self):
        # It asserted "the prompt names it explicitly" for every value,
        # including ones reached from a soft concept echo. TowerControl was told
        # `locomotion MUST be "fly" — the prompt names it explicitly` about a
        # brief reading "you are never in an aircraft".
        intent = classify("A rally driving game with cars.")
        hint = intent.structure_hint()
        assert "the prompt names it explicitly" not in hint
        assert "matched:" in hint

    def test_low_confidence_emits_no_MUST(self):
        # Gated at the same bar apply_intent uses. A concept-only echo is worth
        # 1; it must not issue the strongest possible instruction under a
        # heading that calls itself "not a suggestion".
        intent = classify("", concept="a game with a car in it somewhere")
        assert intent.locomotion_confidence < 3
        assert '"locomotion" MUST' not in intent.structure_hint()

    def test_none_reaches_step2_as_prose_and_step3_as_a_constraint(self):
        # Step 3 only ever sees the CONCEPT, so if the declaration does not
        # survive into concept_hint it is gone before the schema is filled.
        intent = classify("A board game where ducks play monopoly.")
        assert "NO avatar" in intent.concept_hint()
        assert '"locomotion" MUST be "none"' in intent.structure_hint()

    def test_none_never_renders_as_noneing(self):
        # _LOCOMOTION_LABEL has no "none" key and the movement branch builds
        # f"{locomotion}ing" — the declaration must not reach that branch.
        hint = classify("A board game about trains.").concept_hint()
        assert "noneing" not in hint


# ── Control Surface Stack (multi-mode) detection ────────────────────────────
# docs/CONTROL_SURFACE_STACK.md — `locomotion_modes` had no deterministic
# backstop at all before this: 100% LLM-decided at Step 3, and confirmed
# empirically to not reliably get set even with explicit schema-prompt
# guidance (VectorCore stress prompt, 2 different local models, 4 straight
# runs). This mirrors the OTHER three axes' keyword-classifier backstop.

class TestControlSurfaceStackDetection:
    def test_switch_signal_plus_two_kinds_detected(self):
        intent = classify(
            "The player switches between on-foot parkour and driving a car "
            "through a shared Mode Manager.")
        assert set(intent.locomotion_modes) == {"on_foot", "vehicle_car"}

    def test_on_foot_leads_the_ordering_when_present(self):
        intent = classify(
            "Switch between on-foot parkour, driving a car, and flying a "
            "jet — a Control Surface Stack testbed.")
        assert intent.locomotion_modes[0] == "on_foot"
        assert set(intent.locomotion_modes) == {"on_foot", "vehicle_car", "flying"}

    def test_no_switch_signal_stays_empty_even_with_two_kinds_named(self):
        # A brief that just HAPPENS to mention driving and flying somewhere
        # (a garage next to an airstrip, say) is not a Control Surface
        # Stack — the runtime-switch phrase is a required, independent half
        # of the gate, not just more keyword volume.
        intent = classify(
            "A story about a pilot who also loves classic cars. He drives "
            "to the airfield and flies his plane on weekends.")
        assert intent.locomotion_modes == []

    def test_switch_signal_with_only_one_kind_stays_empty(self):
        # "switch between" fires, but only ONE mode kind is actually named —
        # 2+ is required, a registry of one is not a stack (matches
        # _check_multi_mode_registry's own single-entry WARN).
        intent = classify("The player switches between different driving "
                          "styles in this rally game.")
        assert intent.locomotion_modes == []

    def test_denied_kind_does_not_count_toward_the_two(self):
        # Same denial-aware matching every other axis gets — a brief that
        # explicitly rules OUT flying must not still count it as one of the
        # two required kinds.
        intent = classify(
            "Switch between on-foot exploration and driving a car through "
            "the city. There is no flying in this game.")
        assert intent.locomotion_modes == ["on_foot", "vehicle_car"]

    def test_concept_hint_names_the_stack(self):
        intent = classify(
            "Switch between on-foot parkour and driving a car — a Control "
            "Surface Stack testbed.")
        hint = intent.concept_hint()
        assert "Control Surface Stack" in hint
        assert "on_foot" in hint and "vehicle_car" in hint

    def test_structure_hint_emits_a_must_line(self):
        intent = classify(
            "Switch between on-foot parkour and driving a car — a Control "
            "Surface Stack testbed.")
        hint = intent.structure_hint()
        assert '"locomotion_modes" MUST be' in hint
        assert '"on_foot"' in hint and '"vehicle_car"' in hint

    def test_apply_intent_fills_an_empty_registry(self):
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="X", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
        )
        assert schema.locomotion_modes == []
        intent = classify(
            "Switch between on-foot parkour and driving a car — a Control "
            "Surface Stack testbed.")
        msgs = apply_intent(schema, intent)
        assert schema.locomotion_modes == ["on_foot", "vehicle_car"]
        assert any("locomotion_modes" in m for m in msgs)

    def test_apply_intent_never_overrides_an_explicit_registry(self):
        # Strictly additive (docs/CONTROL_SURFACE_STACK.md) — an author who
        # wrote SOMETHING here, even a single entry, made a real call this
        # keyword scorer cannot see. Fill-only, never override.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="X", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            locomotion_modes=["vehicle_car"],
        )
        intent = classify(
            "Switch between on-foot parkour and driving a car — a Control "
            "Surface Stack testbed.")
        apply_intent(schema, intent)
        assert schema.locomotion_modes == ["vehicle_car"]

    def test_low_confidence_never_fills(self):
        # A concept-only echo (worth 1 per axis) must not clear the
        # min_confidence=3 gate _score_multi_mode bakes in.
        intent = classify(
            "", concept="you can switch between on-foot and driving")
        assert intent.locomotion_modes == []

    def test_real_vectorcore_prompt_now_detected(self):
        # The exact live gap this whole feature exists to close: the brief
        # that took 4 straight pipeline runs (2 different local models)
        # without the LLM ever setting locomotion_modes.
        from pathlib import Path
        text = Path("prompts/vectorcore_full_surface_control_stack.txt").read_text(
            encoding="utf-8")
        intent = classify(text)
        assert "on_foot" in intent.locomotion_modes
        assert "vehicle_car" in intent.locomotion_modes
        assert "flying" in intent.locomotion_modes
