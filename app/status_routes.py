"""Status management endpoints (per team; leads change them, members read them)."""
from fastapi import APIRouter, Response

from app import statuses
from app.auth import User
from app.db import Conn

router = APIRouter(prefix="/api/teams/{team_id}/statuses")


@router.get("")
def list_statuses(team_id: str, user: User, conn: Conn):
    return statuses.list_team(conn, user["id"], team_id)


@router.post("", status_code=201)
def create_status(team_id: str, body: statuses.StatusCreate, user: User, conn: Conn):
    return statuses.create_status(conn, user["id"], team_id, body)


# Declared before /{status_id} so "order" isn't taken for a status id.
@router.put("/order")
def reorder_statuses(team_id: str, body: statuses.StatusOrder, user: User, conn: Conn):
    return statuses.reorder(conn, user["id"], team_id, body.ids)


@router.patch("/{status_id}")
def update_status(team_id: str, status_id: str, body: statuses.StatusPatch, user: User, conn: Conn):
    return statuses.update_status(conn, user["id"], team_id, status_id, body)


@router.delete("/{status_id}", status_code=204)
def delete_status(team_id: str, status_id: str, user: User, conn: Conn, move_to: str | None = None):
    statuses.delete_status(conn, user["id"], team_id, status_id, move_to)
    return Response(status_code=204)
