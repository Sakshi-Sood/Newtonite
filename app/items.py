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

from app import statuses as st
from app.errors import AppError
from app.notify import publish
from app.rules import ITEM_TYPES, PRIORITIES, allowed_actions, can

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[str, StringConstraints(max_length=20000)]
ItemType = Literal[ITEM_TYPES]
Priority = Literal[PRIORITIES]
# Times without a zone are taken as UTC, so they compare cleanly with stored timestamptz values.
AwareDatetime = Annotated[datetime, AfterValidator(lambda d: d if d.tzinfo else d.replace(tzinfo=timezone.utc))]
EDITABLE = ("title", "description", "priority", "due_at")  # plain detail fields

# A task together with its status. `closed` = in a finished status, or rejected by a lead.
ITEM_SQL = """
    SELECT w.*, s.name AS status_name, s.kind AS status_kind, s.color AS status_color,
           s.finished AS status_finished,
           (s.finished OR COALESCE(w.approval = 'rejected', false)) AS closed
    FROM work_items w JOIN statuses s ON s.id = w.status_id"""


class ItemCreate(BaseModel):
    team_id: uuid.UUID
    title: Title
    description: Description = ""
    type: ItemType
    priority: Priority
    requires_approval: bool = False
    restricted: bool = False
    due_at: AwareDatetime | None = None
    assignee_id: uuid.UUID | None = None


class ItemPatch(BaseModel):
    expected_version: int
    title: Title | None = None
    description: Description | None = None
    priority: Priority | None = None
    due_at: AwareDatetime | None = None  # explicit null clears the due date
    restricted: bool | None = None
    team_id: uuid.UUID | None = None      # move to another team
    assignee_id: uuid.UUID | None = None  # explicit null unassigns

    @model_validator(mode="after")
    def _check(self):
        sent = self.model_fields_set - {"expected_version"}
        if not sent:
            raise ValueError("Send at least one field to change.")
        for field in ("title", "description", "priority", "restricted", "team_id"):
            if field in sent and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self

    def changes(self) -> dict:
        fields = EDITABLE + ("restricted", "team_id", "assignee_id")
        return {f: getattr(self, f) for f in fields if f in self.model_fields_set}


class CommentIn(BaseModel):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)]


class AssignIn(BaseModel):
    assignee_id: uuid.UUID | None
    expected_version: int


class MoveIn(BaseModel):
    status_id: uuid.UUID
    expected_version: int


