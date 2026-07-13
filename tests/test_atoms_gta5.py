"""
Tests for the GTA5 atom backend: full snippet coverage of every gta5-capable
atom, the engine-neutral dispatcher, gta5_tick_body assembly, and a native-hash
allowlist cross-check so no snippet invents a native.
"""
import json
import re
from pathlib import Path

import pytest

from src.atoms import ALL_ATOMS, ATOM_REGISTRY, get_atom
from src.atoms.backends import (
    atom_snippet,
    gta5_atom_snippet,
    gta5_tick_body,
    unity_atom_snippet,
)
from src.atoms.backends.gta5 import _SNIPPETS as GTA5_SNIPPETS


def _default_args(atom_name: str) -> dict:
    adef = ATOM_REGISTRY[atom_name]
    return {p.name: p.default for p in adef.params}


GTA5_ATOMS = [a for a in ALL_ATOMS if "gta5" in a.engines]
UNITY_ATOMS = [a for a in ALL_ATOMS if "unity" in a.engines]


# ══════════════════════════════════════════════════════════════════════════════
# Engines-list integrity
# ══════════════════════════════════════════════════════════════════════════════

class TestEnginesIntegrity:
    def test_engines_values_are_known(self):
        for a in ALL_ATOMS:
            assert set(a.engines) <= {"unity", "gta5"}, a.name
            assert a.engines, f"{a.name} has an empty engines list"

    def test_every_gta5_atom_has_a_snippet(self):
        # FAIL-FAST doctrine: gta5-capable means a real snippet exists.
        missing = [a.name for a in GTA5_ATOMS if a.name not in GTA5_SNIPPETS]
        assert missing == [], f"gta5-engine atoms without snippets: {missing}"

    def test_no_snippet_for_unity_only_atoms(self):
        # The inverse: nothing in the gta5 table claims a Unity-only atom.
        unity_only = {a.name for a in ALL_ATOMS if a.engines == ["unity"]}
        leaked = sorted(set(GTA5_SNIPPETS) & unity_only)
        assert leaked == [], f"gta5 snippets for Unity-only atoms: {leaked}"

    def test_unity_parity_unbroken(self):
        # Guard: no atom that HAS a unity snippet lost it, and every gta5-capable
        # atom is also unity-renderable (the shared 26 are dual-engine). Most
        # controller atoms are deliberately absent from unity's snippet table —
        # they're consumed by the controller micro templates instead.
        from src.atoms.backends.unity import _SNIPPETS as UNITY_SNIPPETS
        for a in GTA5_ATOMS:
            assert a.name in UNITY_SNIPPETS, f"dual-engine atom {a.name} lost unity"
            snippet = unity_atom_snippet(a.name, _default_args(a.name))
            assert "TODO" not in snippet, f"unity snippet missing for {a.name}"

    def test_wiring_atoms_stay_unity_only(self):
        wiring = [a for a in ALL_ATOMS if a.category == "wiring"]
        assert wiring, "wiring category vanished?"
        for a in wiring:
            assert a.engines == ["unity"], a.name
            assert a.name not in GTA5_SNIPPETS


# ══════════════════════════════════════════════════════════════════════════════
# Snippet quality — every gta5 atom renders real, balanced C#
# ══════════════════════════════════════════════════════════════════════════════

class TestGta5Snippets:
    @pytest.mark.parametrize("atom", [a.name for a in GTA5_ATOMS])
    def test_snippet_renders_real_code(self, atom):
        snippet = gta5_atom_snippet(atom, _default_args(atom))
        assert snippet.strip(), f"{atom}: empty snippet"
        code_lines = [l for l in snippet.splitlines()
                      if l.strip() and not l.strip().startswith("//")]
        assert code_lines, f"{atom}: comment-only snippet"
        assert "TODO" not in snippet, f"{atom}: stub leaked"
        assert snippet.count("{") == snippet.count("}"), f"{atom}: unbalanced braces"
        assert snippet.count("(") == snippet.count(")"), f"{atom}: unbalanced parens"
        assert "UnityEngine" not in snippet, f"{atom}: Unity leak"

    @pytest.mark.parametrize("atom", [a.name for a in GTA5_ATOMS])
    def test_snippet_deterministic(self, atom):
        args = _default_args(atom)
        assert gta5_atom_snippet(atom, args) == gta5_atom_snippet(atom, args)

    def test_unknown_atom_raises(self):
        with pytest.raises(ValueError, match="no GTA5 backend"):
            gta5_atom_snippet("Jump")           # controller — Unity-only

    def test_equip_weapon_resolves_hash_at_runtime(self):
        # Regression: Hash.WEAPON_PISTOL is not a native — must use GET_HASH_KEY.
        snippet = gta5_atom_snippet("EquipWeapon", {"weapon": "carbine"})
        assert "GET_HASH_KEY" in snippet
        assert '"weapon_carbine"' in snippet
        assert "Hash.WEAPON_" not in snippet


# ══════════════════════════════════════════════════════════════════════════════
# Native-hash allowlist cross-check
# ══════════════════════════════════════════════════════════════════════════════

_HASH_RE = re.compile(r"\bHash\.([A-Z0-9_]+)")


