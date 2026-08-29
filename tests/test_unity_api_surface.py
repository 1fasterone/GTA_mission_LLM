"""The Unity API member surface and the CS0117/CS1061 check it feeds.

Ported from the GTA backend's `lint_member_references` +
`lookups/gta_api_surface.json`, which Unity had no equivalent of. Live errors
this exists to catch, both from one Gridfall build:

    CS0117  'Material' does not contain a definition for 'mainTextureMode'
    CS1061  'Touchscreen' does not contain a definition for ...

Nearly every test here runs against a SYNTHETIC surface rather than the frozen
artifact: the extraction reads a local Unity install, CI has none, and the
behaviour under test is the checker's, not the engine's.
"""
from __future__ import annotations

import json

import pytest

from src.micro.code_fields import (find_invalid_member_uses,
                                   lint_invalid_member_uses)
from src.unity_api_surface import (build_surface, load_api_surface,
                                   members_for, resolve_type,
                                   simple_name_index, universal_members,
                                   write_api_surface)


@pytest.fixture
def surface() -> dict:
    """Enough shape to exercise every rule: a real type, its universal bases,
    a too-thin type, and a name owned by two namespaces."""
    return {
        "UnityEngine.Material": ["color", "mainTexture", "mainTextureOffset",
                                 "SetFloat", "SetColor", "shader"],
        "UnityEngine.Transform": ["position", "rotation", "localScale",
                                  "Translate", "Rotate", "LookAt"],
        "UnityEngine.Object": ["name", "hideFlags", "GetInstanceID", "Destroy"],
        "UnityEngine.Component": ["gameObject", "transform", "GetComponent"],
        "UnityEngine.Behaviour": ["enabled", "isActiveAndEnabled"],
        "UnityEngine.MonoBehaviour": ["StartCoroutine", "Invoke"],
        "UnityEngine.Thin": ["a", "b"],                       # below _MIN_SURFACE
        "UnityEngine.Cursor": ["visible", "lockState", "SetCursor", "x", "y"],
        "UnityEngine.UIElements.Cursor": ["hotspot", "texture", "a", "b", "c"],
    }


# ── Extraction ───────────────────────────────────────────────────────────────

class TestBuildSurface:
    def _xml(self, tmp_path, name: str, body: str):
        (tmp_path / name).write_text(
            f'<?xml version="1.0"?><doc><members>{body}</members></doc>',
            encoding="utf-8")

    def test_parses_properties_fields_methods_and_events(self, tmp_path):
        self._xml(tmp_path, "UnityEngine.CoreModule.xml", """
          <member name="T:UnityEngine.Material"><summary>x</summary></member>
          <member name="P:UnityEngine.Material.color"/>
          <member name="F:UnityEngine.Material.someField"/>
          <member name="E:UnityEngine.Material.someEvent"/>
          <member name="M:UnityEngine.Material.SetFloat(System.String,System.Single)"/>
        """)
        s = build_surface(tmp_path)
        # The type entry itself is not a member; the method loses its signature.
        assert s["UnityEngine.Material"] == ["SetFloat", "color", "someEvent",
                                             "someField"]

    def test_constructors_are_not_members(self, tmp_path):
        self._xml(tmp_path, "UnityEngine.CoreModule.xml", """
          <member name="M:UnityEngine.Material.#ctor(UnityEngine.Shader)"/>
          <member name="P:UnityEngine.Material.color"/>
        """)
        assert build_surface(tmp_path)["UnityEngine.Material"] == ["color"]

    def test_non_unityengine_xml_is_ignored(self, tmp_path):
        self._xml(tmp_path, "SomeOtherThing.xml",
                  '<member name="P:Other.Type.member"/>')
        assert build_surface(tmp_path) == {}

    def test_output_is_deterministic(self, tmp_path):
        self._xml(tmp_path, "UnityEngine.CoreModule.xml", """
          <member name="P:UnityEngine.B.z"/>
          <member name="P:UnityEngine.A.y"/>
          <member name="P:UnityEngine.A.x"/>
        """)
        assert json.dumps(build_surface(tmp_path)) == json.dumps(
            build_surface(tmp_path))
        assert list(build_surface(tmp_path)) == ["UnityEngine.A", "UnityEngine.B"]


class TestFreezeAndLoad:
    def test_round_trips_through_the_lookup(self, tmp_path, surface):
        write_api_surface(surface, tmp_path, unity_version="6000.1.4f1")
        assert load_api_surface(tmp_path) == surface

    def test_missing_artifact_loads_as_none(self, tmp_path):
        # None is a first-class answer — every consumer treats it as no opinion.
        assert load_api_surface(tmp_path) is None

    def test_corrupt_artifact_loads_as_none(self, tmp_path):
        p = tmp_path / "unity" / "api_surface.json"
        p.parent.mkdir(parents=True)
        p.write_text("{not json", encoding="utf-8")
        assert load_api_surface(tmp_path) is None


# ── Type resolution ──────────────────────────────────────────────────────────

