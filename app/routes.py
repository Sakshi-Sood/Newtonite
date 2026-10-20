"""Work item endpoints. Thin: validate input, call app.items, return the result."""
from typing import Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from psycopg import Connection

from app import items
from app.auth import current_user
from app.db import get_conn
from app.idempotency import request_hash, run_idempotent

router = APIRouter(prefix="/api/items")


def _idempotent(request: Request, conn: Connection, user: dict, body, fn: Callable[[], tuple[int, dict]]):
    """Run a POST through the Idempotency-Key check; replays get a marker header."""
    h = request_hash(request.method, request.url.path, body)
    status, payload, replayed = run_idempotent(conn, user["id"], request.headers.get("Idempotency-Key"), h, fn)
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(payload, status_code=status, headers=headers)


@router.post("", status_code=201)
def create_item(
    body: items.ItemCreate, request: Request,
    user: dict = Depends(current_user), conn: Connection = Depends(get_conn),
):
    return _idempotent(request, conn, user, body.model_dump(mode="json"),
                       lambda: (201, items.create_item(conn, user["id"], body)))


@router.get("/{item_id}")
def get_item(item_id: str, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return items.get_item(conn, item_id, user["id"])


@router.get("/{item_id}/activity")
def get_activity(
    item_id: str,
    before_id: int | None = None,
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(current_user),
    conn: Connection = Depends(get_conn),
):
    return items.list_activity(conn, item_id, user["id"], before_id, limit)
