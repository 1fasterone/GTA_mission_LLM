"""
Regression tests for the RAG embedding cache and the rag-build CLI.

Observed failure this locks down: Windows temp cleanup deleted
``%TEMP%/fastembed_cache/models--qdrant--bge-small-en-v1.5-onnx-q/snapshots/*``
while leaving ``refs/`` + ``blobs/`` behind.  fastembed then resolved the model
from the local cache forever (never re-downloading), so:

  * ``python -m rag.indexer`` died with NO_SUCHFILE on every run, and
  * ``rag-status`` reported both domains "NOT BUILT" although the index held
    28k entries — because is_ready() loaded the embedding model and swallowed
    the resulting exception.

None of these tests may touch the network or the real fastembed/chromadb.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

from rag import embedder

MODEL    = embedder.EMBED_MODEL
MODEL_DIR_NAME = "models--qdrant--bge-small-en-v1.5-onnx-q"
REV      = "52398278842ec682c6f32300af41344b1c0b0bb2"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _write_cache(cache: Path, *, onnx_bytes: bytes = b"onnx-graph",
                 manifest: bool = True, drop_snapshot: bool = False) -> Path:
    """A fastembed-shaped model cache. drop_snapshot reproduces the temp wipe."""
    model_dir = cache / MODEL_DIR_NAME
    snap      = model_dir / "snapshots" / REV
    snap.mkdir(parents=True, exist_ok=True)
    (model_dir / "blobs").mkdir(exist_ok=True)
    (model_dir / "refs").mkdir(exist_ok=True)
    (model_dir / "refs" / "main").write_text(REV, encoding="utf-8")

    (snap / "model_optimized.onnx").write_bytes(onnx_bytes)
    (snap / "config.json").write_text("{}", encoding="utf-8")

    if manifest:
        # Keys use backslashes — fastembed writes them with the host separator.
        (model_dir / embedder._METADATA_FILE).write_text(json.dumps({
            f"snapshots\\{REV}\\model_optimized.onnx": {"size": len(onnx_bytes), "blob_id": "a"},
            f"snapshots\\{REV}\\config.json":          {"size": 2,               "blob_id": "b"},
        }), encoding="utf-8")

    if drop_snapshot:
        for f in snap.iterdir():
            f.unlink()

    return model_dir


class _FakeEmbedding:
    """Stand-in for fastembed.TextEmbedding — records how it was constructed."""
    calls: list[tuple[str, str]] = []
    fail_times: int = 0

    def __init__(self, model_name, cache_dir=None, **kw):
        type(self).calls.append((model_name, cache_dir))
        if type(self).fail_times > 0:
            type(self).fail_times -= 1
            raise OSError("NO_SUCHFILE: model_optimized.onnx failed. File doesn't exist")
        self.model_name = model_name


@pytest.fixture
def fake_fastembed(monkeypatch):
    _FakeEmbedding.calls = []
    _FakeEmbedding.fail_times = 0
    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = _FakeEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    return _FakeEmbedding


# ── cache location ────────────────────────────────────────────────────────────

def test_cache_dir_is_never_the_system_temp_dir(monkeypatch):
    """The whole bug: fastembed's default cache lives in %TEMP%, which Windows
    cleans up under the running install."""
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    resolved = embedder.cache_dir().resolve()
    temp     = Path(tempfile.gettempdir()).resolve()
    assert temp not in resolved.parents and resolved != temp


def test_cache_dir_honours_fastembed_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "weights"))
    assert embedder.cache_dir() == tmp_path / "weights"


# ── cache verification ────────────────────────────────────────────────────────

def test_complete_cache_is_not_flagged(tmp_path):
    assert embedder.is_incomplete(_write_cache(tmp_path)) is False


def test_wiped_snapshot_is_flagged(tmp_path):
    """refs/ + blobs/ survive, snapshots/ is emptied — the observed state."""
    model_dir = _write_cache(tmp_path, drop_snapshot=True)
    assert (model_dir / "refs" / "main").is_file()
    assert embedder.is_incomplete(model_dir) is True


def test_truncated_file_is_flagged(tmp_path):
    model_dir = _write_cache(tmp_path)
    (model_dir / "snapshots" / REV / "model_optimized.onnx").write_bytes(b"xx")
    assert embedder.is_incomplete(model_dir) is True


def test_unreadable_manifest_is_flagged(tmp_path):
    model_dir = _write_cache(tmp_path)
    (model_dir / embedder._METADATA_FILE).write_text("{not json", encoding="utf-8")
    assert embedder.is_incomplete(model_dir) is True


def test_without_manifest_the_onnx_file_decides(tmp_path):
    complete = _write_cache(tmp_path, manifest=False)
    assert embedder.is_incomplete(complete) is False
    (complete / "snapshots" / REV / "model_optimized.onnx").unlink()
    assert embedder.is_incomplete(complete) is True


def test_purge_only_touches_the_named_model(tmp_path):
    _write_cache(tmp_path)
    other = tmp_path / "models--qdrant--all-MiniLM-L6-v2-onnx"
    other.mkdir()
    removed = embedder.purge(MODEL, tmp_path)
    assert [p.name for p in removed] == [MODEL_DIR_NAME]
    assert other.exists()


def test_status_reports_incomplete_without_loading(tmp_path):
    _write_cache(tmp_path, drop_snapshot=True)
    st = embedder.status(MODEL, tmp_path)
    assert st["cached"] is True and st["complete"] is False


# ── loader ────────────────────────────────────────────────────────────────────

def test_incomplete_cache_is_purged_before_load(fake_fastembed, tmp_path):
    model_dir = _write_cache(tmp_path, drop_snapshot=True)
    embedder.load_embedder(MODEL, cache=tmp_path)
    assert not model_dir.exists(), "half-cached model must be removed, else fastembed reuses it"
    assert fake_fastembed.calls == [(MODEL, str(tmp_path))]


def test_complete_cache_is_kept(fake_fastembed, tmp_path):
    model_dir = _write_cache(tmp_path)
    embedder.load_embedder(MODEL, cache=tmp_path)
    assert model_dir.exists()


def test_load_failure_purges_and_retries_once(fake_fastembed, tmp_path):
    """A cache that passes verification but still fails to load gets one purge."""
    model_dir = _write_cache(tmp_path)
    fake_fastembed.fail_times = 1
    embedder.load_embedder(MODEL, cache=tmp_path)
    assert not model_dir.exists()
    assert len(fake_fastembed.calls) == 2


def test_second_failure_raises_actionable_error(fake_fastembed, tmp_path):
    _write_cache(tmp_path)
    fake_fastembed.fail_times = 2
    with pytest.raises(RuntimeError) as exc:
        embedder.load_embedder(MODEL, cache=tmp_path)
    assert "FASTEMBED_CACHE_PATH" in str(exc.value)
    assert isinstance(exc.value.__cause__, OSError)


def test_repair_false_propagates_the_raw_error(fake_fastembed, tmp_path):
    model_dir = _write_cache(tmp_path, drop_snapshot=True)
    fake_fastembed.fail_times = 1
    with pytest.raises(OSError):
        embedder.load_embedder(MODEL, cache=tmp_path, repair=False)
    assert model_dir.exists(), "repair=False must not delete anything"


# ── is_ready must not depend on the embedding model ───────────────────────────

class _FakeCollection:
    def __init__(self, n): self._n = n
    def count(self): return self._n


@pytest.fixture
def fake_chromadb(monkeypatch):
    class _Client:
        def __init__(self, path=None): self.path = path
        def get_collection(self, name): return _FakeCollection(28006)
    mod = types.ModuleType("chromadb")
    mod.PersistentClient = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chromadb", mod)
    return mod


def test_is_ready_does_not_load_the_embedding_model(fake_chromadb, monkeypatch, tmp_path):
    """rag-status said NOT BUILT for a 28k-entry index because a broken ONNX
    cache raised inside is_ready()'s bare except."""
    def _boom(*a, **kw):
        raise AssertionError("is_ready() must not load the embedding model")
    monkeypatch.setattr(embedder, "load_embedder", _boom)

    from rag.query import RagClient
    assert RagClient(index_dir=str(tmp_path)).is_ready(domain="gta5") is True


