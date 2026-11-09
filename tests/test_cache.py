"""Fast paths retain freshness and keep all public results isolated."""

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from callmap.analyzer import Analyzer
from callmap.store import GraphStore


def project(tmp_path):
    source = tmp_path / "main.py"
    source.write_text('def leaf(value): return value\ndef entry(): leaf("ok")\n')
    return source, GraphStore(tmp_path)


def test_warm_queries_do_not_read_sources_or_decode_sqlite_again(tmp_path, monkeypatch):
    source, store = project(tmp_path)
    store.read_node("entry")
    original = Path.read_bytes

    def no_source_read(path):
        assert path != source
        return original(path)

    def no_connection():
        raise AssertionError("unchanged queries should reuse the snapshot")

    monkeypatch.setattr(Path, "read_bytes", no_source_read)
    monkeypatch.setattr(store, "_connect", no_connection)
    assert store.read_node("entry")["name"] == "entry"
    assert store.expand("entry")["cache_hit"]
    assert store.callers("leaf")["cache_hit"]
    assert store.stats()["cache_hit"]


def test_same_size_edit_with_restored_mtime_invalidates_immediately(tmp_path):
    source, store = project(tmp_path)
    original = store.read_node("entry")
    stamp = source.stat()
    source.write_text(source.read_text().replace('"ok"', '"no"'))
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    updated = store.read_node("entry")
    assert updated["hash"] != original["hash"]
    assert 'leaf("no")' in updated["code"]


def test_atomic_replacement_new_and_deleted_files_refresh_cached_views(tmp_path):
    source, store = project(tmp_path)
    original = store.stats()["snapshot"]
    stamp = source.stat()
    replacement = tmp_path / "replacement.txt"
    replacement.write_text(source.read_text().replace('"ok"', '"no"'))
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(source)
    assert store.stats()["snapshot"] != original
    added = tmp_path / "added.py"
    added.write_text("def added(): pass\n")
    assert store.search("added")
    added.unlink()
    assert not store.search("added")


def test_public_results_cannot_mutate_snapshot_indexes_or_annotations(tmp_path):
    _, store = project(tmp_path)
    snapshot = store.ensure()
    snapshot["nodes"].clear()
    assert store.ensure()["nodes"]
    graph = store.graph()
    graph["nodes"].clear()
    graph["edges"].clear()
    assert store.graph()["edges"]
    expanded = store.expand("entry")
    expanded["edges"][0]["callsite"]["arguments"][0]["expression"] = "poison"
    expanded["edges"][0]["resolution"]["unknowns"].clear()
    assert (
        store.expand("entry")["edges"][0]["callsite"]["arguments"][0]["expression"]
        == "'ok'"
    )
    assert store.expand("entry")["edges"][0]["resolution"]["unknowns"]
    store.annotate("entry", "manual")
    node = store.read_node("entry")
    node["doc"] = "poison"
    node["annotation"]["doc"] = "poison"
    annotations = store.annotations()
    annotations[node["id"]]["doc"] = "poison"
    assert store.read_node("entry")["doc"] == "manual"
    store.annotations_path.unlink()
    assert store.read_node("entry")["doc"] == ""
    assert store.read_node("entry")["doc_source"] != "manual"


def test_context_views_and_result_mutations_remain_isolated(tmp_path):
    (tmp_path / "main.py").write_text("""
def read(): pass
def save(): pass
def query(callback): callback()
def reading(): query(read)
def saving(): query(save)
""")
    store = GraphStore(tmp_path)
    reading = store.graph("reading")
    reading["edges"].clear()
    reading["analysis"].clear()
    for entry, expected, absent in (
        ("reading", "main.read", "main.save"),
        ("saving", "main.save", "main.read"),
        ("reading", "main.read", "main.save"),
    ):
        names = {
            n["qualified_name"]
            for n in store.expand(entry, context_entry=entry)["nodes"]
        }
        assert expected in names
        assert absent not in names


def test_another_store_annotation_update_refreshes_document_and_manual_targets(
    tmp_path,
):
    (tmp_path / "main.py").write_text("""
def read(): pass
def save(): pass
def entry(callback): callback()
""")
    first, second = GraphStore(tmp_path), GraphStore(tmp_path)
    unknown = next(
        n for n in first.expand("entry")["nodes"] if n["kind"] == "unresolved"
    )
    second.annotate(unknown["id"], "read target", ["read"])
    assert "main.read" in {n["qualified_name"] for n in first.expand("entry")["nodes"]}
    second.annotate(unknown["id"], "save target", ["save"])
    names = {n["qualified_name"] for n in first.expand("entry")["nodes"]}
    assert "main.save" in names and "main.read" not in names
    assert first.read_node(unknown["id"])["doc"] == "save target"


def test_another_store_snapshot_is_adopted_without_reanalysis(tmp_path, monkeypatch):
    source, first = project(tmp_path)
    second = GraphStore(tmp_path)
    first.expand("entry")
    source.write_text(source.read_text().replace('"ok"', '"no"'))
    updated = second.ensure(force=True)

    def no_analysis(*args, **kwargs):
        raise AssertionError("the other store has already rebuilt this snapshot")

    monkeypatch.setattr(Analyzer, "analyze", no_analysis)
    assert first.graph()["indexed_at"] == updated["indexed_at"]
    assert 'leaf("no")' in first.read_node("entry")["code"]


def test_committed_wal_changes_are_visible_to_a_warm_store(tmp_path, monkeypatch):
    _, store = project(tmp_path)
    store.graph()
    connection = sqlite3.connect(store.database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        original = store.graph()["indexed_at"]
        from callmap.cache import file_stamp

        frozen = {}
        for path in (store.database, Path(str(store.database) + "-wal")):
            try:
                frozen[path] = file_stamp(path)
            except FileNotFoundError:
                frozen[path] = None

        def same_database_stat(path):
            if path in frozen:
                if frozen[path] is None:
                    raise FileNotFoundError(path)
                return frozen[path]
            return file_stamp(path)

        monkeypatch.setattr("callmap.store.file_stamp", same_database_stat)
        data = json.loads(
            connection.execute("SELECT data FROM snapshot WHERE id=1").fetchone()[0]
        )
        data["indexed_at"] = original + 10
        connection.execute("UPDATE snapshot SET data=? WHERE id=1", (json.dumps(data),))
        connection.commit()
        assert store.graph()["indexed_at"] == original + 10
    finally:
        connection.close()


def test_force_and_dependency_environment_changes_invalidate_hash_cache(
    tmp_path, monkeypatch
):
    source, store = project(tmp_path)
    distribution = SimpleNamespace(metadata={"Name": "example"}, version="1")
    monkeypatch.setattr(
        "callmap.store.importlib.metadata.distributions", lambda: [distribution]
    )
    before = store.stats()["snapshot"]
    reads = []
    original = Path.read_bytes

    def track(path):
        if path == source:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", track)
    assert store.stats()["cache_hit"]
    assert not reads
    store.stats(force=True)
    assert reads
    distribution.version = "2"
    assert store.stats()["snapshot"] != before
