from logging.config import fileConfig

from alembic import context

from app.core.config import get_settings
from app.core.database import Base, create_database_engine
from app import models  # noqa: F401  (importing the models is what registers the tables)

config = context.config

if config.config_file_name is not None:
    # keep the app's own loggers when this runs from inside the test suite
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    # The URL comes from the app settings (the .env file, or DATABASE_URL set by the
    # shell when the migration user runs this on the server, DEPLOYMENT.md section 9.1).
    engine = create_database_engine(get_settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
