"""Settings loaded from the environment / .env file."""
import os
import warnings
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = quote(os.getenv("DB_USER", "postgres"), safe="")
    password = quote(os.getenv("DB_PASSWORD", ""), safe="")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "newtonite")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


DATABASE_URL = _database_url()

SECRET_KEY = os.getenv("SECRET_KEY") or ""
if not SECRET_KEY:
    warnings.warn("SECRET_KEY is not set; using an insecure development key.")
    SECRET_KEY = "dev-only-insecure-key"
