"""Live updates: Postgres LISTEN/NOTIFY -> Server-Sent Events.

Every change commits a NOTIFY on `item_changes` (see items.record_activity).
Each app process keeps ONE listening database connection in a background
thread and fans each notification out to the browser tabs connected to
GET /api/events that are allowed to see that item. Browser connections hold
no database connection, so thousands of open tabs are cheap.

The browser only gets "something changed in item X"; it decides whether to
show the "New changes - Refresh" banner, and fetches data only when the user
clicks Refresh. Content always comes through the normal, authorised API.
"""
import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.auth import User
from app.notify import CHANNEL

log = logging.getLogger("app.events")
router = APIRouter(prefix="/api")

PING_SECONDS = 25        # keeps proxies from closing an idle stream
TEAMS_REFRESH_SECONDS = 60  # pick up membership changes on open streams


@dataclass(eq=False)
class Subscriber:
    user_id: str
    team_ids: set[str]
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200))

    def wants(self, event: dict) -> bool:
        return event.get("created_by") == self.user_id or bool(self.team_ids & set(event.get("team_ids", [])))


class Broker:
    def __init__(self, database_url: str):
        self.url = database_url
        self.subscribers: set[Subscriber] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle (called from the app lifespan) --
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._thread = threading.Thread(target=self._listen, name="pg-listen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._send_all({"type": "closing"})

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- subscriptions --
    def subscribe(self, user_id: str, team_ids: set[str]) -> Subscriber:
        sub = Subscriber(user_id, team_ids)
        with self._lock:
            self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self.subscribers.discard(sub)

    # -- delivery (runs on the listener thread) --
    def _deliver(self, sub: Subscriber, event: dict) -> None:
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A tab that stopped reading: tell it to resync instead of growing forever.
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.queue.put_nowait({"type": "resync"})

    def _send(self, event: dict) -> None:
        with self._lock:
            targets = [s for s in self.subscribers if s.wants(event)]
        for sub in targets:
            self.loop.call_soon_threadsafe(self._deliver, sub, event)

    def _send_all(self, event: dict) -> None:
        if not self.loop:
            return
        with self._lock:
            targets = list(self.subscribers)
        for sub in targets:
            self.loop.call_soon_threadsafe(self._deliver, sub, event)

    def _listen(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.url, autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    if not first:
                        # We may have missed changes while disconnected.
                        self._send_all({"type": "resync"})
                    first = False
                    while not self._stop.is_set():
                        for note in conn.notifies(timeout=1.0):
                            try:
                                event = json.loads(note.payload)
                            except ValueError:
                                continue
                            self._send({"type": "change", **event})
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("Live-update listener lost its database connection; retrying in 3s")
                time.sleep(3)


def _team_ids(pool, user_id: str) -> set[str]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT team_id::text AS id FROM memberships WHERE user_id = %s", (user_id,))
        return {r["id"] for r in rows}


@router.get("/events")
async def events(request: Request, user: User):
    """Server-Sent Events stream of 'item X changed' notices for this user."""
    broker: Broker = request.app.state.broker
    pool = request.app.state.pool
    user_id = str(user["id"])
    sub = broker.subscribe(user_id, await run_in_threadpool(_team_ids, pool, user_id))

    async def stream():
        try:
            yield "retry: 3000\n\n"  # browser reconnect delay
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            refreshed = time.monotonic()
            while not broker.stopping:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=PING_SECONDS)
                    if event.get("type") == "closing":
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if await request.is_disconnected():
                    break
                if time.monotonic() - refreshed > TEAMS_REFRESH_SECONDS:
                    sub.team_ids = await run_in_threadpool(_team_ids, pool, user_id)
                    refreshed = time.monotonic()
        finally:
            broker.unsubscribe(sub)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
