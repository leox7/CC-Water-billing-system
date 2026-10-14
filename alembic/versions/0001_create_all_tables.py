"""create all tables

Revision ID: 0001
Revises: 
Create Date: 2026-10-13 09:27:59.107583

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('audit_log',
    sa.Column('audit_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('created_at', mysql.TIMESTAMP(fsp=3), server_default=sa.text('CURRENT_TIMESTAMP(3)'), nullable=False),
    sa.Column('level', sa.Enum('info', 'warning', 'error', name='audit_level'), server_default='info', nullable=False),
    sa.Column('event_type', sa.String(length=50), nullable=False),
    sa.Column('summary', sa.String(length=255), nullable=False),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('actor_label', sa.String(length=100), nullable=True),
    sa.Column('entity_type', sa.String(length=30), nullable=True),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('request_id', sa.String(length=36), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('details', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('audit_id')
    )
    op.create_index('idx_audit_created', 'audit_log', ['created_at'], unique=False)
    op.create_index('idx_audit_customer', 'audit_log', ['customer_id', 'created_at'], unique=False)
    op.create_index('idx_audit_entity', 'audit_log', ['entity_type', 'entity_id'], unique=False)
    op.create_index('idx_audit_event', 'audit_log', ['event_type', 'created_at'], unique=False)
    op.create_index('idx_audit_request', 'audit_log', ['request_id'], unique=False)
    op.create_table('customers',
    sa.Column('customer_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('first_name', sa.String(length=50), nullable=False),
    sa.Column('last_name', sa.String(length=50), nullable=False),
    sa.Column('phone', sa.String(length=12), nullable=False),
    sa.Column('location', sa.String(length=100), nullable=False),
    sa.Column('meter_number', sa.String(length=20), nullable=False),
    sa.Column('rate_per_unit', sa.Numeric(precision=10, scale=2), server_default='130.00', nullable=False),
    sa.Column('service_charge', sa.Numeric(precision=10, scale=2), server_default='150.00', nullable=False),
    sa.Column('status', sa.Enum('active', 'inactive', name='customer_status'), server_default='active', nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.PrimaryKeyConstraint('customer_id'),
    sa.UniqueConstraint('meter_number', name='uq_customers_meter_number')
    )
    op.create_table('users',
    sa.Column('user_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('first_name', sa.String(length=50), nullable=False),
    sa.Column('last_name', sa.String(length=50), nullable=False),
    sa.Column('phone', sa.String(length=12), nullable=True),
    sa.Column('email', sa.String(length=120), nullable=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('owner', 'field_agent', name='user_role'), nullable=False),
    sa.Column('status', sa.Enum('active', 'inactive', name='user_status'), server_default='active', nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint('phone IS NOT NULL OR email IS NOT NULL', name='ck_users_phone_or_email'),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('email', name='uq_users_email'),
    sa.UniqueConstraint('phone', name='uq_users_phone')
    )
    op.create_table('meter_readings',
    sa.Column('reading_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('billing_period', sa.CHAR(length=7), nullable=False),
    sa.Column('previous_reading', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('current_reading', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('units_consumed', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('reading_date', sa.Date(), nullable=False),
    sa.Column('recorded_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint('current_reading >= previous_reading', name='ck_readings_not_lower'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.customer_id'], name='fk_readings_customer'),
    sa.ForeignKeyConstraint(['recorded_by'], ['users.user_id'], name='fk_readings_user'),
    sa.PrimaryKeyConstraint('reading_id'),
    sa.UniqueConstraint('customer_id', 'billing_period', name='uq_readings_customer_period')
    )
    op.create_table('bills',
    sa.Column('bill_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('reading_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('rate_per_unit', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('service_charge', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('units_consumed', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('consumption_charge', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('previous_balance', sa.Numeric(precision=12, scale=2), server_default='0.00', nullable=False),
    sa.Column('total_due', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('amount_paid', sa.Numeric(precision=12, scale=2), server_default='0.00', nullable=False),
    sa.Column('status', sa.Enum('unpaid', 'partial', 'paid', name='bill_status'), server_default='unpaid', nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.customer_id'], name='fk_bills_customer'),
    sa.ForeignKeyConstraint(['reading_id'], ['meter_readings.reading_id'], name='fk_bills_reading'),
    sa.PrimaryKeyConstraint('bill_id'),
    sa.UniqueConstraint('reading_id', name='uq_bills_reading')
    )
    op.create_index('idx_bills_customer', 'bills', ['customer_id', 'created_at'], unique=False)
    op.create_table('messages_log',
    sa.Column('message_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('bill_id', sa.Integer(), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('message_type', sa.Enum('bill', 'correction', name='message_type'), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'sent', 'failed', name='message_status'), server_default='pending', nullable=False),
    sa.Column('provider_message_id', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'), nullable=False),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.bill_id'], name='fk_messages_bill', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.customer_id'], name='fk_messages_customer'),
    sa.PrimaryKeyConstraint('message_id')
    )
    op.create_index('idx_messages_bill', 'messages_log', ['bill_id', 'created_at'], unique=False)
    op.create_index('idx_messages_customer', 'messages_log', ['customer_id'], unique=False)
    op.create_table('payments',
    sa.Column('payment_id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('bill_id', sa.Integer(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('payment_date', sa.Date(), nullable=False),
    sa.Column('method', sa.String(length=20), server_default='mpesa', nullable=False),
    sa.Column('mpesa_reference', sa.String(length=20), nullable=True),
    sa.Column('recorded_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint('amount > 0', name='ck_payments_amount_positive'),
    sa.ForeignKeyConstraint(['bill_id'], ['bills.bill_id'], name='fk_payments_bill'),
    sa.ForeignKeyConstraint(['recorded_by'], ['users.user_id'], name='fk_payments_user'),
    sa.PrimaryKeyConstraint('payment_id'),
    sa.UniqueConstraint('mpesa_reference', name='uq_payments_mpesa_reference')
    )
    op.create_index('idx_payments_bill', 'payments', ['bill_id'], unique=False)
    op.create_index('idx_payments_date', 'payments', ['payment_date'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Dropping a table drops its indexes too. Tables go in reverse order of their
    # foreign keys. (MySQL refuses to drop an index that a foreign key still needs.)
    op.drop_table('payments')
    op.drop_table('messages_log')
    op.drop_table('bills')
    op.drop_table('meter_readings')
    op.drop_table('users')
    op.drop_table('customers')
    op.drop_table('audit_log')