class ApprovalIn(BaseModel):
    decision: Literal["approve", "reject"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    expected_version: int


def _not_found() -> AppError:
    return AppError(404, "NOT_FOUND", "Work item not found.")


def role_in_team(conn: Connection, user_id, team_id) -> str | None:
    row = conn.execute(
        "SELECT role FROM memberships WHERE user_id = %s AND team_id = %s", (user_id, team_id)
    ).fetchone()
    return row["role"] if row else None


def access_role(conn: Connection, user_id, item: dict) -> str | None:
    """The caller's role for this item: their team role, or "requester" if they
    created it from outside the team, or None (no access)."""
    role = role_in_team(conn, user_id, item["team_id"])
    if role is None and str(item["created_by"]) == str(user_id):
        return "requester"
    return role


def load_item(conn: Connection, item_id: str, user_id, for_update: bool = False) -> tuple[dict, str]:
    """The item row and the caller's role for it.

    Missing items, malformed ids and items the caller can't see all look the
    same (404), so outsiders can't learn what exists.
    """
    try:
        item_uuid = uuid.UUID(str(item_id))
    except ValueError:
        raise _not_found()
    lock = " FOR UPDATE OF w" if for_update else ""
    row = conn.execute(f"{ITEM_SQL} WHERE w.id = %s{lock}", (item_uuid,)).fetchone()
    if row is None:
        raise _not_found()
    role = access_role(conn, user_id, row)
    if role is None:
        raise _not_found()
    return row, role


def require(item: dict, user_id, role: str, permission: str) -> None:
    if not can(item, user_id, role, permission):
        raise AppError(403, "FORBIDDEN", f"You are not allowed to {permission} this item.",
                       {"permission": permission})


def record_activity(conn: Connection, item_id, actor_id, kind: str, payload: dict, also_team=None) -> int:
    row = conn.execute(
        """INSERT INTO activity (work_item_id, actor_id, kind, payload)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (item_id, actor_id, kind, Jsonb(jsonable_encoder(payload))),
    ).fetchone()
    item = conn.execute(
        "UPDATE work_items SET last_activity_at = now() WHERE id = %s RETURNING team_id, created_by",
        (item_id,),
    ).fetchone()
    # `also_team` = the old team on a move, so its members hear about it too.
    teams = [str(item["team_id"])] + ([str(also_team)] if also_team else [])
    publish(conn, {"item_id": str(item_id), "team_ids": teams, "created_by": str(item["created_by"]),
                   "actor_id": str(actor_id), "kind": kind})
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
        "status": {"id": row["status_id"], "name": row["status_name"], "kind": row["status_kind"],
                   "color": row["status_color"], "finished": row["status_finished"]},
        "requires_approval": row["requires_approval"],
        "approval": row["approval"],
        "closed": row["closed"],
        "restricted": row["restricted"],
        "due_at": row["due_at"],
        "assignee": {"id": row["assignee_id"], "name": names["assignee_name"]} if row["assignee_id"] else None,
        "assigned_by": _user_ref(conn, row["assigned_by"]),
        "created_by": {"id": row["created_by"], "name": names["creator_name"]},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_activity_at": row["last_activity_at"],
        "version": row["version"],
        "my_role": role,
        "allowed_actions": allowed_actions(row, user_id, role),
    }


def _fresh(conn: Connection, item_id) -> dict:
    return conn.execute(f"{ITEM_SQL} WHERE w.id = %s", (item_id,)).fetchone()


def _fresh_dto(conn: Connection, item_id, user_id, role: str) -> dict:
    return item_dto(conn, _fresh(conn, item_id), user_id, role)


def _team_exists(conn: Connection, team_id) -> bool:
    return conn.execute("SELECT 1 FROM teams WHERE id = %s", (team_id,)).fetchone() is not None


def _check_assignee(conn: Connection, team_id, assignee_id) -> None:
    if assignee_id is not None and role_in_team(conn, assignee_id, team_id) is None:
        raise AppError(422, "INVALID_ASSIGNEE", "The assignee must be a member of the item's team.")


def create_item(conn: Connection, user_id, data: ItemCreate) -> dict:
    """Create an item in any team. Outside your own teams it's a request: you
    can follow, comment and edit it, and that team does the work."""
    if not _team_exists(conn, data.team_id):
        raise AppError(422, "INVALID_TEAM", "That team doesn't exist.")
    role = role_in_team(conn, user_id, data.team_id) or "requester"
    if data.assignee_id is not None:
        _check_assignee(conn, data.team_id, data.assignee_id)
        # Anyone in the team may assign anyone in it; a requester leaves that to the team.
        if role == "requester":
            raise AppError(403, "FORBIDDEN", "The team you're sending this to decides who works on it.",
                           {"permission": "assign"})
    if data.restricted and role == "lead":
        raise AppError(403, "FORBIDDEN", "Leads can always edit, so restricting is only for other creators.",
                       {"permission": "restrict"})
    todo = st.default_status_id(conn, data.team_id, "todo")
    row = conn.execute(
        """INSERT INTO work_items (team_id, title, description, type, priority, status_id,
                                   requires_approval, approval, restricted, due_at, assignee_id, assigned_by,
                                   created_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (data.team_id, data.title, data.description, data.type, data.priority, todo,
         data.requires_approval, "pending" if data.requires_approval else None, data.restricted, data.due_at,
         data.assignee_id, user_id if data.assignee_id else None, user_id),
    ).fetchone()
    fresh = _fresh(conn, row["id"])
    record_activity(conn, row["id"], user_id, "created",
                    {"title": fresh["title"], "priority": fresh["priority"], "status": fresh["status_name"]})
    if data.assignee_id is not None:
        record_activity(conn, row["id"], user_id, "assigned", _assignment_payload(conn, None, data.assignee_id))
    return _fresh_dto(conn, row["id"], user_id, role)


def get_item(conn: Connection, item_id: str, user_id) -> dict:
    row, role = load_item(conn, item_id, user_id)
    return item_dto(conn, row, user_id, role)


def stale_error(conn: Connection, item_id, user_id, role: str) -> AppError:
    """409 carrying the item as it is now and who changed it last, so the client can show a diff."""
    # Only kinds that bump the version count; a later comment didn't cause the conflict.
    last = conn.execute(
        """SELECT u.id, u.name FROM activity a JOIN users u ON u.id = a.actor_id
           WHERE a.work_item_id = %s AND a.kind IN ('updated', 'assigned', 'status_changed', 'approval')
           ORDER BY a.id DESC LIMIT 1""",
        (item_id,),
    ).fetchone()
    return AppError(409, "STALE_VERSION", "This item was changed since you loaded it.",
                    {"current": _fresh_dto(conn, item_id, user_id, role),
                     "changed_by": {"id": last["id"], "name": last["name"]} if last else None})


def _team_name(conn: Connection, team_id) -> str:
    return conn.execute("SELECT name FROM teams WHERE id = %s", (team_id,)).fetchone()["name"]


UPDATABLE = EDITABLE + ("restricted", "team_id", "assignee_id")


def update_item(conn: Connection, item_id: str, user_id, expected_version: int, changes: dict) -> dict:
    """Edit details, the restriction flag, the team (move) and/or the assignee in one
    versioned update. Only fields that actually change are checked and written."""
    row, role = load_item(conn, item_id, user_id)
    changed = {f: v for f, v in changes.items() if f in UPDATABLE and row[f] != v}
    if not changed:
        return item_dto(conn, row, user_id, role)
    if set(changed) - {"assignee_id"}:
        require(row, user_id, role, "edit")
    if "restricted" in changed:
        require(row, user_id, role, "restrict")

    team_id = changed.get("team_id", row["team_id"])
    if "team_id" in changed:
        if not _team_exists(conn, team_id):
            raise AppError(422, "INVALID_TEAM", "That team doesn't exist.")
        # The task keeps its place in the workflow: same default status, or the
        # custom one with the same name, otherwise To do.
        changed["status_id"] = st.equivalent_in(conn, row["status_id"], team_id)
    if "assignee_id" in changed:
        taking_it = str(changed["assignee_id"]) == str(user_id) and can(row, user_id, role, "claim")
        if not taking_it:
            require(row, user_id, role, "assign")
        _check_assignee(conn, team_id, changed["assignee_id"])
    elif "team_id" in changed and row["assignee_id"] and role_in_team(conn, row["assignee_id"], team_id) is None:
        changed["assignee_id"] = None  # moved away from the assignee's team
    if "assignee_id" in changed:
        changed["assigned_by"] = user_id if changed["assignee_id"] else None

    sets = ", ".join(f"{f} = %s" for f in changed)  # names come from UPDATABLE (+ computed columns) only
    updated = conn.execute(
        f"""UPDATE work_items SET {sets}, version = version + 1, updated_at = now()
            WHERE id = %s AND version = %s RETURNING id""",
        (*changed.values(), row["id"], expected_version),
    ).fetchone()
    if updated is None:
        raise stale_error(conn, row["id"], user_id, role)

    details = {f: {"from": row[f], "to": v} for f, v in changed.items()
               if f not in ("team_id", "assignee_id", "assigned_by", "status_id")}
    if "team_id" in changed:
        details["team"] = {"from": _team_name(conn, row["team_id"]), "to": _team_name(conn, team_id)}
        new_status = conn.execute("SELECT name FROM statuses WHERE id = %s", (changed["status_id"],)).fetchone()["name"]
        if new_status != row["status_name"]:
            details["status"] = {"from": row["status_name"], "to": new_status}
    if details:
        record_activity(conn, row["id"], user_id, "updated", {"changes": details},
                        also_team=row["team_id"] if "team_id" in changed else None)
    if "assignee_id" in changed:
        record_activity(conn, row["id"], user_id, "assigned",
                        _assignment_payload(conn, row["assignee_id"], changed["assignee_id"]))
    # After a move the caller may no longer have access; they then get allowed_actions = [].
    fresh = _fresh(conn, row["id"])
    return item_dto(conn, fresh, user_id, access_role(conn, user_id, fresh))


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
    if row["assignee_id"] is not None and not row["closed"]:
        raise _already_claimed(conn, row["id"])
    require(row, user_id, role, "claim")
    updated = conn.execute(
        """UPDATE work_items SET assignee_id = %s, assigned_by = %s, version = version + 1, updated_at = now()
           WHERE id = %s AND assignee_id IS NULL
             AND status_id IN (SELECT id FROM statuses WHERE NOT finished)
             AND approval IS DISTINCT FROM 'rejected'
           RETURNING id""",
        (user_id, user_id, row["id"]),
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
        """UPDATE work_items SET assignee_id = %s, assigned_by = %s, version = version + 1, updated_at = now()
           WHERE id = %s AND version = %s RETURNING id""",
        (assignee_id, user_id if assignee_id else None, row["id"], expected_version),
    ).fetchone()
    if updated is None:
        raise stale_error(conn, row["id"], user_id, role)
    record_activity(conn, row["id"], user_id, "assigned",
                    _assignment_payload(conn, row["assignee_id"], assignee_id))
    return _fresh_dto(conn, row["id"], user_id, role)


def move_item(conn: Connection, item_id: str, user_id, status_id, expected_version: int) -> dict:
    """Change a task's status to any status of its team. The assignee or a lead may
    do it, unless a lead still has to approve the task."""
    # Row lock: concurrent moves of one task run one after another.
    row, role = load_item(conn, item_id, user_id, for_update=True)
    if row["version"] != expected_version:
        raise stale_error(conn, row["id"], user_id, role)
    target = conn.execute("SELECT id, name, color, kind FROM statuses WHERE id = %s AND team_id = %s",
                          (status_id, row["team_id"])).fetchone()
    if target is None:
        raise AppError(422, "INVALID_STATUS", "That status doesn't belong to this task's team.")
    if row["approval"] in ("pending", "rejected"):
        raise AppError(422, "APPROVAL_REQUIRED",
                       "A team lead has to approve this task before its status can change."
                       if row["approval"] == "pending" else "A lead rejected this task, so its status is locked.")
    require(row, user_id, role, "move")
    if target["id"] == row["status_id"]:
        return item_dto(conn, row, user_id, role)
    conn.execute("UPDATE work_items SET status_id = %s, version = version + 1, updated_at = now() WHERE id = %s",
                 (target["id"], row["id"]))
    record_activity(conn, row["id"], user_id, "status_changed",
                    {"from": row["status_name"], "to": target["name"], "from_id": row["status_id"],
                     "to_id": target["id"], "to_color": target["color"]})
    return _fresh_dto(conn, row["id"], user_id, role)


def decide_approval(conn: Connection, item_id: str, user_id, decision: str, reason: str | None,
                    expected_version: int) -> dict:
    """A lead (never the task's creator) approves or rejects a task that needs approval.
    Until approved, the task's status can't change; a rejected task is closed, but any
    other lead can still approve it later."""
    row, role = load_item(conn, item_id, user_id, for_update=True)
    if row["version"] != expected_version:
        raise stale_error(conn, row["id"], user_id, role)
    state = row["approval"]
    if (decision == "approve" and state not in ("pending", "rejected")) or (decision == "reject" and state != "pending"):
        raise AppError(422, "INVALID_APPROVAL_STATE",
                       "This task isn't waiting for approval." if state in (None, "approved")
                       else "This task has already been rejected.")
    require(row, user_id, role, decision)
    new_state = "approved" if decision == "approve" else "rejected"
    conn.execute(
        """UPDATE work_items SET approval = %s, approval_by = %s, version = version + 1, updated_at = now()
           WHERE id = %s""", (new_state, user_id, row["id"]))
    record_activity(conn, row["id"], user_id, "approval",
                    {"decision": new_state, "reason": (reason or "").strip() or None})
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
