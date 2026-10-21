"""Work item endpoints. Thin: validate input, call app.items, return the result."""
from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from psycopg import Connection

from app import items, listing, projects
from app.auth import User
from app.db import Conn
from app.idempotency import request_hash, run_idempotent

router = APIRouter(prefix="/api/items")
overview_router = APIRouter(prefix="/api")


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


@overview_router.get("/overview")
def overview(user: User, conn: Conn):
    return {"counts": listing.overview_counts(conn, user["id"])}


@overview_router.get("/projects")
def visible_projects(user: User, conn: Conn, team_id: str | None = None):
    """Projects for pickers and filters (one team's, or those of the caller's own teams)."""
    return projects.list_visible(conn, user["id"], team_id)


@router.get("")
def list_items(
    user: User,
    conn: Conn,
    view: str | None = None,
    team_id: str | None = None,
    project: str | None = None,
    status: str | None = None,
    status_id: str | None = None,
    priority: str | None = None,
    type: str | None = None,
    assignee: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(25, ge=1, le=100),
):
    return listing.list_items(
        conn, user["id"], view=view, team_id=team_id, project=project, status=_csv(status), status_id=status_id,
        priority=_csv(priority), type=_csv(type), assignee=assignee, q=q, sort=sort, page=page,
        page_size=page_size,
    )


# Declared before /{item_id} so "counts" isn't taken for an item id.
@router.get("/counts")
def status_counts(
    user: User,
    conn: Conn,
    view: str | None = None,
    team_id: str | None = None,
    project: str | None = None,
    priority: str | None = None,
    type: str | None = None,
    assignee: str | None = None,
    q: str | None = None,
):
    """Tasks per status id for one team (`team_id` is required: statuses are per team)."""
    return listing.status_counts(conn, user["id"], view=view, team_id=team_id, project=project, priority=_csv(priority),
                                 type=_csv(type), assignee=assignee, q=q)


def _idempotent(request: Request, conn: Connection, user: dict, body, fn: Callable[[], tuple[int, dict]]):
    """Run a POST through the Idempotency-Key check; replays get a marker header."""
    h = request_hash(request.method, request.url.path, body)
    status, payload, replayed = run_idempotent(conn, user["id"], request.headers.get("Idempotency-Key"), h, fn)
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(payload, status_code=status, headers=headers)


@router.post("", status_code=201)
def create_item(
    body: items.ItemCreate, request: Request,
    user: User, conn: Conn,
):
    return _idempotent(request, conn, user, body.model_dump(mode="json"),
                       lambda: (201, items.create_item(conn, user["id"], body)))


@router.get("/{item_id}")
def get_item(item_id: str, user: User, conn: Conn):
    return items.get_item(conn, item_id, user["id"])


@router.patch("/{item_id}")
def update_item(
    item_id: str, body: items.ItemPatch,
    user: User, conn: Conn,
):
    return items.update_item(conn, item_id, user["id"], body.expected_version, body.changes())


@router.post("/{item_id}/comments", status_code=201)
def add_comment(
    item_id: str, body: items.CommentIn, request: Request,
    user: User, conn: Conn,
):
    return _idempotent(request, conn, user, body.model_dump(mode="json"),
                       lambda: (201, items.add_comment(conn, item_id, user["id"], body.body)))


@router.post("/{item_id}/claim")
def claim_item(item_id: str, request: Request, user: User, conn: Conn):
    return _idempotent(request, conn, user, None, lambda: (200, items.claim_item(conn, item_id, user["id"])))


@router.post("/{item_id}/assign")
def assign_item(
    item_id: str, body: items.AssignIn, request: Request,
    user: User, conn: Conn,
):
    return _idempotent(request, conn, user, body.model_dump(mode="json"), lambda: (
        200, items.assign_item(conn, item_id, user["id"], body.assignee_id, body.expected_version)))


@router.post("/{item_id}/status")
def move_item(
    item_id: str, body: items.MoveIn, request: Request,
    user: User, conn: Conn,
):
    return _idempotent(request, conn, user, body.model_dump(mode="json"), lambda: (
        200, items.move_item(conn, item_id, user["id"], body.status_id, body.expected_version, body.assignee_id)))


@router.post("/{item_id}/approval")
def decide_approval(
    item_id: str, body: items.ApprovalIn, request: Request,
    user: User, conn: Conn,
):
    return _idempotent(request, conn, user, body.model_dump(mode="json"), lambda: (
        200, items.decide_approval(conn, item_id, user["id"], body.decision, body.reason, body.expected_version)))


@router.get("/{item_id}/activity")
def get_activity(
    item_id: str,
    user: User,
    conn: Conn,
    before_id: int | None = None,
    limit: int = Query(50, ge=1, le=100),
):
    return items.list_activity(conn, item_id, user["id"], before_id, limit)
