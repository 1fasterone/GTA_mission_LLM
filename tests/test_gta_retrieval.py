"""
Ranking-level checks against a real gta5 index.

Unlike tests/test_gta_corpus_semantics.py — which is pure and always runs —
these need a built index and the embedding model, so they skip when either is
missing (CI has neither). Run them after `python main.py rag-build`:

    pytest tests/test_gta_retrieval.py -v

They encode the probes that exposed the corpus problems in the first place:
the right model has to win, and — the part that matters more — nonsense has to
be *rejected*, which a corpus full of shared boilerplate could never do.
"""
from __future__ import annotations

import pytest

from rag.query import RagClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def rag():
    client = RagClient()
    if not client.is_ready(domain="gta5"):
        pytest.skip("gta5 index not built — run: python main.py rag-build")
    try:
        client.search("warmup", domain="gta5", kind="ped", n=1)
    except Exception as exc:  # embedding model unavailable / offline
        pytest.skip(f"embedding model unavailable: {exc}")
    return client


def top(rag, query, kind, n=5):
    return [h["canonical"] for h in rag.search(query, domain="gta5", kind=kind, n=n)]


# ── the wrong answers that used to rank ───────────────────────────────────────

def test_a_dog_is_not_a_bystander(rag):
    """A_C_Westy came back at rank 2 for this exact query."""
    hits = top(rag, "civilian bystander walking down the street", "ped")
    animals = [h for h in hits if h.lower().startswith("a_c_")]
    assert not animals, f"animals returned for a human query: {animals}"


def test_cop_query_returns_law_enforcement(rag):
    hits = rag.search("corrupt cop on the take", domain="gta5", kind="ped", n=5)
    factions = [h.get("tags", "") for h in hits]
    assert any("faction.law" in f for f in factions), \
        f"no law-enforcement ped in top 5: {[h['canonical'] for h in hits]}"


def test_blunt_weapon_query_does_not_return_stationery(rag):
    hits = top(rag, "baseball bat", "weapon", n=3)
    assert hits[0] == "w_me_bat"
    assert not any("papers" in h.lower() for h in hits), hits


def test_siren_beats_police_dialogue(rag):
    """The sound wins over a dialogue bank that merely says 'police'."""
    hits = rag.search("police siren wail", domain="gta5", kind="audio", n=5)
    top_hit = hits[0]
    assert top_hit["kind"] == "audio"
    assert "subject.siren" in top_hit.get("tags", ""), \
        f"top audio hit is not a siren: {top_hit['canonical']} ({top_hit.get('tags')})"


# ── the answers that were already right must stay right ───────────────────────

@pytest.mark.parametrize("query,kind,expected", [
    ("police helicopter",   "vehicle", "polmav"),
    ("drug package",        "prop",    "prop_drug_package"),
    ("baseball bat",        "weapon",  "w_me_bat"),
])
def test_known_good_resolutions_hold(rag, query, kind, expected):
    assert expected in top(rag, query, kind, n=3)


def test_guard_scenario_still_resolves(rag):
    assert any("GUARD_STAND" in h for h in top(rag, "guard standing watch", "scenario", n=3))


def test_protagonist_is_reachable_by_name(rag):
    # the dumps ship this as Player_Two; canonical keeps their casing
    assert "player_two" in [h.lower() for h in top(rag, "Trevor Philips", "ped", n=3)]


# ── rejection: the property the flat corpus could not provide ─────────────────

NONSENSE = [
    "a quadratic equation about tax policy",
    "photosynthesis in deciduous trees",
    "compiler register allocation strategy",
    "recipe for sourdough starter",
]

# Kinds whose true and false distances actually separate — see rag/_calibrate.py.
# ped, prop and scenario overlap: their weakest true match ("homeless man" at
# 0.486) sits above the nearest nonsense (0.418), so no top-1 distance divides
# them and asserting rejection there would be asserting a fiction.
SEPARABLE = ["vehicle", "weapon", "audio", "animation"]


@pytest.mark.parametrize("kind", SEPARABLE)
@pytest.mark.parametrize("query", NONSENSE)
def test_nonsense_is_rejected(rag, kind, query):
    """
    Every probe used to land under the flat 0.55 threshold, so _best() could
    never say "no match" — which is what the FAIL-FAST doctrine needs it to say.
    """
    limit = rag.threshold_for(kind)
    hit   = rag.search(query, domain="gta5", kind=kind, n=1)[0]
    assert hit["distance"] >= limit, (
        f"{query!r} matched {hit['canonical']} at {hit['distance']:.3f}, "
        f"under the {limit} threshold for {kind} — it would be accepted")


@pytest.mark.parametrize("kind,query", [
    ("ped", "swat officer"), ("ped", "homeless man"), ("ped", "a stray dog"),
    ("vehicle", "police car"), ("vehicle", "getaway van"),
    ("prop", "duffel bag of cash"), ("prop", "office chair"),
    ("weapon", "molotov cocktail"), ("audio", "crowd cheering"),
    ("scenario", "filming with a phone"), ("animation", "sitting in a chair"),
])
def test_real_queries_are_never_rejected(rag, kind, query):
    """The invariant that holds for every kind, overlapping or not: a plausible
    mission query must resolve. A false reject hard-fails generation."""
    limit = rag.threshold_for(kind)
    hit   = rag.search(query, domain="gta5", kind=kind, n=1)[0]
    assert hit["distance"] < limit, (
        f"{query!r} -> {hit['distance']:.3f}, at or over the {limit} "
        f"threshold for {kind} — generation would hard-fail")
