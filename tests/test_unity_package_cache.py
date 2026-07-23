"""
Unity package cache tests — src/unity_package_cache.py.

Never touches the network or the real global cache: the registry is faked and
every cache root is a tmp_path. The behaviours worth locking in are the ones
that are expensive to get wrong on a user's machine — priming must never
clobber an existing editor-downloaded package, a partial download must never
be mistaken for a good cache entry, and a hostile tarball must not escape the
cache directory.
"""

import hashlib
import io
import json
import tarfile

import pytest

from src.unity_package_cache import (
    BUILTIN_PREFIX,
    CacheReport,
    PackagePin,
    build_lock,
    download,
    ensure_cached,
    global_npm_dir,
    global_package_dir,
    is_globally_cached,
    local_entry_dir,
    prime,
    resolve_closure,
    unity_cache_root,
)


def _tarball(files: dict[str, str] | None = None, root: str = "package") -> bytes:
    """Build a registry-shaped .tgz: everything under a single `package/` root."""
    files = files or {"package.json": '{"name":"x","version":"1.0.0"}'}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeRegistry:
    """Stands in for packages.unity.com. Records what was fetched."""

    def __init__(self, graph: dict[str, dict]):
        self.graph = graph
        self.tarballs: dict[str, bytes] = {}
        self.fetches: list[str] = []
        for name, versions in graph.items():
            for ver in versions:
                self.tarballs[f"https://dl/{name}-{ver}.tgz"] = _tarball()

    def http_get(self, url, timeout=60):
        self.fetches.append(url)
        if url in self.tarballs:
            return self.tarballs[url]
        name = url.rsplit("/", 1)[-1]
        if name not in self.graph:
            raise OSError(f"404 {name}")
        versions = {}
        for ver, deps in self.graph[name].items():
            blob = self.tarballs[f"https://dl/{name}-{ver}.tgz"]
            versions[ver] = {
                "dist": {"tarball": f"https://dl/{name}-{ver}.tgz",
                         "shasum": hashlib.sha1(blob).hexdigest()},
                "dependencies": deps,
            }
        return json.dumps({"name": name, "versions": versions}).encode()


@pytest.fixture
def registry(monkeypatch):
    reg = FakeRegistry({
        "com.unity.cinemachine": {"3.1.7": {"com.unity.splines": "2.0.0",
                                            "com.unity.modules.imgui": "1.0.0"}},
        "com.unity.splines":     {"2.0.0": {"com.unity.mathematics": "1.2.1"}},
        "com.unity.mathematics": {"1.2.1": {}},
        "com.unity.burst":       {"1.4.1": {}},
    })
    monkeypatch.setattr("src.unity_package_cache._http_get", reg.http_get)
    return reg


# ── Closure ───────────────────────────────────────────────────────────────────