class TestHashAllowlist:
    def test_all_emitted_natives_exist(self):
        allowlist_path = Path("lookups/gta_hash_allowlist.json")
        if not allowlist_path.exists():
            pytest.skip("lookups/gta_hash_allowlist.json not present")
        allowed = set(json.loads(allowlist_path.read_text(encoding="utf-8")))
        invented: dict[str, set[str]] = {}
        for a in GTA5_ATOMS:
            snippet = gta5_atom_snippet(a.name, _default_args(a.name))
            bad = {h for h in _HASH_RE.findall(snippet) if h not in allowed}
            if bad:
                invented[a.name] = bad
        assert invented == {}, f"snippets reference non-existent natives: {invented}"


# ══════════════════════════════════════════════════════════════════════════════
# Engine-neutral dispatcher
# ══════════════════════════════════════════════════════════════════════════════

class TestDispatcher:
    def test_dispatches_both_engines(self):
        u = atom_snippet("unity", "Teleport", {"x": 1, "y": 2, "z": 3})
        g = atom_snippet("gta5", "Teleport", {"x": 1, "y": 2, "z": 3})
        assert "transform.position" in u
        assert "Game.Player.Character.Position" in g

    def test_resolves_aliases(self):
        assert atom_snippet("gta5", "warp", {}) == gta5_atom_snippet("Teleport", {})

    def test_unknown_engine_raises(self):
        with pytest.raises(ValueError, match="unknown engine"):
            atom_snippet("godot", "Teleport")

    def test_unknown_atom_raises(self):
        with pytest.raises(ValueError, match="unknown atom"):
            atom_snippet("gta5", "FrobnicateWidget")

    def test_engine_mismatch_raises(self):
        with pytest.raises(ValueError, match="not available on engine"):
            atom_snippet("gta5", "Jump")        # controller atom, Unity-only


# ══════════════════════════════════════════════════════════════════════════════
# gta5_tick_body — sequence assembly
# ══════════════════════════════════════════════════════════════════════════════

class TestTickBody:
    def test_repeated_atoms_are_brace_scoped(self):
        # Two MoveTo both declare `var _target` — must land in separate scopes.
        body = gta5_tick_body([
            {"atom": "MoveTo", "args": {"x": 1.0}, "label": "first leg"},
            {"atom": "MoveTo", "args": {"x": 2.0}, "label": "second leg"},
        ])
        assert body.count("var _target") == 2
        assert body.count("{") == body.count("}")
        # Each declaration sits inside its own wrapper block.
        assert body.index("{") < body.index("var _target")

    def test_flags_preamble_only_when_needed(self):
        without = gta5_tick_body([{"atom": "Wait", "args": {"seconds": 1}}])
        assert "_flags" not in without
        with_flags = gta5_tick_body([
            {"atom": "SetFlag", "args": {"flag": "armed", "value": True}},
            {"atom": "ConditionCheck", "args": {"flag": "armed", "expect": True}},
        ])
        assert with_flags.count("var _flags = new Dictionary<string, bool>();") == 1
        assert '_flags["armed"] = true;' in with_flags

    def test_trigger_event_namespaces_flag(self):
        body = gta5_tick_body([{"atom": "TriggerEvent", "args": {"event": "AlarmRaised"}}])
        assert '_flags["event:AlarmRaised"] = true;' in body

    def test_condition_check_aborts_sequence(self):
        body = gta5_tick_body([
            {"atom": "ConditionCheck", "args": {"flag": "go", "expect": True}},
            {"atom": "Wait", "args": {"seconds": 1}},
        ])
        assert "return;" in body
        assert body.index("return;") < body.index("Script.Wait(1000);")

    def test_loop_wraps_body_and_yields(self):
        body = gta5_tick_body(
            [{"atom": "Wait", "args": {"seconds": 2}}], loop=True)
        assert body.startswith("while (true)")
        assert "Script.Wait(0);" in body          # yield each iteration
        assert "Script.Wait(2000);" in body
        assert body.count("{") == body.count("}")

    def test_flags_preamble_stays_outside_loop(self):
        body = gta5_tick_body(
            [{"atom": "SetFlag", "args": {"flag": "f"}}], loop=True)
        assert body.index("var _flags") < body.index("while (true)")

    def test_labels_become_comments(self):
        body = gta5_tick_body(
            [{"atom": "Wait", "args": {"seconds": 1}, "label": "hold position"}])
        assert "// hold position" in body

    def test_deterministic(self):
        invs = [{"atom": "SpawnEntity", "args": {"entity_type": "a_m_y_beach_01"}},
                {"atom": "PlaySFX", "args": {"sound": "CHECKPOINT_NORMAL"}}]
        assert gta5_tick_body(invs) == gta5_tick_body(invs)


# ══════════════════════════════════════════════════════════════════════════════
# Registry sanity for the port
# ══════════════════════════════════════════════════════════════════════════════

class TestPortCoverage:
    def test_original_26_atoms_are_gta5_capable(self):
        # The 7 original categories (26 atoms) are the engine-agnostic core.
        core = [a for a in ALL_ATOMS if a.category in
                ("movement", "animation", "camera", "audio", "fx", "logic", "entity")]
        assert len(core) == 26
        for a in core:
            assert "gta5" in a.engines, f"{a.name} lost gta5 capability"

    def test_get_atom_alias_lookup_still_works(self):
        assert get_atom("teleport").name == "Teleport"
        assert get_atom("Teleport").name == "Teleport"
