"""Every stored value of a database as bytes, for "this secret is never stored in plaintext" assertions."""

from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool


def raw_db_dump(url: str) -> bytes:
    if url.startswith("sqlite:///"):
        return b"".join(line.encode() for line in sqlite3.connect(url.removeprefix("sqlite:///")).iterdump())
    engine = create_engine(url, poolclass=NullPool)
    try:
        chunks: list[bytes] = []
        with engine.connect() as conn:
            for table in inspect(engine).get_table_names():
                for row in conn.execute(text(f'SELECT * FROM "{table}"')):
                    chunks.append(repr(tuple(row)).encode())
        return b"\n".join(chunks)
    finally:
        engine.dispose()
