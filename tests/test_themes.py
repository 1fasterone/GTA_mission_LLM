"""Tests for the theme template layer."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from src import themes as th
from src.prompts import CONCEPT_USER, SCHEMA_FILL_USER
from src.unity_prompts import UNITY_CONCEPT_USER, UNITY_SCHEMA_FILL_USER


THEMES_DIR = Path(__file__).resolve().parent.parent / "themes"


# ── Loader / listing ─────────────────────────────────────────────────────────

def test_load_known_theme():
    theme = th.load_theme("zombie_apocalypse")
    assert theme.name == "zombie_apocalypse"
    assert theme.display.startswith("Zombie")
    assert theme.twist_seeds, "zombie theme should ship twist seeds"
    assert "gta" in theme.pipelines


def test_list_themes_returns_all_starter_files():
    found = {t.name for t in th.list_themes()}
    expected = {
        "zombie_apocalypse", "underwater_city", "superhero_flying",
        "time_travel", "cyberpunk_neon", "gravity_control",
        "medieval_sword", "alien_invasion", "jurassic_safari",
        "hyper_physics", "race",
    }
    assert expected.issubset(found), f"missing themes: {expected - found}"


def test_pick_random_theme_deterministic():
    a = th.pick_random_theme(rng=random.Random(42))
    b = th.pick_random_theme(rng=random.Random(42))
    assert a.name == b.name


# ── Auto-detect ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prompt,expected", [
    ("a mission with infected and survivors at the docks",  "zombie_apocalypse"),
    ("explore a drowned city underwater with divers",       "underwater_city"),
    ("a cyberpunk neon hacker job downtown",                "cyberpunk_neon"),
    ("knights and a medieval sword duel",                   "medieval_sword"),
    ("an alien invasion of the military base",              "alien_invasion"),
    ("a dinosaur safari at the reserve",                    "jurassic_safari"),
    ("street race lap around the airport circuit",          "race"),
])
def test_autodetect_picks_expected_theme(prompt, expected):
    theme = th.autodetect_theme(prompt)
    assert theme is not None, f"expected {expected!r}, got None"
    assert theme.name == expected


def test_autodetect_returns_none_for_neutral_prompt():
    # Nothing in this prompt should match any theme's keywords.
    assert th.autodetect_theme("xyz") is None


# ── Mashup ───────────────────────────────────────────────────────────────────

def test_mashup_unions_vocab_and_references_both():
    a = th.load_theme("medieval_sword")
    b = th.load_theme("underwater_city")
    merged = th.mashup(a, b, rng=random.Random(0))
    assert a.display in merged.concept_bias
    assert b.display in merged.concept_bias
    # Union of vocabularies
    assert set(a.vocabulary.peds).issubset(set(merged.vocabulary.peds))
    assert set(b.vocabulary.peds).issubset(set(merged.vocabulary.peds))
    # Mashup name combines both
    assert "medieval_sword" in merged.name and "underwater_city" in merged.name


# ── Concept-bias rendering ───────────────────────────────────────────────────

def test_render_concept_bias_includes_theme_twist_and_avoid():
    theme = th.load_theme("zombie_apocalypse")
    block, twist = th.render_concept_bias(theme, rng=random.Random(1))
    assert "THEME:" in block
    assert "REQUIRED TWIST" in block
    assert twist and twist in block
    assert "AVOID THESE CLICHÉS" in block
    assert "ORIGINALITY DIRECTIVE" in block


def test_render_concept_bias_is_deterministic_with_seed():
    theme = th.load_theme("zombie_apocalypse")
    _, twist_a = th.render_concept_bias(theme, rng=random.Random(7))
    _, twist_b = th.render_concept_bias(theme, rng=random.Random(7))
    assert twist_a == twist_b


# ── resolve_theme (the integration entry point) ──────────────────────────────

def test_resolve_theme_explicit_name():
    theme, _, meta = th.resolve_theme(user_prompt="anything", theme_name="zombie_apocalypse", seed=3)
    assert theme is not None and theme.name == "zombie_apocalypse"
    assert meta["theme"] == "zombie_apocalypse"
    assert meta["seed"] == 3


def test_resolve_theme_no_match_returns_none():
    theme, _, meta = th.resolve_theme(user_prompt="xyz")
    assert theme is None
    assert meta["theme"] is None


def test_resolve_theme_mashup():
    theme, _, meta = th.resolve_theme(
        user_prompt="anything",
        theme_name="medieval_sword",
        mashup_with="underwater_city",
        seed=11,
    )
    assert theme is not None
    assert "medieval_sword" in theme.name and "underwater_city" in theme.name
    assert meta["mashup_with"] == "underwater_city"


# ── Prompt-injection smoke tests ─────────────────────────────────────────────

def test_concept_user_renders_empty_when_no_theme():
    """Without a theme, the prompt should still format cleanly with an empty block."""
    rendered = CONCEPT_USER.format(user_prompt="test idea", theme_block="", archetype_block="")
    assert "test idea" in rendered
    assert "THEME:" not in rendered


def test_concept_user_includes_theme_block_when_present():
    theme = th.load_theme("zombie_apocalypse")
    block, _ = th.render_concept_bias(theme, rng=random.Random(2))
    rendered = CONCEPT_USER.format(user_prompt="test idea", theme_block=block, archetype_block="")
    assert "THEME: Zombie Apocalypse Survival" in rendered
    assert "test idea" in rendered


def test_schema_fill_renders_with_empty_hints():
    rendered = SCHEMA_FILL_USER.format(concept="a story", schema_hints="", structure_block="")
    assert "a story" in rendered


def test_unity_concept_includes_theme_block():
    theme = th.load_theme("underwater_city")
    block, _ = th.render_concept_bias(theme, rng=random.Random(2))
    rendered = UNITY_CONCEPT_USER.format(user_prompt="test", theme_block=block)
    assert "Underwater City" in rendered


def test_unity_schema_fill_empty_hints():
    rendered = UNITY_SCHEMA_FILL_USER.format(concept="brief", schema_hints="",
                                             multimodal_block="", brief_block="")
    assert "brief" in rendered