class TestResolveClosure:
    def test_walks_transitive_dependencies(self, registry, tmp_path):
        """manifest.json lists only direct deps — caching those alone still
        leaves Unity fetching the rest of the graph."""
        pins = resolve_closure({"com.unity.cinemachine": "3.1.7"}, tmp_path)
        assert set(pins) == {"com.unity.cinemachine", "com.unity.splines",
                             "com.unity.mathematics"}

    def test_depth_is_recorded_for_the_lock_file(self, registry, tmp_path):
        pins = resolve_closure({"com.unity.cinemachine": "3.1.7"}, tmp_path)
        assert pins["com.unity.cinemachine"].depth == 0
        assert pins["com.unity.splines"].depth == 1
        assert pins["com.unity.mathematics"].depth == 2

    def test_builtin_modules_are_skipped(self, registry, tmp_path):
        """com.unity.modules.* ship inside the editor — never downloaded."""
        pins = resolve_closure({"com.unity.modules.imgui": "1.0.0",
                                "com.unity.burst": "1.4.1"}, tmp_path)
        assert set(pins) == {"com.unity.burst"}
        assert not any(p.name.startswith(BUILTIN_PREFIX) for p in pins.values())

    @pytest.mark.parametrize("version", [
        "https://github.com/x/y.git", "git+https://x/y.git", "file:../local",
    ])
    def test_non_registry_sources_are_skipped(self, registry, tmp_path, version):
        assert resolve_closure({"com.example.thing": version}, tmp_path) == {}

    def test_unpublished_version_is_reported_as_editor_bundled_not_failed(
            self, registry, tmp_path):
        """URP 17.x is the live case: the registry's history stops at 8.3.1
        because Unity 6 resolves it from the editor install. Not an error."""
        report = CacheReport()
        pins = resolve_closure({"com.unity.burst": "99.0.0"}, tmp_path, report)
        assert pins == {}
        assert report.not_on_registry == ["com.unity.burst@99.0.0"]
        assert report.failed == {}

    def test_a_package_reached_twice_is_resolved_once(self, registry, tmp_path):
        """A failed lookup never lands in `resolved`, so without a separate
        attempted-set a second parent re-fetches and double-reports it."""
        # Two parents both depend on splines, and the pinned version is not
        # published — the live shape of com.unity.ugui@1.0.0.
        report = CacheReport()
        resolve_closure({"com.unity.splines": "9.9.9", "com.unity.burst": "1.4.1"},
                        tmp_path, report)
        assert report.not_on_registry.count("com.unity.splines@9.9.9") == 1

    def test_network_failure_degrades_to_a_miss(self, monkeypatch, tmp_path):
        def boom(url, timeout=60):
            raise OSError("network down")
        monkeypatch.setattr("src.unity_package_cache._http_get", boom)
        report = CacheReport()
        assert resolve_closure({"com.unity.burst": "1.4.1"}, tmp_path, report) == {}
        assert "com.unity.burst@1.4.1" in report.failed

    def test_metadata_is_memoized_on_disk(self, registry, tmp_path):
        resolve_closure({"com.unity.burst": "1.4.1"}, tmp_path)
        first = len(registry.fetches)
        resolve_closure({"com.unity.burst": "1.4.1"}, tmp_path)
        assert len(registry.fetches) == first  # served from tmp_path

    def test_corrupt_memo_refetches_instead_of_raising(self, registry, tmp_path):
        (tmp_path / "com.unity.burst.json").write_text("{not json", encoding="utf-8")
        assert "com.unity.burst" in resolve_closure({"com.unity.burst": "1.4.1"},
                                                    tmp_path)


# ── Download ──────────────────────────────────────────────────────────────────

