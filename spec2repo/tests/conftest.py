import json
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

from spec2repo.config import Settings
from spec2repo.storage.database import make_engine
from spec2repo.storage.models import Base

ROOT = Path(__file__).resolve().parents[1]


def migrate(engine):
    config = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@pytest.fixture(scope="session")
def mysql_engine():
    url = os.environ.get("SPEC2REPO_TEST_MYSQL_URL")
    if not url:
        pytest.skip("MySQL not configured; use scripts/run_mysql_tests.py")
    parsed = make_url(url)
    if parsed.drivername != "mysql+pymysql" or parsed.database != "spec2repo_test":
        pytest.fail("Refusing to clear any schema except mysql+pymysql / spec2repo_test")
    engine = make_engine(url)
    migrate(engine)  # Configured but unavailable database is a failure, never a skip.
    yield engine
    engine.dispose()


@pytest.fixture(params=["sqlite", pytest.param("mysql", marks=pytest.mark.mysql)])
def db_engine(request, tmp_path):
    if request.param == "mysql":
        engine = request.getfixturevalue("mysql_engine")
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'control.db'}", hide_parameters=True)

        @event.listens_for(engine, "connect")
        def sqlite_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        migrate(engine)
    # Explicitly isolated test schema / temporary SQLite file; not production data.
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    yield engine
    if request.param == "sqlite":
        engine.dispose()


@pytest.fixture
def api_settings():
    return Settings(
        "mysql+pymysql://unused@localhost/spec2repo_test",
        {"alice": "test-alice-" + "a" * 24, "bob": "test-bob-" + "b" * 24},
    )


@pytest.fixture
def spec_payload():
    path = Path(__file__).resolve().parents[1] / "examples" / "task_api.spec.json"
    return json.loads(path.read_text(encoding="utf-8"))
