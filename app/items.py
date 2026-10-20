"""Work item operations. Every function takes the request's connection, so the
change and its activity row commit (or roll back) together.
"""
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi.encoders import jsonable_encoder
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import AfterValidator, BaseModel, StringConstraints, model_validator

from app.errors import AppError
from app.rules import ACTIONS, ITEM_TYPES, PRIORITIES, REASON_REQUIRED, TERMINAL, allowed_actions, can, next_status

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(max_length=20000)]
ItemType = Literal[ITEM_TYPES]
Priority = Literal[PRIORITIES]
# Times without a zone are taken as UTC, so they compare cleanly with stored timestamptz values.
AwareDatetime = Annotated[datetime, AfterValidator(lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc))]
EDITABLE = ("title", "description", "priority", "due_at")


class ItemCreate(BaseModel):
    team_id: uuid.UUID
    title: Title
    description: Description = ""
    type: ItemType
    priority: Priority
    requires_approval: bool = False
    due_at: AwareDatetime | None = None


class ItemPatch(BaseModel):
    expected_version: int
    title: Title | None = None
    description: Description | None = None
    priority: Priority | None = None
    due_at: AwareDatetime | None = None  # explicit null clears the due date

    @model_validator(mode="after")
    def _check(self):
        sent = self.model_fields_set - {"expected_version"}
        if not sent:
            raise ValueError("Send at least one field to change.")
        for field in ("title", "description", "priority"):
            if field in sent and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self

    def changes(self) -> dict:
        return {f: getattr(self, f) for f in EDITABLE if f in self.model_fields_set}


class CommentIn(BaseModel):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]


class AssignIn(BaseModel):
    assignee_id: uuid.UUID | None
    expected_version: int


class TransitionIn(BaseModel):
    action: Literal[ACTIONS]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    expected_version: int


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


def stale_error(conn: Connection, item_id, user_id, role: str) -> AppError:
    """409 carrying the item as it is now and who changed it last, so the client can show a diff."""
    # Only kinds that bump the version count; a later comment didn't cause the conflict.
    last = conn.execute(
        """SELECT u.id, u.name FROM activity a JOIN users u ON u.id = a.actor_id
           WHERE a.work_item_id = %s AND a.kind IN ('updated', 'assigned', 'status_changed')
           ORDER BY a.id DESC LIMIT 1""",
        (item_id,),
    ).fetchone()
    return AppError(409, "STALE_VERSION", "This item was changed since you loaded it.",
                    {"current": _fresh_dto(conn, item_id, user_id, role),
                     "changed_by": {"id": last["id"], "name": last["name"]} if last else None})


def update_item(conn: Connection, item_id: str, user_id, expected_version: int, changes: dict) -> dict:
    row, role = load_item(conn, item_id, user_id)
    require(row, user_id, role, "edit")
    diffs = {f: {"from": row[f], "to": v} for f, v in changes.items() if f in EDITABLE and row[f] != v}
    if not diffs:
        return item_dto(conn, row, user_id, role)
    sets = ", ".join(f"{f} = %s" for f in diffs)  # field names come from EDITABLE only
    updated = conn.execute(
        f"""UPDATE work_items SET {sets}, version = version + 1, updated_at = now()
            WHERE id = %s AND version = %s RETURNING *""",
        (*[d["to"] for d in diffs.values()], row["id"], expected_version),
    ).fetchone()
    if updated is None:
        raise stale_error(conn, row["id"], user_id, role)
    record_activity(conn, row["id"], user_id, "updated", {"changes": diffs})
    return _fresh_dto(conn, row["id"], user_id, role)


def _user_ref(conn: Connection, user_id) -> dict | None:
    if user_id is None:
        return None
    row = conn.execute("SELECT id, name FROM users WHERE id = %s", (user_id,)).fetchone()
    return {"id": row["id"], "name": row["name"]} if row else None


def _assignment_payload(conn: Connection, old_id, new_id) -> dict:
    old, new = _user_ref(conn, old_id), _user_ref(conn, new_id)
    return {"from": old_id, "to": new_id,
            "from_name": old["name"] if old else None, "to_name": new["name"] if new else None}


