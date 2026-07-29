"""ingestion_runs.resource_key — scope incremental-sync cursors per resource

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-29
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('ingestion_runs', sa.Column('resource_key', sa.Text(), nullable=True))
    op.create_index(
        'ix_ingestion_runs_source_resource',
        'ingestion_runs',
        ['source_system', 'resource_key'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_ingestion_runs_source_resource', table_name='ingestion_runs')
    op.drop_column('ingestion_runs', 'resource_key')
