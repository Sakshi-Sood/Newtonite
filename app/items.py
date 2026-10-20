"""Work item operations. Every function takes the request's connection, so the
change and its activity row commit (or roll back) together.
"""
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi.encoders import jsonable_encoder
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, StringConstraints

from app.errors import AppError
from app.rules import ITEM_TYPES, PRIORITIES, allowed_actions, can

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(max_length=20000)]
ItemType = Literal[ITEM_TYPES]
Priority = Literal[PRIORITIES]


class ItemCreate(BaseModel):
    team_id: uuid.UUID
    title: Title
    description: Description = ""
    type: ItemType
    priority: Priority
    requires_approval: bool = False
    due_at: datetime | None = None


def _not_found() -> AppError:
    return AppError(404, "NOT_FOUND", "Work item not found.")


def role_in_team(conn: Connection, user_id, team_id) -> str | None:
    row = conn.execute(
        "SELECT role FROM memberships WHERE user_id = %s AND team_id = %s", (user_id, team_id)
    ).fetchone()
    return row["role"] if row else None


def load_item(conn: Connection, item_id: str, user_id, for_update: bool = False) -> tuple[dict, str]:
    """The item row and the caller's role in its team.

    Missing items, malformed ids and items in teams the caller isn't in all
    look the same (404), so outsiders can't learn what exists.
    """
    try:
        item_uuid = uuid.UUID(str(item_id))
    except ValueError:
        raise _not_found()
    lock = " FOR UPDATE" if for_update else ""
    row = conn.execute(f"SELECT * FROM work_items WHERE id = %s{lock}", (item_uuid,)).fetchone()
    if row is None:
        raise _not_found()
    role = role_in_team(conn, user_id, row["team_id"])
    if role is None:
        raise _not_found()
    return row, role


def require(item: dict, user_id, role: str, permission: str) -> None:
    if not can(item, user_id, role, permission):
        raise AppError(403, "FORBIDDEN", f"You are not allowed to {permission} this item.",
                       {"permission": permission})


def record_activity(conn: Connection, item_id, actor_id, kind: str, payload: dict) -> int:
    row = conn.execute(
        """INSERT INTO activity (work_item_id, actor_id, kind, payload)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (item_id, actor_id, kind, Jsonb(jsonable_encoder(payload))),
    ).fetchone()
    conn.execute("UPDATE work_items SET last_activity_at = now() WHERE id = %s", (item_id,))
    return row["id"]


def item_dto(conn: Connection, row: dict, user_id, role: str) -> dict:
    names = conn.execute(
        """SELECT t.name AS team_name, c.name AS creator_name, a.name AS assignee_name
           FROM teams t JOIN users c ON c.id = %s LEFT JOIN users a ON a.id = %s
           WHERE t.id = %s""",
        (row["created_by"], row["assignee_id"], row["team_id"]),
    ).fetchone()
    return {
        "id": row["id"],
        "team": {"id": row["team_id"], "name": names["team_name"]},
        "title": row["title"],
        "description": row["description"],
        "type": row["type"],
        "priority": row["priority"],
        "status": row["status"],
        "requires_approval": row["requires_approval"],
        "due_at": row["due_at"],
        "assignee": {"id": row["assignee_id"], "name": names["assignee_name"]} if row["assignee_id"] else None,
        "created_by": {"id": row["created_by"], "name": names["creator_name"]},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_activity_at": row["last_activity_at"],
        "version": row["version"],
        "allowed_actions": allowed_actions(row, user_id, role),
    }


def _fresh_dto(conn: Connection, item_id, user_id, role: str) -> dict:
    row = conn.execute("SELECT * FROM work_items WHERE id = %s", (item_id,)).fetchone()
    return item_dto(conn, row, user_id, role)


def create_item(conn: Connection, user_id, data: ItemCreate) -> dict:
    role = role_in_team(conn, user_id, data.team_id)
    if role is None:
        raise AppError(404, "NOT_FOUND", "Team not found.")
    status = "awaiting_approval" if data.requires_approval else "open"
    row = conn.execute(
        """INSERT INTO work_items
               (team_id, title, description, type, priority, status, requires_approval, due_at, created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
        (data.team_id, data.title, data.description, data.type, data.priority, status,
         data.requires_approval, data.due_at, user_id),
    ).fetchone()
    record_activity(conn, row["id"], user_id, "created",
                    {"title": row["title"], "priority": row["priority"], "status": status})
    return _fresh_dto(conn, row["id"], user_id, role)


def get_item(conn: Connection, item_id: str, user_id) -> dict:
    row, role = load_item(conn, item_id, user_id)
    return item_dto(conn, row, user_id, role)


def _entry(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "actor": {"id": row["actor_id"], "name": row["actor_name"]},
        "payload": row["payload"],
        "created_at": row["created_at"],
    }


def list_activity(conn: Connection, item_id: str, user_id, before_id: int | None = None, limit: int = 50) -> dict:
    item, _ = load_item(conn, item_id, user_id)
    rows = conn.execute(
        """SELECT a.id, a.kind, a.payload, a.created_at, u.id AS actor_id, u.name AS actor_name
           FROM activity a JOIN users u ON u.id = a.actor_id
           WHERE a.work_item_id = %s AND (%s::bigint IS NULL OR a.id < %s)
           ORDER BY a.id DESC LIMIT %s""",
        (item["id"], before_id, before_id, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "entries": [_entry(r) for r in rows],
        "next_before_id": rows[-1]["id"] if has_more else None,
    }