def test_search_still_embeds(fake_chromadb, monkeypatch, tmp_path):
    class _Vec(list):
        def tolist(self): return list(self)

    class _Model:
        def embed(self, texts): return iter([_Vec([0.1, 0.2])])

    monkeypatch.setattr(embedder, "load_embedder", lambda *a, **kw: _Model())
    monkeypatch.setattr(_FakeCollection, "query", lambda self, **kw: {
        "metadatas": [[{"canonical": "s_m_y_cop_01", "kind": "ped"}]],
        "distances": [[0.12]],
    }, raising=False)

    from rag.query import RagClient
    hits = RagClient(index_dir=str(tmp_path)).search("cop", domain="gta5")
    assert hits[0]["canonical"] == "s_m_y_cop_01"


# ── rag-build CLI ─────────────────────────────────────────────────────────────

@pytest.fixture
def cli_runs(monkeypatch):
    """Capture the subprocess argv rag-build would run."""
    import subprocess
    runs: list[list[str]] = []
    codes: list[int] = []

    class _Completed:
        def __init__(self, code): self.returncode = code

    def _fake_run(args, **kw):
        runs.append(list(args))
        return _Completed(codes.pop(0) if codes else 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return runs, codes


@pytest.fixture(scope="module")
def cli_app():
    """
    main.app, imported without leaking main.py's module-level load_dotenv()
    into os.environ — dotenv walks up to the repo .env, and other suites assert
    on env-derived state (e.g. the Sketchfab token gate in
    tests/test_sketchfab_client.py).
    """
    before = dict(os.environ)
    import main
    os.environ.clear()
    os.environ.update(before)
    return main.app


def _invoke(app, *args):
    from typer.testing import CliRunner
    return CliRunner().invoke(app, list(args))


def test_rag_build_unity_flag_reaches_both_steps(cli_app, cli_runs):
    runs, _ = cli_runs
    result = _invoke(cli_app, "rag-build", "--unity")
    assert result.exit_code == 0, result.output
    assert "--unity" in runs[0] and runs[0][-1] == "--unity"
    assert runs[1][-2:] == ["--domain", "unity"]


def test_rag_build_default_indexes_every_domain(cli_app, cli_runs):
    runs, _ = cli_runs
    assert _invoke(cli_app, "rag-build").exit_code == 0
    assert "--unity" not in runs[0]
    assert "--domain" not in runs[1]


def test_rag_build_forwards_targets_and_options(cli_app, cli_runs):
    runs, _ = cli_runs
    result = _invoke(cli_app, "rag-build", "weapons", "scenarios", "--force", "--limit", "50")
    assert result.exit_code == 0, result.output
    assert runs[0][-4:] == ["weapons", "scenarios", "--force", "--limit=50"]


def test_rag_build_rejects_unity_with_explicit_targets(cli_app, cli_runs):
    runs, _ = cli_runs
    result = _invoke(cli_app, "rag-build", "weapons", "--unity")
    assert result.exit_code == 2
    assert runs == [], "nothing should run when the flags contradict"


def test_rag_build_reports_a_failing_step_without_a_traceback(cli_app, cli_runs):
    """CalledProcessError used to dump a Rich traceback from main.py itself."""
    runs, codes = cli_runs
    codes.extend([0, 1])
    result = _invoke(cli_app, "rag-build")
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "vector index" in result.output


def test_rag_build_stops_when_the_scrape_fails(cli_app, cli_runs):
    runs, codes = cli_runs
    codes.append(3)
    result = _invoke(cli_app, "rag-build")
    assert result.exit_code == 3
    assert len(runs) == 1, "the indexer must not run on a failed scrape"
