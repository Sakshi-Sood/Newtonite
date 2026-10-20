"""Application entry point: uvicorn app.main:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import auth, routes
from app.config import DATABASE_URL, SECRET_KEY
from app.db import create_pool
from app.errors import install_handlers

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = create_pool(DATABASE_URL)
    yield
    app.state.pool.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Newtonite Ops", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=12 * 3600, same_site="lax")
    install_handlers(app)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(routes.overview_router)
    # Static frontend last, so /api routes take precedence.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True, check_dir=False), name="static")
    return app


app = create_app()