def _already_claimed(conn: Connection, item_id) -> AppError:
    row = conn.execute("SELECT assignee_id FROM work_items WHERE id = %s", (item_id,)).fetchone()
    return AppError(409, "ALREADY_CLAIMED", "Someone else already took this item.",
                    {"assignee": _user_ref(conn, row["assignee_id"])})


def claim_item(conn: Connection, item_id: str, user_id) -> dict:
    """Take an unassigned item. A single conditional UPDATE decides the race:
    whoever's statement runs first wins; everyone else gets 409 naming the winner.
    No expected_version: an unrelated edit shouldn't make a claim fail.
    """
    row, role = load_item(conn, item_id, user_id)
    if row["assignee_id"] is not None and str(row["assignee_id"]) == str(user_id):
        return item_dto(conn, row, user_id, role)
    if row["assignee_id"] is not None and row["status"] not in TERMINAL:
        raise _already_claimed(conn, row["id"])
    require(row, user_id, role, "claim")
    updated = conn.execute(
        """UPDATE work_items SET assignee_id = %s, version = version + 1, updated_at = now()
           WHERE id = %s AND assignee_id IS NULL AND status NOT IN ('done', 'cancelled')
           RETURNING *""",
        (user_id, row["id"]),
    ).fetchone()
    if updated is None:
        raise _already_claimed(conn, row["id"])
    record_activity(conn, row["id"], user_id, "assigned", _assignment_payload(conn, None, user_id))
    return _fresh_dto(conn, row["id"], user_id, role)


def assign_item(conn: Connection, item_id: str, user_id, assignee_id, expected_version: int) -> dict:
    row, role = load_item(conn, item_id, user_id)
    require(row, user_id, role, "assign")
    if assignee_id is not None and role_in_team(conn, assignee_id, row["team_id"]) is None:
        raise AppError(422, "INVALID_ASSIGNEE", "The assignee must be a member of the item's team.")
    if str(row["assignee_id"]) == str(assignee_id):
        return item_dto(conn, row, user_id, role)
    updated = conn.execute(
        """UPDATE work_items SET assignee_id = %s, version = version + 1, updated_at = now()
           WHERE id = %s AND version = %s RETURNING *""",
        (assignee_id, row["id"], expected_version),
    ).fetchone()
    if updated is None:
        raise stale_error(conn, row["id"], user_id, role)
    record_activity(conn, row["id"], user_id, "assigned",
                    _assignment_payload(conn, row["assignee_id"], assignee_id))
    return _fresh_dto(conn, row["id"], user_id, role)


def transition_item(conn: Connection, item_id: str, user_id, action: str, reason: str | None,
                    expected_version: int) -> dict:
    # Row lock: concurrent transitions on one item run one after another.
    row, role = load_item(conn, item_id, user_id, for_update=True)
    if row["version"] != expected_version:
        raise stale_error(conn, row["id"], user_id, role)
    new_status = next_status(row["status"], action)
    if new_status is None:
        raise AppError(422, "INVALID_TRANSITION", f"Cannot {action} an item that is {row['status']}.",
                       {"from": row["status"], "action": action})
    require(row, user_id, role, action)
    reason = (reason or "").strip() or None
    if action in REASON_REQUIRED and not reason:
        raise AppError(400, "REASON_REQUIRED", f"A reason is required to {action} this item.")
    if action == "start" and row["assignee_id"] is None:
        raise AppError(422, "ASSIGNEE_REQUIRED", "Assign the item to someone before starting it.")
    conn.execute(
        "UPDATE work_items SET status = %s, version = version + 1, updated_at = now() WHERE id = %s",
        (new_status, row["id"]),
    )
    record_activity(conn, row["id"], user_id, "status_changed",
                    {"from": row["status"], "to": new_status, "action": action, "reason": reason})
    return _fresh_dto(conn, row["id"], user_id, role)


def add_comment(conn: Connection, item_id: str, user_id, body: str) -> dict:
    row, role = load_item(conn, item_id, user_id)
    require(row, user_id, role, "comment")
    activity_id = record_activity(conn, row["id"], user_id, "comment", {"body": body})
    entry = conn.execute(
        """SELECT a.id, a.kind, a.payload, a.created_at, u.id AS actor_id, u.name AS actor_name
           FROM activity a JOIN users u ON u.id = a.actor_id WHERE a.id = %s""",
        (activity_id,),
    ).fetchone()
    return _entry(entry)


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
