"""owner and member roles

The "field agent" role is replaced by "member" (PRD section 3). Existing field_agent
accounts become members. The enum is first widened so both names are allowed, the rows
are converted, and then the old name is dropped. The downgrade does the same in reverse.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# "owner" stays first: MySQL sorts an ENUM by its place in the list, and the team list
# relies on the owner coming first
OLD_ROLES = sa.Enum('owner', 'field_agent', name='user_role')
BOTH_ROLES = sa.Enum('owner', 'field_agent', 'member', name='user_role')
NEW_ROLES = sa.Enum('owner', 'member', name='user_role')


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('users', 'role', existing_type=OLD_ROLES, type_=BOTH_ROLES, existing_nullable=False)
    op.execute("UPDATE users SET role = 'member' WHERE role = 'field_agent'")
    op.alter_column('users', 'role', existing_type=BOTH_ROLES, type_=NEW_ROLES, existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('users', 'role', existing_type=NEW_ROLES, type_=BOTH_ROLES, existing_nullable=False)
    op.execute("UPDATE users SET role = 'field_agent' WHERE role = 'member'")
    op.alter_column('users', 'role', existing_type=BOTH_ROLES, type_=OLD_ROLES, existing_nullable=False)
