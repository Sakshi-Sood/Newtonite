"""Safe retries. A client sends an Idempotency-Key with each mutation; repeating
the request with the same key returns the first response instead of acting twice.

The key row is written in the same transaction as the change itself:
- two concurrent requests with one key queue on the primary key, and the second
  sees the first's stored response once it commits;
- if the change fails, the key row rolls back with it, so a retry can succeed.
"""
import hashlib
import json
from typing import Callable

from fastapi.encoders import jsonable_encoder
from psycopg import Connection
from psycopg.types.json import Jsonb

from app.errors import AppError


def request_hash(method: str, path: str, body) -> str:
    raw = f"{method} {path} {json.dumps(body, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def run_idempotent(
    conn: Connection, user_id, key: str | None, req_hash: str, fn: Callable[[], tuple[int, dict]]
) -> tuple[int, dict, bool]:
    """Returns (status, body, replayed)."""
    if not key or len(key) > 200:
        raise AppError(400, "IDEMPOTENCY_KEY_REQUIRED",
                       "Send an Idempotency-Key header (1-200 characters) with this request.")
    inserted = conn.execute(
        """INSERT INTO idempotency_keys (user_id, key, request_hash, status_code, response)
           VALUES (%s, %s, %s, 0, 'null') ON CONFLICT DO NOTHING RETURNING key""",
        (user_id, key, req_hash),
    ).fetchone()
    if inserted is None:
        prior = conn.execute(
            "SELECT request_hash, status_code, response FROM idempotency_keys WHERE user_id = %s AND key = %s",
            (user_id, key),
        ).fetchone()
        if prior["request_hash"] != req_hash:
            raise AppError(422, "IDEMPOTENCY_KEY_REUSED",
                           "This Idempotency-Key was already used for a different request.")
        return prior["status_code"], prior["response"], True

    status, body = fn()
    body = jsonable_encoder(body)
    conn.execute(
        "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE user_id = %s AND key = %s",
        (status, Jsonb(body), user_id, key),
    )
    return status, body, False
