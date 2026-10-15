"""Alembic environment. Migrations always run as the owner role (DATABASE_ADMIN_URL)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import database_admin_url

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)


def run_migrations_offline() -> None:
    context.configure(url=database_admin_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_admin_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