class TestResolveType:
    def test_simple_name(self, surface):
        assert resolve_type("Material", surface) == "UnityEngine.Material"

    def test_fully_qualified_name(self, surface):
        assert resolve_type("UnityEngine.Material", surface) == "UnityEngine.Material"

    def test_generic_element_type(self, surface):
        assert resolve_type("List<Material>", surface) == "UnityEngine.Material"

    def test_array_and_multidim_array(self, surface):
        assert resolve_type("Material[]", surface) == "UnityEngine.Material"
        assert resolve_type("Material[,]", surface) == "UnityEngine.Material"

    def test_nullable_suffix(self, surface):
        assert resolve_type("Material?", surface) == "UnityEngine.Material"

    def test_unknown_type_abstains(self, surface):
        # A project's own class — no opinion, never a guess.
        assert resolve_type("GridLogic", surface) == ""

    def test_ambiguous_simple_name_abstains(self, surface):
        # `Cursor` is UnityEngine and UIElements — the same tie-abstains rule
        # the objective-target and board derivations use.
        assert resolve_type("Cursor", surface) == ""
        assert "Cursor" not in simple_name_index(surface)

    def test_empty_type(self, surface):
        assert resolve_type("", surface) == ""


class TestMembersFor:
    def test_includes_inherited_universal_members(self, surface):
        m = members_for("UnityEngine.Material", surface)
        # `name` is documented on Object, never on Material — flagging it would
        # be the obvious false positive.
        assert "name" in m and "GetInstanceID" in m
        assert "gameObject" in m and "enabled" in m
        assert "ToString" in m           # System.Object

    def test_keeps_the_types_own_members(self, surface):
        assert "mainTexture" in members_for("UnityEngine.Material", surface)

    def test_thin_surface_abstains(self, surface):
        # Below _MIN_SURFACE: a partial extraction would flag every real use.
        assert members_for("UnityEngine.Thin", surface) is None

    def test_unknown_type_abstains(self, surface):
        assert members_for("UnityEngine.Nope", surface) is None

    def test_universal_members_come_from_the_surface(self, surface):
        assert {"name", "gameObject", "enabled", "StartCoroutine"} <= universal_members(surface)


# ── The check itself ─────────────────────────────────────────────────────────

class TestFindInvalidMemberUses:
    def test_flags_the_live_material_error(self, surface):
        bad = find_invalid_member_uses(
            "_mat.mainTextureMode = 1;",
            field_types={"_mat": "Material"}, surface=surface)
        assert bad == {0: {"Material.mainTextureMode"}}

    def test_real_member_passes(self, surface):
        assert find_invalid_member_uses(
            "_mat.mainTexture = tex;",
            field_types={"_mat": "Material"}, surface=surface) == {}

    def test_inherited_member_passes(self, surface):
        assert find_invalid_member_uses(
            '_mat.name = "x"; int i = _mat.GetInstanceID();',
            field_types={"_mat": "Material"}, surface=surface) == {}

    def test_unknown_type_is_never_flagged(self, surface):
        assert find_invalid_member_uses(
            "_grid.WhateverInvented();",
            field_types={"_grid": "GridLogic"}, surface=surface) == {}

    def test_no_surface_is_a_no_op(self, surface):
        assert find_invalid_member_uses(
            "_mat.mainTextureMode = 1;",
            field_types={"_mat": "Material"}, surface=None) == {}

    def test_a_different_field_with_the_same_member_name_is_unaffected(self, surface):
        # Keyed off the FIELD, so an identically named member on another
        # object is not collateral.
        bad = find_invalid_member_uses(
            "other.mainTextureMode = 1;",
            field_types={"_mat": "Material"}, surface=surface)
        assert bad == {}

    def test_member_access_through_a_longer_identifier_is_not_matched(self, surface):
        # `_material` must not match the `_mat` field.
        assert find_invalid_member_uses(
            "_material.mainTextureMode = 1;",
            field_types={"_mat": "Material"}, surface=surface) == {}

    def test_reports_the_line_index(self, surface):
        src = "int a = 1;\n_mat.color = c;\n_mat.bogusThing = 2;\n"
        assert find_invalid_member_uses(
            src, field_types={"_mat": "Material"}, surface=surface) == {
                2: {"Material.bogusThing"}}


class TestLintInvalidMemberUses:
    def test_neutralizes_the_offending_statement_only(self, surface):
        src = ("_mat.mainTexture = tex;\n"
               "_mat.mainTextureMode = 1;\n"
               "_t.position = p;\n")
        out, names = lint_invalid_member_uses(
            src, field_types={"_mat": "Material", "_t": "Transform"},
            surface=surface)
        assert names == {"Material.mainTextureMode"}
        lines = out.splitlines()
        assert lines[0] == "_mat.mainTexture = tex;"
        assert lines[1].lstrip().startswith("//")
        assert "mainTextureMode" in lines[1]
        assert lines[2] == "_t.position = p;"

    def test_clean_body_is_returned_unchanged(self, surface):
        src = "_mat.color = Color.white;\n_t.Translate(v);\n"
        out, names = lint_invalid_member_uses(
            src, field_types={"_mat": "Material", "_t": "Transform"},
            surface=surface)
        assert (out, names) == (src, set())

    def test_no_surface_leaves_the_body_alone(self, surface):
        src = "_mat.mainTextureMode = 1;\n"
        assert lint_invalid_member_uses(
            src, field_types={"_mat": "Material"}, surface=None) == (src, set())


# ── The frozen artifact this repo ships ──────────────────────────────────────

class TestFrozenArtifact:
    def test_the_committed_surface_answers_the_live_case(self):
        s = load_api_surface("lookups")
        if s is None:
            pytest.skip("api_surface.json not built — run: "
                        "python main.py unity-api-surface")
        m = members_for("UnityEngine.Material", s)
        assert m is not None
        assert "mainTexture" in m and "SetFloat" in m
        assert "mainTextureMode" not in m        # the live CS0117
        assert "name" in m                       # inherited from Object
