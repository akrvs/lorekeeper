"""raw_documents.extracted_hash — skip re-extraction of unchanged documents

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-29
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('raw_documents', sa.Column('extracted_hash', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('raw_documents', 'extracted_hash')
