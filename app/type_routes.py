"""Task type endpoints (per team; its leads and admins change them, everyone signed in can read them)."""
from fastapi import APIRouter, Response

from app import task_types
from app.auth import User
from app.db import Conn

router = APIRouter(prefix="/api/teams/{team_id}/types")


@router.get("")
def list_types(team_id: str, user: User, conn: Conn):
    return task_types.list_team(conn, team_id)


@router.post("", status_code=201)
def create_type(team_id: str, body: task_types.TypeIn, user: User, conn: Conn):
    return task_types.create_type(conn, user, team_id, body)


@router.patch("/{type_id}")
def rename_type(team_id: str, type_id: str, body: task_types.TypeIn, user: User, conn: Conn):
    return task_types.rename_type(conn, user, team_id, type_id, body)


@router.delete("/{type_id}", status_code=204)
def delete_type(team_id: str, type_id: str, user: User, conn: Conn, move_to: str | None = None):
    task_types.delete_type(conn, user, team_id, type_id, move_to)
    return Response(status_code=204)
