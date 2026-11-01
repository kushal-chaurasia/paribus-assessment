import sqlite3

from alembic import command

from core.config import get_settings


def _tables() -> set[str]:
    path = get_settings().database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_models_match_latest_migration(alembic_config, migrated_db):
    # Raises if autogenerate finds model changes without a migration.
    command.check(alembic_config)


def test_migrations_downgrade_and_reapply(alembic_config, migrated_db):
    assert {"batches", "batch_rows"} <= _tables()

    command.downgrade(alembic_config, "base")
    assert _tables() == {"alembic_version"}

    command.upgrade(alembic_config, "head")
    assert {"batches", "batch_rows"} <= _tables()