class TestDownload:
    def _pin(self, registry, name="com.unity.burst", ver="1.4.1"):
        meta = json.loads(registry.http_get(f"https://packages.unity.com/{name}"))
        d = meta["versions"][ver]["dist"]
        return PackagePin(name=name, version=ver, tarball=d["tarball"],
                          shasum=d["shasum"])

    def test_writes_the_npm_layout(self, registry, tmp_path):
        pin = self._pin(registry)
        assert download(pin, tmp_path) is True
        entry = local_entry_dir(tmp_path, pin)
        assert (entry / "package" / "package.json").is_file()
        assert (entry / "package.tgz").is_file()
        assert json.loads((entry / "signature").read_text())["status"] == "Valid"

    def test_second_call_is_a_no_op(self, registry, tmp_path):
        pin = self._pin(registry)
        download(pin, tmp_path)
        report = CacheReport()
        assert download(pin, tmp_path, report) is False
        assert report.already_cached == [pin.slug]

    def test_checksum_mismatch_leaves_no_entry(self, registry, tmp_path):
        pin = self._pin(registry)
        bad = PackagePin(name=pin.name, version=pin.version, tarball=pin.tarball,
                         shasum="0" * 40)
        report = CacheReport()
        assert download(bad, tmp_path, report) is False
        assert not local_entry_dir(tmp_path, bad).exists()
        assert "checksum mismatch" in report.failed[bad.slug]

    def test_tarball_without_a_package_root_is_rejected(self, monkeypatch, tmp_path):
        blob = _tarball(root="notpackage")
        monkeypatch.setattr("src.unity_package_cache._http_get",
                            lambda url, timeout=60: blob)
        pin = PackagePin(name="com.x", version="1.0.0", tarball="https://dl/x.tgz")
        assert download(pin, tmp_path, CacheReport()) is False
        assert not local_entry_dir(tmp_path, pin).exists()

    def test_path_traversal_entry_is_refused(self, monkeypatch, tmp_path):
        """A tarball is remote input; `../../evil` must not write outside."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"pwned"
            info = tarfile.TarInfo("../../evil.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        monkeypatch.setattr("src.unity_package_cache._http_get",
                            lambda url, timeout=60: buf.getvalue())
        pin = PackagePin(name="com.x", version="1.0.0", tarball="https://dl/x.tgz")
        assert download(pin, tmp_path, CacheReport()) is False
        assert not (tmp_path.parent / "evil.txt").exists()


# ── Prime ─────────────────────────────────────────────────────────────────────

class TestPrime:
    def _filled(self, registry, tmp_path):
        pins = resolve_closure({"com.unity.burst": "1.4.1"}, tmp_path / "meta")
        pin = pins["com.unity.burst"]
        download(pin, tmp_path / "local")
        return pin

    def test_writes_both_layouts_unity_reads(self, registry, tmp_path):
        pin = self._filled(registry, tmp_path)
        g = tmp_path / "global"
        prime([pin], tmp_path / "local", g)
        assert (global_package_dir(pin, g) / "package.json").is_file()
        assert (global_npm_dir(pin, g) / "package.tgz").is_file()
        assert is_globally_cached(pin, g)

    def test_never_overwrites_an_existing_global_entry(self, registry, tmp_path):
        """An existing <pkg>@<ver> is the user's own editor download — replacing
        it risks swapping a good package for a worse one, for no gain."""
        pin = self._filled(registry, tmp_path)
        g = tmp_path / "global"
        dest = global_package_dir(pin, g)
        dest.mkdir(parents=True)
        (dest / "package.json").write_text("THEIRS", encoding="utf-8")

        report = prime([pin], tmp_path / "local", g)
        assert (dest / "package.json").read_text() == "THEIRS"
        assert report.already_global == [pin.slug]
        assert report.primed == []

    def test_unfilled_package_is_skipped_not_half_primed(self, registry, tmp_path):
        pin = PackagePin(name="com.never", version="1.0.0", tarball="https://dl/x")
        g = tmp_path / "global"
        report = prime([pin], tmp_path / "local", g)
        assert report.primed == []
        assert not global_package_dir(pin, g).exists()

    def test_priming_twice_is_idempotent(self, registry, tmp_path):
        pin = self._filled(registry, tmp_path)
        g = tmp_path / "global"
        prime([pin], tmp_path / "local", g)
        report = prime([pin], tmp_path / "local", g)
        assert report.primed == [] and report.already_global == [pin.slug]

    def test_ensure_cached_fills_then_primes(self, registry, tmp_path):
        report = ensure_cached({"com.unity.cinemachine": "3.1.7"},
                               tmp_path / "local", tmp_path / "global")
        assert len(report.downloaded) == 3      # + splines + mathematics
        assert len(report.primed) == 3

    def test_offline_mode_primes_without_downloading(self, registry, tmp_path):
        ensure_cached({"com.unity.burst": "1.4.1"}, tmp_path / "local",
                      tmp_path / "global")
        report = ensure_cached({"com.unity.burst": "1.4.1"}, tmp_path / "local",
                               tmp_path / "global2", download_missing=False)
        assert report.downloaded == []
        assert report.primed == ["com.unity.burst@1.4.1"]


# ── Cache root ────────────────────────────────────────────────────────────────

class TestCacheRoot:
    def test_upm_cache_root_env_wins(self, monkeypatch, tmp_path):
        """Unity's own override — honoring it keeps us pointed wherever the
        user already redirected the editor."""
        monkeypatch.setenv("UPM_CACHE_ROOT", str(tmp_path / "elsewhere"))
        assert unity_cache_root() == tmp_path / "elsewhere"

    def test_windows_default_is_localappdata(self, monkeypatch, tmp_path):
        monkeypatch.delenv("UPM_CACHE_ROOT", raising=False)
        monkeypatch.setattr("platform.system", lambda: "Windows")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert unity_cache_root() == tmp_path / "Unity" / "cache"


# ── packages-lock.json ────────────────────────────────────────────────────────

class TestBuildLock:
    def test_registry_packages_carry_version_depth_and_deps(self, registry, tmp_path):
        deps = {"com.unity.cinemachine": "3.1.7"}
        lock = build_lock(resolve_closure(deps, tmp_path), deps)
        cm = lock["dependencies"]["com.unity.cinemachine"]
        assert cm["version"] == "3.1.7"
        assert cm["source"] == "registry"
        assert cm["depth"] == 0
        assert cm["dependencies"]["com.unity.splines"] == "2.0.0"

    def test_builtin_modules_are_emitted_as_builtin(self, registry, tmp_path):
        deps = {"com.unity.burst": "1.4.1", "com.unity.modules.imgui": "1.0.0"}
        lock = build_lock(resolve_closure(deps, tmp_path), deps)
        assert lock["dependencies"]["com.unity.modules.imgui"]["source"] == "builtin"

    def test_is_json_serializable_and_sorted(self, registry, tmp_path):
        deps = {"com.unity.cinemachine": "3.1.7", "com.unity.modules.ai": "1.0.0"}
        lock = build_lock(resolve_closure(deps, tmp_path), deps)
        names = list(lock["dependencies"])
        assert names == sorted(names)
        json.dumps(lock)


# ── The pipeline contract: this never fails a build ───────────────────────────

class TestPipelineHookIsBestEffort:
    @pytest.fixture
    def pipeline(self):
        from src.unity_pipeline import UnityPipeline
        p = object.__new__(UnityPipeline)
        p.package_cache_cfg = {}
        p._ok = lambda m: None
        p._warn = lambda m: None
        return p

    def test_disabled_by_config_is_a_no_op(self, pipeline, tmp_path):
        pipeline.package_cache_cfg = {"enabled": False}
        pipeline._prime_package_cache(tmp_path, {"com.unity.burst": "1.4.1"})
        assert not (tmp_path / "Packages").exists()

    def test_registry_outage_does_not_raise(self, pipeline, tmp_path, monkeypatch):
        def boom(url, timeout=60):
            raise OSError("no network")
        monkeypatch.setattr("src.unity_package_cache._http_get", boom)
        pipeline.package_cache_cfg = {"dir": str(tmp_path / "c"),
                                      "global_root": str(tmp_path / "g")}
        pipeline._prime_package_cache(tmp_path, {"com.unity.burst": "1.4.1"})

    def test_unwritable_cache_does_not_raise(self, pipeline, tmp_path, monkeypatch):
        monkeypatch.setattr("src.unity_package_cache.ensure_cached",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("readonly")))
        pipeline.package_cache_cfg = {"dir": str(tmp_path / "c")}
        pipeline._prime_package_cache(tmp_path, {"com.unity.burst": "1.4.1"})

    def test_lock_file_lands_in_the_project(self, pipeline, tmp_path, registry):
        pipeline.package_cache_cfg = {"dir": str(tmp_path / "c"),
                                      "global_root": str(tmp_path / "g")}
        pipeline._prime_package_cache(tmp_path, {"com.unity.cinemachine": "3.1.7",
                                                 "com.unity.modules.ai": "1.0.0"})
        lock = json.loads((tmp_path / "Packages" / "packages-lock.json").read_text())
        assert lock["dependencies"]["com.unity.cinemachine"]["version"] == "3.1.7"
        assert lock["dependencies"]["com.unity.modules.ai"]["source"] == "builtin"

    def test_write_lock_can_be_turned_off(self, pipeline, tmp_path, registry):
        pipeline.package_cache_cfg = {"dir": str(tmp_path / "c"),
                                      "global_root": str(tmp_path / "g"),
                                      "write_lock": False}
        pipeline._prime_package_cache(tmp_path, {"com.unity.burst": "1.4.1"})
        assert not (tmp_path / "Packages" / "packages-lock.json").exists()
