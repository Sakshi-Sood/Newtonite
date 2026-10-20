"""Work item endpoints. Thin: validate input, call app.items, return the result."""
from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from app import items
from app.auth import current_user
from app.db import get_conn

router = APIRouter(prefix="/api/items")


@router.post("", status_code=201)
def create_item(body: items.ItemCreate, user: dict = Depends(current_user), conn: Connection = Depends(get_conn)):
    return items.create_item(conn, user["id"], body)


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
