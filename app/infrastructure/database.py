"""Database configuration and session management."""

from __future__ import annotations

from collections.abc import Generator, Sequence

import logging
import re
import urllib.parse

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


settings = get_settings()

logger = logging.getLogger(__name__)

_ODBC_DRIVER_PATTERN = re.compile(r"(?i)driver\s*=\s*(\{[^}]+\}|[^;]+)")


def _pick_best_sql_server_driver(installed: Sequence[str]) -> str | None:
    """Return the newest installed SQL Server ODBC driver."""

    if not installed:
        return None

    def driver_sort_key(name: str) -> tuple[int, str]:
        version_match = re.search(r"(\d+)", name)
        version = int(version_match.group(1)) if version_match else -1
        return (version, name)

    return sorted(installed, key=driver_sort_key)[-1]


def _ensure_sql_server_driver(raw_connection: str) -> str:
    """Ensure the ODBC driver referenced in the connection string exists locally."""

    driver_match = _ODBC_DRIVER_PATTERN.search(raw_connection)
    if not driver_match:
        return raw_connection

    driver_token = driver_match.group(1).strip()
    brace_wrapped = driver_token.startswith("{") and driver_token.endswith("}")
    driver_name = driver_token[1:-1] if brace_wrapped else driver_token

    try:  # pragma: no cover - pyodbc not installed in unit tests
        import pyodbc
    except ModuleNotFoundError:  # pragma: no cover - fallback when pyodbc missing
        return raw_connection

    installed_drivers = pyodbc.drivers()
    lookup = {d.lower(): d for d in installed_drivers}
    normalized_name = driver_name.lower()
    if normalized_name in lookup:
        return raw_connection

    sql_server_drivers = [d for d in installed_drivers if "sql server" in d.lower()]
    replacement = _pick_best_sql_server_driver(sql_server_drivers)
    if replacement is None:
        raise RuntimeError(
            "The configured ODBC driver '%s' is not installed. Install it or update the connection string to use a "
            "driver that exists on this machine." % driver_name
        )

    logger.warning(
        "Configured ODBC driver '%s' is not installed. Falling back to '%s'.",
        driver_name,
        replacement,
    )

    replacement_token = f"{{{replacement}}}" if brace_wrapped else replacement
    return (
        raw_connection[: driver_match.start(1)]
        + replacement_token
        + raw_connection[driver_match.end(1) :]
    )


def build_odbc_conn_str(settings: Settings) -> str:
    """Build an Azure SQL / SQL Server ODBC connection string from settings."""

    return (
        f"Driver={{{settings.db_driver}}};"
        f"Server=tcp:{settings.db_server},{settings.db_port};"
        f"Database={settings.db_name};"
        f"Uid={settings.db_user};"
        f"Pwd={settings.db_password};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )


def _build_sqlalchemy_database_url(settings: "Settings") -> str:
    """Construct the SQLAlchemy URL for the configured SQL Server instance."""

    odbc_connection = _ensure_sql_server_driver(build_odbc_conn_str(settings))
    params = urllib.parse.quote_plus(odbc_connection)
    return f"mssql+pyodbc:///?odbc_connect={params}"


database_url = _build_sqlalchemy_database_url(settings)
engine = create_engine(database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def initialize_database() -> None:
    """Ensure all ORM models have corresponding database tables."""

    from app.infrastructure import models  # noqa: F401  # ensure models are imported

    Base.metadata.create_all(bind=engine, checkfirst=True)
    _ensure_user_columns()
    _ensure_rule_columns()
    _sync_rule_statuses()


def _ensure_user_columns() -> None:
    """Add non-destructive missing columns for legacy user tables."""

    inspector = inspect(engine)
    if "user" not in inspector.get_table_names():
        return

    existing_columns = {column["name"].lower() for column in inspector.get_columns("user")}
    if "send_emails" in existing_columns:
        return

    dialect = engine.dialect.name.lower()
    if dialect == "mssql":
        statement = (
            "ALTER TABLE [user] ADD [send_emails] BIT NOT NULL "
            "CONSTRAINT [DF_user_send_emails] DEFAULT 1"
        )
    else:
        statement = 'ALTER TABLE "user" ADD COLUMN "send_emails" BOOLEAN NOT NULL DEFAULT true'

    with engine.begin() as connection:
        connection.execute(text(statement))


def _ensure_rule_columns() -> None:
    """Add non-destructive missing columns for legacy rule tables."""

    inspector = inspect(engine)
    if "rule" not in inspector.get_table_names():
        return

    existing_columns = {column["name"].lower() for column in inspector.get_columns("rule")}
    dialect = engine.dialect.name.lower()
    statements: list[str] = []
    if "summary" not in existing_columns:
        if dialect == "mssql":
            statements.append("ALTER TABLE [rule] ADD [summary] NVARCHAR(MAX) NULL")
        else:
            statements.append('ALTER TABLE "rule" ADD COLUMN "summary" TEXT NULL')
    if "attachment" not in existing_columns:
        if dialect == "mssql":
            statements.append("ALTER TABLE [rule] ADD [attachment] NVARCHAR(MAX) NULL")
        else:
            statements.append('ALTER TABLE "rule" ADD COLUMN "attachment" TEXT NULL')
    if "status" not in existing_columns:
        if dialect == "mssql":
            statements.append(
                "ALTER TABLE [rule] ADD [status] NVARCHAR(20) NOT NULL CONSTRAINT [DF_rule_status] DEFAULT 'borrador'"
            )
        else:
            statements.append(
                'ALTER TABLE "rule" ADD COLUMN "status" VARCHAR(20) NOT NULL DEFAULT \'borrador\''
            )

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _sync_rule_statuses() -> None:
    """Backfill rule status for legacy data based on published template usage."""
    inspector = inspect(engine)
    table_names = {name.lower() for name in inspector.get_table_names()}
    required_tables = {"rule", "template", "template_column", "template_column_rule"}
    if not required_tables.issubset(table_names):
        return

    dialect = engine.dialect.name.lower()
    with engine.begin() as connection:
        if dialect == "mssql":
            connection.execute(
                text(
                    """
                    UPDATE r
                    SET [status] = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM [template_column_rule] tcr
                            INNER JOIN [template_column] tc
                                ON tc.[id] = tcr.[template_column_id]
                            INNER JOIN [template] t
                                ON t.[id] = tc.[template_id]
                            WHERE tcr.[rule_id] = r.[id]
                              AND tc.[deleted] = 0
                              AND t.[deleted] = 0
                              AND t.[status] = 'published'
                        ) THEN 'asignada'
                        ELSE 'borrador'
                    END
                    FROM [rule] r
                    WHERE r.[deleted] = 0
                    """
                )
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE "rule" AS r
                    SET "status" = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM "template_column_rule" AS tcr
                            INNER JOIN "template_column" AS tc
                                ON tc."id" = tcr."template_column_id"
                            INNER JOIN "template" AS t
                                ON t."id" = tc."template_id"
                            WHERE tcr."rule_id" = r."id"
                              AND tc."deleted" = false
                              AND t."deleted" = false
                              AND t."status" = 'published'
                        ) THEN 'asignada'
                        ELSE 'borrador'
                    END
                    WHERE r."deleted" = false
                    """
                )
            )


def get_db() -> Generator:
    """Yield a database session and close it afterwards."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


initialize_database()
