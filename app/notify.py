"""Live-update notices through Postgres NOTIFY.

A NOTIFY sent inside a transaction is only delivered when that transaction
commits, so a change that rolls back never announces itself.
app.events turns these notices into Server-Sent Events for open browser tabs.
"""
import json

from psycopg import Connection

CHANNEL = "item_changes"


def publish(conn: Connection, payload: dict) -> None:
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, json.dumps(payload)))
