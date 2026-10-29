"""phone method messages

The phone method (PRD SMS-14 to SMS-22): a message waits as "ready" until a user sends it
from their own phone and marks it sent, and a ready message whose bill is deleted becomes
"cancelled" (SMS-20). sent_by_user_id keeps who marked it sent (SMS-17). It is a plain
link with no cascade: a member who marked a message is deactivated, never deleted (AUTH-4).

The downgrade refuses while any ready or cancelled row exists: the old list of statuses
cannot hold them, and they are neither changed nor deleted to make it fit.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STATUSES = sa.Enum('pending', 'sent', 'failed', name='message_status')
# the new values go at the end, so the rows already saved keep their place in the list
NEW_STATUSES = sa.Enum('pending', 'sent', 'failed', 'ready', 'cancelled', name='message_status')


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('messages_log', 'status', existing_type=OLD_STATUSES, type_=NEW_STATUSES,
                    existing_nullable=False, existing_server_default='pending')
    op.add_column('messages_log', sa.Column('sent_by_user_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_messages_sent_by', 'messages_log', 'users', ['sent_by_user_id'], ['user_id'])


def downgrade() -> None:
    """Downgrade schema."""
    waiting = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM messages_log WHERE status IN ('ready', 'cancelled')")
    ).scalar()
    if waiting:
        raise RuntimeError(
            f"Cannot downgrade: messages_log has {waiting} ready or cancelled message(s), which the "
            "older schema cannot hold. Send and mark the ready messages first. Cancelled messages "
            "are history and are not removed by this migration."
        )
    op.drop_constraint('fk_messages_sent_by', 'messages_log', type_='foreignkey')
    op.drop_column('messages_log', 'sent_by_user_id')
    op.alter_column('messages_log', 'status', existing_type=NEW_STATUSES, type_=OLD_STATUSES,
                    existing_nullable=False, existing_server_default='pending')
