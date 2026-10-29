"""Migrations run explicitly; API startup never creates or upgrades tables."""

import os

from alembic import context

from spec2repo.storage.database import make_engine
from spec2repo.storage.models import Base

config = context.config


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        dialect_name="mysql",
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
elif supplied := config.attributes.get("connection"):
    migrate(supplied)
else:
    url = os.environ.get("SPEC2REPO_DATABASE_URL")
    if not url or not url.startswith("mysql+pymysql://"):
        raise RuntimeError("Set SPEC2REPO_DATABASE_URL to the target mysql+pymysql database")
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
