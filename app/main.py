"""Application entry point: uvicorn app.main:app --reload --timeout-graceful-shutdown 3

(Live-update streams stay open; the shutdown timeout lets a restart close them.)
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import admin_routes, auth, events, profile, routes, status_routes, type_routes
from app.config import DATABASE_URL, SECRET_KEY
from app.db import create_pool
from app.errors import install_handlers

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class RevalidatingStatic(StaticFiles):
    """Static files the browser must re-check on every load (a cheap 304 when unchanged),
    so a changed script is never hidden behind a stale cached copy."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = create_pool(DATABASE_URL)
    app.state.broker = events.Broker(DATABASE_URL)
    app.state.broker.start(asyncio.get_running_loop())
    yield
    app.state.broker.stop()
    app.state.pool.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Newtonite Ops", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=12 * 3600, same_site="lax")
    install_handlers(app)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(routes.overview_router)
    app.include_router(status_routes.router)
    app.include_router(type_routes.router)
    app.include_router(profile.router)
    app.include_router(admin_routes.router)
    app.include_router(events.router)
    # Static frontend last, so /api routes take precedence.
    app.mount("/", RevalidatingStatic(directory=STATIC_DIR, html=True, check_dir=False), name="static")
    return app


app = create_app()
