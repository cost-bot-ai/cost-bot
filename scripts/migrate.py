#!/usr/bin/env python3
"""Apply db/migrations/*.sql in order, once each, recording a checksum.

    python scripts/migrate.py                 # uses $DATABASE_URL
    python scripts/migrate.py --dsn postgresql://...   # or pass one (the value is never printed)

Each file runs in its own transaction together with its bookkeeping row, so a failing migration leaves nothing
half-applied. If an already-applied file has since been edited, the runner refuses to continue: change the schema
with a NEW migration, never by editing an old one.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys

DEFAULT_DIR = pathlib.Path(__file__).resolve().parents[1] / "db" / "migrations"

BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    pass


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def migrate(conn, directory: pathlib.Path = DEFAULT_DIR) -> list[str]:
    """Apply pending migrations on an open psycopg connection; return the filenames applied."""
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise MigrationError(f"no .sql files in {directory}")
    with conn.transaction():
        conn.execute(BOOKKEEPING)
    applied = {row[0]: row[1] for row in conn.execute("SELECT filename, checksum FROM schema_migrations")}
    done: list[str] = []
    for f in files:
        sql = f.read_text()
        digest = checksum(sql)
        if f.name in applied:
            if applied[f.name] != digest:
                raise MigrationError(
                    f"{f.name} was edited after it was applied; add a new migration instead of changing an old one")
            continue
        with conn.transaction():
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)", (f.name, digest))
        done.append(f.name)
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--dir", type=pathlib.Path, default=DEFAULT_DIR)
    args = ap.parse_args()
    if not args.dsn:
        print("No database: set DATABASE_URL or pass --dsn.", file=sys.stderr)
        return 2
    import psycopg  # imported late so the module can be tested without a database

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        try:
            done = migrate(conn, args.dir)
        except MigrationError as e:
            print(f"Migration refused: {e}", file=sys.stderr)
            return 1
    print("Applied:", ", ".join(done) if done else "nothing (already up to date)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
