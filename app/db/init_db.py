"""Database bootstrap: wait for DB -> apply Alembic migrations -> seed ontology.

Schema is owned entirely by Alembic now (no SQLAlchemy create_all). The initial
migration creates the pgvector/pg_trgm extensions and all 7 tables/indexes;
applying `upgrade head` on startup is idempotent. The ontology registry is
re-seeded (idempotent upsert) afterwards so it tracks app/db/ontology_seed.py.

Run standalone with:  python -m app.db.init_db
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from app.config import settings
from app.db.ontology_seed import seed_ontology
from app.db.session import engine

logger = logging.getLogger("company_brain.init_db")

# Repo root (where alembic.ini lives): app/db/init_db.py -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@retry(
    stop=stop_after_attempt(30),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(OperationalError),
    reraise=True,
)
def wait_for_db(eng: Engine = engine) -> None:
    """Block until the database accepts connections (container warm-up)."""
    with eng.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("Database is reachable.")


def _alembic_config() -> Config:
    """Build an Alembic Config that works regardless of the current directory,
    with the URL injected from centralized settings (no hardcoded credentials)."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


def run_migrations() -> None:
    logger.info("Applying database migrations (alembic upgrade head)...")
    command.upgrade(_alembic_config(), "head")
    logger.info("Migrations applied.")


def init_db(eng: Engine = engine) -> None:
    wait_for_db(eng)
    run_migrations()
    seed_ontology(eng)
    logger.info("Database ready (migrated + ontology seeded).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
