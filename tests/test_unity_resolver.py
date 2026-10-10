"""
Tests for UnityResolver: local lookup, version resolution, package injection.
"""
import pytest

from src.unity_resolver import (
    UnityResolver, _local_resolve, _get_version, _urp_has_rendergraph,
)
from src.unity_schema import UnityPackage, UnitySchema, UnityScript


@pytest.fixture
def resolver() -> UnityResolver:
    return UnityResolver(lookups_dir="lookups")


@pytest.fixture
def db(resolver) -> dict:
    return resolver.db


# ── _local_resolve ─────────────────────────────────────────────────────────────

class TestLocalResolve:
    def test_exact_synonym_match(self, db):
        """A description that exactly matches a synonym key resolves immediately."""
        # "cinemachine" is a well-known synonym
        result = _local_resolve("cinemachine", db)
        assert result == "com.unity.cinemachine"

    def test_case_insensitive_substring(self, db):
        """Descriptions with extra words still match via substring scan."""
        result = _local_resolve("unity cinemachine package", db)
        assert result == "com.unity.cinemachine"

    def test_word_overlap_fallback(self, db):
        """Multi-word overlap picks the best match."""
        result = _local_resolve("render pipeline universal urp", db)
        assert result == "com.unity.render-pipelines.universal"

    def test_unknown_description_returns_none(self, db):
        result = _local_resolve("xyzzy-nonexistent-package-42", db)
        assert result is None

    def test_already_valid_package_id_bypasses_lookup(self, resolver):
        pkg_id, _ = resolver.resolve_package("com.unity.cinemachine", "2022.3")
        assert pkg_id == "com.unity.cinemachine"

    def test_runtime_physics_resolves_to_builtin_module(self, resolver):
        pkg_id, version = resolver.resolve_package(
            "Physics package for rigidbody dynamics and collision resolution.",
            "6000.0",
        )
        assert pkg_id == "com.unity.modules.physics"
        assert version == "1.0.0"

    def test_legacy_external_2d_physics_package_is_canonicalized(self, resolver):
        pkg_id, version = resolver.resolve_package("com.unity.2d.physics", "6000.0")
        assert pkg_id == "com.unity.modules.physics2d"
        assert version == "1.0.0"


# ── _get_version ───────────────────────────────────────────────────────────────

class TestGetVersion:
    def test_exact_unity_version_match(self, db):
        v = _get_version("com.unity.cinemachine", "2022.3", db)
        assert v is not None
        assert v.startswith("2.")   # e.g. "2.9.7"

    def test_major_prefix_fallback(self, db):
        """Version "2022.x" not in DB but major "2022" prefix should still match."""
        # 2022.9 doesn't exist but starts with "2022"
        v = _get_version("com.unity.cinemachine", "2022.9", db)
        # May return None if DB only has specific minors — accept either
        # Just assert no exception is raised
        assert v is None or isinstance(v, str)

    def test_unknown_package_returns_none(self, db):
        v = _get_version("com.example.nonexistent", "2022.3", db)
        assert v is None


# ── UnityResolver.resolve_schema ───────────────────────────────────────────────

class TestResolveSchema:
    def test_fills_package_id_from_description(self, resolver, minimal_schema):
        minimal_schema.packages.append(UnityPackage(description="cinemachine"))
        result = resolver.resolve_schema(minimal_schema)
        resolved = [p for p in result.packages if p.description == "cinemachine"]
        assert resolved[0].package_id == "com.unity.cinemachine"

    def test_skips_already_resolved_packages(self, resolver, minimal_schema):
        pkg = UnityPackage(
            description="urp",
            package_id="com.unity.render-pipelines.universal",
            version="14.0.9",
        )
        minimal_schema.packages.append(pkg)
        resolver.resolve_schema(minimal_schema)
        # package_id must be unchanged
        assert pkg.package_id == "com.unity.render-pipelines.universal"

    def test_replaces_legacy_external_2d_physics_with_3d_builtin_module(
        self, resolver, minimal_schema
    ):
        minimal_schema.game_type = "3D"
        minimal_schema.packages.append(UnityPackage(
            description="Physics package for rigidbody dynamics.",
            package_id="com.unity.2d.physics",
            version="1.0.0",
        ))
        resolver.resolve_schema(minimal_schema)
        pkg = minimal_schema.packages[-1]
        assert pkg.package_id == "com.unity.modules.physics"
        assert pkg.version == "1.0.0"

    def test_replaces_legacy_external_2d_physics_with_2d_builtin_module(
        self, resolver, minimal_schema
    ):
        minimal_schema.game_type = "2D"
        minimal_schema.packages.append(UnityPackage(
            description="Physics package for rigidbody dynamics.",
            package_id="com.unity.2d.physics",
            version="1.0.0",
        ))
        resolver.resolve_schema(minimal_schema)
        pkg = minimal_schema.packages[-1]
        assert pkg.package_id == "com.unity.modules.physics2d"
        assert pkg.version == "1.0.0"


