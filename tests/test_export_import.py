"""Export/import round-trip and merge-semantics tests."""
import json

import pytest

from omega.sqlite_store import SQLiteStore


def _count(store: SQLiteStore) -> int:
    return store._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]


@pytest.fixture
def store_b(tmp_omega_dir):
    """A second isolated SQLiteStore, distinct from the `store` fixture."""
    db_path = tmp_omega_dir / "test-b.db"
    s = SQLiteStore(db_path=db_path)
    yield s
    s.close()


class TestExportRoundTrip:
    def test_export_creates_valid_json(self, store, tmp_omega_dir):
        store.store("alpha", metadata={"event_type": "lesson_learned"})
        store.store("beta", metadata={"event_type": "decision"})

        out = tmp_omega_dir / "exp.json"
        result = store.export_to_file(out)

        assert result["node_count"] == 2
        data = json.loads(out.read_text())
        assert data["version"] == "omega-sqlite-v1"
        assert len(data["nodes"]) == 2
        contents = sorted(n["content"] for n in data["nodes"])
        assert contents == ["alpha", "beta"]

    def test_import_clear_restores_nodes(self, store, store_b, tmp_omega_dir):
        store.store("alpha")
        store.store("beta")
        out = tmp_omega_dir / "exp.json"
        store.export_to_file(out)

        # store_b starts empty; default import clears + restores
        assert _count(store_b) == 0
        result = store_b.import_from_file(out)
        assert result["node_count"] == 2
        assert _count(store_b) == 2


class TestMergeSemantics:
    """Non-destructive import (`clear_existing=False`) — the merge path."""

    def test_merge_adds_novel_rows(self, store, store_b, tmp_omega_dir):
        # A has X+Y; B has Z. After merge B should have X+Y+Z.
        store.store("alpha unique to A")
        store.store("beta unique to A")
        store_b.store("gamma unique to B")

        out = tmp_omega_dir / "from-A.json"
        store.export_to_file(out)
        store_b.import_from_file(out, clear_existing=False)

        assert _count(store_b) == 3
        contents = {
            row[0]
            for row in store_b._conn.execute("SELECT content FROM memories").fetchall()
        }
        assert contents == {
            "alpha unique to A",
            "beta unique to A",
            "gamma unique to B",
        }

    def test_merge_preserves_existing_when_overlapping(
        self, store, store_b, tmp_omega_dir
    ):
        # Identical content on both sides: dedup pipeline must absorb it.
        store.store("shared memory")
        store.store("only on A")
        store_b.store("shared memory")          # exact dup
        store_b.store("only on B")

        out = tmp_omega_dir / "from-A.json"
        store.export_to_file(out)
        store_b.import_from_file(out, clear_existing=False)

        # Expected: original 2 in B + 1 novel from A; the shared row collapses.
        assert _count(store_b) == 3

    def test_merge_is_idempotent(self, store, store_b, tmp_omega_dir):
        # A second pass of the same export must not grow the DB.
        store.store("alpha")
        store_b.store("gamma")
        out = tmp_omega_dir / "from-A.json"
        store.export_to_file(out)

        store_b.import_from_file(out, clear_existing=False)
        first = _count(store_b)
        store_b.import_from_file(out, clear_existing=False)
        second = _count(store_b)
        assert first == second == 2

    def test_merge_keeps_local_rows_intact(self, store, store_b, tmp_omega_dir):
        # B's existing rows must survive the merge (no clobbering).
        store_b.store("preexisting on B")
        store.store("incoming from A")
        out = tmp_omega_dir / "from-A.json"
        store.export_to_file(out)

        store_b.import_from_file(out, clear_existing=False)

        contents = {
            row[0]
            for row in store_b._conn.execute("SELECT content FROM memories").fetchall()
        }
        assert "preexisting on B" in contents
        assert "incoming from A" in contents
