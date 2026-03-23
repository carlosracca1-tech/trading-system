"""
Alembic environment configuration.

Key points:
- DATABASE_URL is read from the environment (not from alembic.ini)
- All models are imported here so Alembic detects schema changes
- autogenerate compares the current DB to the SQLAlchemy metadata
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Make sure the project root is in sys.path ─────────────────────────────────
# This allows Alembic to import models and settings without installation.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings

# ── Import ALL models so their metadata is picked up by autogenerate ──────────
# Add every new model file here as you create it.
from packages.shared.models.base import Base  # noqa: F401 — Base.metadata

# Future imports (uncomment as models are created):
# from packages.shared.models.assets import Asset          # noqa: F401
# from packages.shared.models.market_data import MarketDataDaily  # noqa: F401
# from packages.shared.models.runs import StrategyRun      # noqa: F401
# from packages.shared.models.signals import Signal        # noqa: F401
# from packages.shared.models.orders import Order          # noqa: F401
# from packages.shared.models.positions import Position    # noqa: F401

# ── Alembic Config ────────────────────────────────────────────────────────────
config = context.config

# Wire up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url with the environment variable
# This is the only place credentials are injected — never in alembic.ini
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Used when generating SQL scripts without a live DB connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode (against a live database).
    Used in normal deploy flow: `alembic upgrade head`
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # no pooling during migrations
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
