import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for directory in ("raw", "datasets", "transformation_reports"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                document TEXT NOT NULL, UNIQUE(source_id, fingerprint));
            CREATE TABLE IF NOT EXISTS mappings (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transformation_recipes (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transformation_runs (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quality_runs (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ingestion_history (
                id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id),
                created_at TEXT NOT NULL, document TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.root / "metadata.sqlite", timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, table, identifier):
        assert table in {
            "sources",
            "datasets",
            "mappings",
            "reconciliation_runs",
            "quality_runs",
            "transformation_recipes",
            "transformation_runs",
        }
        with self.db() as db:
            row = db.execute(
                f"SELECT document FROM {table} WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise KeyError(f"{table}: {identifier} not found")
        return json.loads(row["document"])

    def list(self, table):
        assert table in {
            "sources",
            "datasets",
            "mappings",
            "ingestion_history",
            "reconciliation_runs",
            "quality_runs",
            "transformation_recipes",
            "transformation_runs",
        }
        with self.db() as db:
            rows = db.execute(
                f"SELECT document FROM {table} ORDER BY rowid DESC"
            ).fetchall()
        return [json.loads(row["document"]) for row in rows]

    def put(self, table, document):
        assert table in {
            "sources",
            "mappings",
            "reconciliation_runs",
            "quality_runs",
            "transformation_recipes",
            "transformation_runs",
        }
        with self.db() as db:
            db.execute(
                f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (document["id"], json.dumps(document)),
            )

    def history(self, source_id, **details):
        document = dict(id=uid(), source_id=source_id, created_at=now(), **details)
        with self.db() as db:
            db.execute(
                "INSERT INTO ingestion_history VALUES (?,?,?,?)",
                (
                    document["id"],
                    source_id,
                    document["created_at"],
                    json.dumps(document),
                ),
            )
