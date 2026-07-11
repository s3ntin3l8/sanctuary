import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.models.database import (
    Base,
)

config = context.config

# Only configure logging if we are running standalone (not via the app)
if config.config_file_name is not None and not os.getenv("SANCTUARY_APP"):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def include_object(obj, name, type_, reflected, compare_to):
    """Exclude vec0 virtual tables and their shadow tables from autogenerate.

    Both document_chunk_vectors and claim_vectors are vec0 virtual tables;
    vec0 creates several `*_chunks`, `*_info`, `*_rowids`, `*_vector_chunks*`
    shadow tables alongside the parent table. None of them belong in
    autogenerate output. (document_chunks — the real, non-vec0 table
    document_chunk_vectors joins against — is NOT excluded; it's ordinary
    SQLAlchemy metadata.)
    """
    vec_prefixes = ("document_chunk_vectors", "claim_vectors")
    if type_ == "table":
        return not any(
            name == prefix or name.startswith(prefix + "_") for prefix in vec_prefixes
        )
    if type_ == "index":
        return not (
            name.startswith("sqlite_butoindex_")
            or any(name.startswith(prefix + "_") for prefix in vec_prefixes)
        )
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def _load_extensions(dbapi_conn, _):
    """Load sqlite-vec extension so vec0 virtual tables work in migrations."""
    try:
        import sqlite_vec

        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)
    except Exception:
        pass


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    from sqlalchemy import event as sa_event

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    sa_event.listen(connectable, "connect", _load_extensions)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
