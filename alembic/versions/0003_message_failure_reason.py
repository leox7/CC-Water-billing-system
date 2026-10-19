"""message failure reason

A failed SMS keeps a short plain reason, chosen by the app from the provider's status code,
so the bill can say what to fix before Send again (PRD SMS-3). Empty unless the message failed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('messages_log', sa.Column('failure_reason', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('messages_log', 'failure_reason')