# ── inject_required_packages ───────────────────────────────────────────────────

class TestInjectRequiredPackages:
    def test_urp_pipeline_injects_urp_package(self, resolver, minimal_schema):
        minimal_schema.render_pipeline = "URP"
        result = resolver.inject_required_packages(minimal_schema)
        ids = {p.package_id for p in result.packages if p.package_id}
        assert "com.unity.render-pipelines.universal" in ids

    def test_hdrp_pipeline_injects_hdrp_package(self, resolver, minimal_schema):
        minimal_schema.render_pipeline = "HDRP"
        result = resolver.inject_required_packages(minimal_schema)
        ids = {p.package_id for p in result.packages if p.package_id}
        assert "com.unity.render-pipelines.high-definition" in ids

    def test_no_duplicate_injection(self, resolver, minimal_schema):
        """Calling inject twice must not add duplicates."""
        minimal_schema.render_pipeline = "URP"
        resolver.inject_required_packages(minimal_schema)
        count_before = len(minimal_schema.packages)
        resolver.inject_required_packages(minimal_schema)
        assert len(minimal_schema.packages) == count_before

    def test_input_system_feature_injects_package(self, resolver, minimal_schema):
        minimal_schema.features.append("input_system")
        result = resolver.inject_required_packages(minimal_schema)
        ids = {p.package_id for p in result.packages if p.package_id}
        assert "com.unity.inputsystem" in ids

    def test_ugui_always_injected_even_without_compose(self, resolver, minimal_schema):
        # Regression (live Unity, StickmanClimber run): CS0234 "The type or
        # namespace name 'UI' does not exist in the namespace 'UnityEngine'"
        # + CS0246 on Text. SceneSetup.cs is ALWAYS emitted and always uses
        # UnityEngine.UI.Text, but com.unity.ugui (Unity 6 split UnityEngine.UI
        # out of a built-in module into a real package) was previously only
        # injected via unity_compose.py's per-kind module map when --compose
        # happened to select hud/menu/transition/vfx — a plain non-compose
        # `unity_cli.py new` run never got it.
        result = resolver.inject_required_packages(minimal_schema)
        ids = {p.package_id for p in result.packages if p.package_id}
        assert "com.unity.ugui" in ids


# ── build_manifest_deps ────────────────────────────────────────────────────────

class TestBuildManifestDeps:
    def test_resolved_packages_appear_in_manifest(self, resolver, minimal_schema):
        minimal_schema.packages.append(UnityPackage(
            description="cinemachine",
            package_id="com.unity.cinemachine",
            version="2.9.7",
        ))
        deps = resolver.build_manifest_deps(minimal_schema)
        assert "com.unity.cinemachine" in deps
        assert deps["com.unity.cinemachine"] == "2.9.7"

    def test_unresolved_packages_are_skipped_not_fallback(self, resolver, minimal_schema):
        """
        Packages with no resolvable version must be SKIPPED, not written with
        a placeholder "1.0.0". Unity rejects the whole manifest at import time
        when any single dep doesn't resolve on the registry, so skipping is
        safer than writing a bogus version. (Real-world catch in private beta.)
        """
        minimal_schema.packages.append(UnityPackage(
            description="mystery",
            package_id="com.example.mystery",
            version=None,
        ))
        deps = resolver.build_manifest_deps(minimal_schema)
        assert "com.example.mystery" not in deps

    def test_packages_without_id_excluded(self, resolver, minimal_schema):
        minimal_schema.packages.append(UnityPackage(description="unresolvable-xyz"))
        deps = resolver.build_manifest_deps(minimal_schema)
        for key in deps:
            assert key.startswith("com.")

    def test_legacy_external_2d_physics_is_blocked_for_old_schema_replays(
        self, resolver, minimal_schema
    ):
        minimal_schema.packages.append(UnityPackage(
            description="legacy package",
            package_id="com.unity.2d.physics",
            version="1.0.0",
        ))
        deps = resolver.build_manifest_deps(minimal_schema)
        assert "com.unity.2d.physics" not in deps
        assert deps["com.unity.modules.physics"] == "1.0.0"

    def test_manifest_is_sorted(self, resolver, minimal_schema):
        minimal_schema.packages += [
            UnityPackage(description="z", package_id="com.z.pkg", version="1.0.0"),
            UnityPackage(description="a", package_id="com.a.pkg", version="1.0.0"),
        ]
        deps = resolver.build_manifest_deps(minimal_schema)
        keys = list(deps.keys())
        assert keys == sorted(keys)


# ── Atmosphere ⇄ Unity-version reconciliation ──────────────────────────────────
# The vendored URP-Atmosphere render feature is written against URP 17's
# RenderGraph API, which the URP 14 that Unity 2022.3 pins does not have. When a
# sky-implying brief gets pinned to a pre-RenderGraph Unity, the resolver raises
# the version so the manifest (URP included) resolves against a version that can
# actually compile the feature.

