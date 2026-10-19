"""Migration 0002: field agents become members, and the downgrade turns them back.
Migration 0003: messages_log gets failure_reason, and the downgrade takes it away.

Runs the real migrations on the test database (DB-5), and always leaves it at head with
no rows behind.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.conftest import PROJECT_ROOT


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def role_column(connection) -> str:
    return connection.execute(text(
        "SELECT column_type FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'role'"
    )).scalar()


def role_of(connection, phone: str) -> str:
    return connection.execute(text("SELECT role FROM users WHERE phone = :phone"), {"phone": phone}).scalar()


def test_0002_turns_field_agents_into_members_and_back(test_engine):
    config = alembic_config()
    phone = "254799000123"
    command.downgrade(config, "0001")
    try:
        with test_engine.begin() as connection:
            assert role_column(connection) == "enum('owner','field_agent')"
            connection.execute(text(
                "INSERT INTO users (first_name, last_name, phone, password_hash, role) "
                "VALUES ('Peter', 'Otieno', :phone, 'x', 'field_agent')"), {"phone": phone})

        command.upgrade(config, "0002")
        with test_engine.connect() as connection:
            assert role_column(connection) == "enum('owner','member')"
            assert role_of(connection, phone) == "member"

        command.downgrade(config, "0001")
        with test_engine.connect() as connection:
            assert role_column(connection) == "enum('owner','field_agent')"
            assert role_of(connection, phone) == "field_agent"
    finally:
        with test_engine.begin() as connection:
            connection.execute(text("DELETE FROM users WHERE phone = :phone"), {"phone": phone})
        command.upgrade(config, "head")

    with test_engine.connect() as connection:
        assert role_column(connection) == "enum('owner','member')"
        assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar() == 0


def failure_reason_column(connection):
    return connection.execute(text(
        "SELECT column_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'messages_log' AND column_name = 'failure_reason'"
    )).one_or_none()


def test_0003_adds_failure_reason_to_messages_log_and_takes_it_away(test_engine):
    config = alembic_config()
    command.downgrade(config, "0002")
    try:
        with test_engine.connect() as connection:
            assert failure_reason_column(connection) is None

        command.upgrade(config, "0003")
        with test_engine.connect() as connection:
            assert tuple(failure_reason_column(connection)) == ("varchar(255)", "YES")

        command.downgrade(config, "0002")
        with test_engine.connect() as connection:
            assert failure_reason_column(connection) is None
    finally:
        command.upgrade(config, "head")

    with test_engine.connect() as connection:
        assert tuple(failure_reason_column(connection)) == ("varchar(255)", "YES")
