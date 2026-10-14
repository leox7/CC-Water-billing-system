"""Database engine, session and the get_db dependency."""

from collections.abc import Iterator
from datetime import datetime
from functools import lru_cache

from sqlalchemy import TIMESTAMP, Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Kenya has no daylight saving, so a fixed offset is enough (DEPLOYMENT.md section 8).
# It is set on every connection, so the shared MySQL server setting is not touched.
EAT_TIME_ZONE_COMMAND = "SET time_zone = '+03:00'"


class Base(DeclarativeBase):
    """Parent of every model in app/models/."""

    # Every datetime column is a MySQL TIMESTAMP, as in the reference schema
    type_annotation_map = {datetime: TIMESTAMP}


def create_database_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        # check the connection before use, so a dropped one is replaced quietly
        pool_pre_ping=True,
        connect_args={"charset": "utf8mb4", "init_command": EAT_TIME_ZONE_COMMAND},
    )


@lru_cache
def get_engine() -> Engine:
    return create_database_engine(get_settings().database_url)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        # close() also rolls back anything a service did not commit
        session.close()
