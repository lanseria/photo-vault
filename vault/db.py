from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db"


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def init_db(dsn: str) -> list[str]:
    applied: list[str] = []
    with connect(dsn) as conn:
        conn.execute(
            """create table if not exists schema_migrations (
                   version text primary key,
                   applied_at timestamptz not null default now())"""
        )
        conn.commit()
        done = {
            r["version"]
            for r in conn.execute("select version from schema_migrations").fetchall()
        }
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = sql_file.stem
            if version in done:
                continue
            conn.execute(sql_file.read_text(encoding="utf-8"))
            conn.commit()
            applied.append(version)
    return applied


def ping(dsn: str) -> str:
    with connect(dsn) as conn:
        row = conn.execute("select version() as v").fetchone()
    return row["v"] if row else "unknown"