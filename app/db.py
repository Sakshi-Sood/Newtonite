"""Connection pool and the per-request connection dependency."""
from pathlib import Path
from typing import Iterator

from fastapi import Request
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def create_pool(url: str) -> ConnectionPool:
    return ConnectionPool(
        url, min_size=2, max_size=10, kwargs={"row_factory": dict_row}, open=True
    )


def get_conn(request: Request) -> Iterator[Connection]:
    """One connection and one transaction per request.

    The pool's context manager commits when the request handler returns and
    rolls back if it raises, so a request's writes land together or not at all.
    """
    with request.app.state.pool.connection() as conn:
        yield conn


def apply_schema(conn: Connection) -> None:
    conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
