"""Alembic environment.

Credentials come from app/config.py (never hardcoded). pgvector / pg_trgm are
handled explicitly so autogenerate never proposes to drop the `vector` type or
churn the operator-class indexes it can't faithfully round-trip.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.db.models  # noqa: F401 — imported for its side effect: populate metadata
from app.config import settings
from app.db.base import Base

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover
    Vector = None

config = context.config

# Inject the URL from centralized settings rather than alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    # disable_existing_loggers=False so the app's loggers survive migrations
    # run programmatically inside the FastAPI process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# These indexes use pgvector/pg_trgm operator classes that Alembic cannot
# reflect faithfully; they are created in the migration via op.create_index and
# excluded from autogenerate comparison so they never produce drop/create churn.
_MANAGED_INDEXES = {"ix_nodes_embedding_hnsw", "ix_nodes_name_trgm"}


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "index" and name in _MANAGED_INDEXES:
        return False
    return True


def compare_type(context_, inspected_column, metadata_column, inspected_type, metadata_type):  # noqa: ANN001
    # pgvector columns reflect as USER-DEFINED 'vector'; never flag them as changed.
    if Vector is not None and isinstance(metadata_type, Vector):
        return False
    if "vector" in str(inspected_type).lower():
        return False
    return None  # fall back to Alembic's default type comparison


def render_item(type_, obj, autogen_context):  # noqa: ANN001
    # Render pgvector columns as pgvector.sqlalchemy.Vector(dim=N) in autogen output.
    if Vector is not None and type_ == "type" and isinstance(obj, Vector):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.Vector(dim={obj.dim})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=compare_type,
        include_object=include_object,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
            include_object=include_object,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
