"""proposals — the self-maintenance change queue

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-13
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('proposals',
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('agent', sa.Text(), nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('dedup_key', sa.Text(), nullable=True),
    sa.Column('reviewed_by', sa.Text(), nullable=True),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('applied_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('rollback_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_proposals')),
    sa.UniqueConstraint('kind', 'dedup_key', name='uq_proposals_dedup')
    )
    op.create_index('ix_proposals_status', 'proposals', ['status'], unique=False)
    op.create_index('ix_proposals_kind', 'proposals', ['kind'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_proposals_kind', table_name='proposals')
    op.drop_index('ix_proposals_status', table_name='proposals')
    op.drop_table('proposals')