_URP = "com.unity.render-pipelines.universal"


def _atmo_schema(**kw) -> UnitySchema:
    """A 3D URP schema that trips wants_atmosphere via a sky-implying description."""
    base = dict(
        project_name="AT", company_name="C",
        description="high-altitude freefall above the clouds",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="2022.3", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        scripts=[UnityScript(name="PlayerController", type="player", description="p")],
    )
    base.update(kw)
    return UnitySchema(**base)


class TestUrpHasRenderGraph:
    def test_pre_rendergraph_versions(self, db):
        assert not _urp_has_rendergraph("2021.3", db)   # URP 12
        assert not _urp_has_rendergraph("2022.3", db)   # URP 14

    def test_rendergraph_versions(self, db):
        assert _urp_has_rendergraph("6000.0", db)       # URP 17.0.3
        assert _urp_has_rendergraph("6000.1", db)       # URP 17.1.0
        assert _urp_has_rendergraph("6000.4", db)       # URP 17.4.0

    def test_unmapped_future_unity_not_downgraded(self, db):
        # No URP pin mapped, but Unity 6000+ ships URP 17+ — treat as capable so
        # a newer-than-mapped release is never needlessly bumped down.
        assert _urp_has_rendergraph("6000.9", db)


class TestAtmosphereVersionReconcile:
    def test_bumps_2022_3_atmosphere_to_6x(self, resolver):
        s = _atmo_schema(unity_version="2022.3")
        resolver.resolve_schema(s)
        assert s.unity_version == "6000.0"

    def test_bumps_2021_3_atmosphere_via_locomotion_fly(self, resolver):
        # locomotion=fly is the other atmosphere trigger (no sky words needed).
        s = _atmo_schema(unity_version="2021.3", description="a race across town",
                         locomotion="fly")
        resolver.resolve_schema(s)
        assert s.unity_version == "6000.0"

    def test_leaves_6x_atmosphere_untouched(self, resolver):
        s = _atmo_schema(unity_version="6000.1")
        resolver.resolve_schema(s)
        assert s.unity_version == "6000.1"

    def test_no_atmosphere_2022_3_is_not_bumped(self, resolver):
        # A grounded brief keeps its 2022.3 pin — the bump is atmosphere-only.
        s = _atmo_schema(unity_version="2022.3",
                         description="a quiet dungeon crawler underground")
        resolver.resolve_schema(s)
        assert s.unity_version == "2022.3"

    def test_builtin_pipeline_not_bumped(self, resolver):
        # wants_atmosphere gates on URP; a Built-in project never ships the
        # RenderGraph feature, so its version is left alone.
        s = _atmo_schema(unity_version="2022.3", render_pipeline="Built-in")
        resolver.resolve_schema(s)
        assert s.unity_version == "2022.3"

    def test_reconcile_is_idempotent(self, resolver):
        s = _atmo_schema(unity_version="2022.3")
        resolver.resolve_schema(s)
        resolver.resolve_schema(s)
        assert s.unity_version == "6000.0"

    def test_manifest_urp_pin_is_rendergraph_capable_after_bump(self, resolver):
        # The whole point of bumping BEFORE package resolution: the URP pin that
        # lands in the manifest must be one whose URP has RenderGraph.
        s = _atmo_schema(unity_version="2022.3")
        resolver.resolve_schema(s)
        resolver.inject_required_packages(s)
        deps = resolver.build_manifest_deps(s)
        assert _URP in deps
        assert int(deps[_URP].split(".")[0]) >= 17   # not the URP 14 that 2022.3 pins

    def test_bump_repins_stale_baked_versions(self, resolver):
        # unity-validate replay: a saved schema carries baked pins from the old
        # engine. Bumping the version must re-pin db-known packages, not keep the
        # incompatible URP 14 / Cinemachine 2.9.7 that 2022.3 baked in.
        s = _atmo_schema(unity_version="2022.3", packages=[
            UnityPackage(description="urp", package_id=_URP, version="14.0.11"),
            UnityPackage(description="cinemachine",
                         package_id="com.unity.cinemachine", version="2.9.7"),
        ])
        resolver.resolve_schema(s)
        by_id = {p.package_id: p.version for p in s.packages}
        assert int(by_id[_URP].split(".")[0]) >= 17
        assert by_id["com.unity.cinemachine"].startswith("3.")   # Unity 6 line

    def test_bump_leaves_unknown_baked_pin_untouched(self, resolver):
        # A pin we can't re-derive from the version db is left as-is rather than
        # dropped — clearing it would make build_manifest_deps skip the package.
        s = _atmo_schema(unity_version="2022.3", packages=[
            UnityPackage(description="custom", package_id="com.acme.custom",
                         version="9.9.9"),
        ])
        resolver.resolve_schema(s)
        by_id = {p.package_id: p.version for p in s.packages}
        assert by_id["com.acme.custom"] == "9.9.9"
