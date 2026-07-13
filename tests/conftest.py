"""Pytest fixtures (Track 2).

Two database fixtures:
  * `db` — a session wrapped in a transaction rolled back at teardown
    (`create_savepoint` mode lets code under test call commit() without escaping
    the outer rollback). Total isolation; nothing leaks between tests/threads.
  * `committed_graph` — really commits the demo graph (and truncates it after),
    for tests that cross connection boundaries (MCP tools open their own session;
    the stdio test spawns a subprocess).

DB-backed tests skip cleanly when no database is reachable.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cache import get_cache
from app.db.init_db import init_db
from app.db.session import SessionLocal, engine

_TABLES = (
    "node_mentions, edges, nodes, raw_documents, ingestion_runs, "
    "audit_log, access_grants, proposals"
)


def _db_reachable() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


_DB_READY = _db_reachable()


@pytest.fixture(scope="session")
def schema():
    if not _DB_READY:
        pytest.skip("no database reachable (set POSTGRES_* env vars)")
    init_db(engine)  # migrate + seed once for the whole session
    return True


@pytest.fixture
def db(schema) -> Session:
    conn = engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def provider():
    from tests.fixtures import ScriptedProvider

    return ScriptedProvider()


@pytest.fixture
def committed_graph(schema):
    """Commit the demo graph (visible across connections); clean up afterwards."""
    from tests.fixtures import ScriptedProvider, populate

    with SessionLocal() as s:
        populate(s, ScriptedProvider())
    yield
    with SessionLocal() as s:
        s.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
        s.commit()
    get_cache().bump("graph")
