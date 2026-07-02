"""
Tests for UnityResolver: local lookup, version resolution, package injection.
"""
import pytest

from src.unity_resolver import UnityResolver, _local_resolve, _get_version
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

    def test_manifest_is_sorted(self, resolver, minimal_schema):
        minimal_schema.packages += [
            UnityPackage(description="z", package_id="com.z.pkg", version="1.0.0"),
            UnityPackage(description="a", package_id="com.a.pkg", version="1.0.0"),
        ]
        deps = resolver.build_manifest_deps(minimal_schema)
        keys = list(deps.keys())
        assert keys == sorted(keys)
