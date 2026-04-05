"""SQLite schema migration manager for agent-kernel persistence layer.

Provides deterministic, versioned schema migrations for all SQLite stores.
Each migration is registered once at module import time and applied
idempotently via a ``schema_migrations`` bookkeeping table.

Usage::

    from agent_kernel.kernel.persistence.migrations import
    SchemaMigrationManager

    manager = SchemaMigrationManager(conn)
    manager.apply_all()  # idempotent: only runs pending migrations
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Migration:
    """Describes a single, versioned schema migration.

    Attributes:
        version: Monotonically increasing integer version (1-based).
        description: Human-readable summary of what this migration does.
        sql: DDL/DML SQL to execute when applying this migration.

    """

    version: int
    description: str
    sql: str


@dataclass(slots=True)
class SchemaMigrationManager:
    """Applies pending SQLite schema migrations idempotently.

    Maintains a ``schema_migrations`` table that records which versions have
    been applied.  Migrations are applied in ascending version order within a
    single transaction.

    Args:
        connection: An open ``sqlite3.Connection`` to the target database.

    """

    _connection: sqlite3.Connection
    _migrations: list[Migration] = field(default_factory=list)

    def __init__(
        self,
        connection: sqlite3.Connection,
        extra_migrations: list[Migration] | None = None,
    ) -> None:
        """Initialise with a connection and optional extra migrations.

        Args:
            connection: Open SQLite connection.
            extra_migrations: Additional migrations to apply after the
                kernel-built-in migrations.  Must have unique version numbers
                that do not overlap with built-in versions.

        """
        self._connection = connection
        self._migrations = list(KERNEL_MIGRATIONS)
        if extra_migrations:
            for m in extra_migrations:
                self._migrations.append(m)
        self._migrations.sort(key=lambda m: m.version)
        seen_versions: set[int] = set()
        for m in self._migrations:
            if m.version in seen_versions:
                raise ValueError(f"Duplicate migration version {m.version!r} detected.")
            seen_versions.add(m.version)

    def apply_all(self) -> int:
        """Apply all pending migrations in version order.

        Creates the ``schema_migrations`` bookkeeping table if absent.
        Applies each pending migration in a transaction.

        Returns:
            Number of migrations applied in this call (0 = already up to date).

        """
        self._ensure_migrations_table()
        applied = self._applied_versions()
        pending = [m for m in self._migrations if m.version not in applied]
        for migration in sorted(pending, key=lambda m: m.version):
            self._apply(migration)
        return len(pending)

    def _ensure_migrations_table(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                description TEXT    NOT NULL,
                applied_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._connection.commit()

    def _applied_versions(self) -> frozenset[int]:
        cursor = self._connection.execute("SELECT version FROM schema_migrations")
        return frozenset(row[0] for row in cursor.fetchall())

    def _apply(self, migration: Migration) -> None:
        logger.info(
            "Applying schema migration v%d: %s",
            migration.version,
            migration.description,
        )
        with self._connection:
            self._connection.executescript(migration.sql)
            self._connection.execute(
                "INSERT INTO schema_migrations (version, description) VALUES(?, ?)",
                (migration.version, migration.description),
            )
        logger.info("Migration v%d applied successfully.", migration.version)


# ---------------------------------------------------------------------------
# Kernel-built-in migrations
# ---------------------------------------------------------------------------

KERNEL_MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description="Initial schema: runtime_events, turn_intents,dedupe_store, recovery_outcomes",
        sql="""
        CREATE TABLE IF NOT EXISTS runtime_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT    NOT NULL,
            commit_offset INTEGER NOT NULL,
            event_type    TEXT    NOT NULL,
            payload_json  TEXT    NOT NULL,
            occurred_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (run_id, commit_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_runtime_events_run_id
            ON runtime_events (run_id);

        CREATE TABLE IF NOT EXISTS turn_intents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT    NOT NULL,
            turn_offset   INTEGER NOT NULL,
            intent_json   TEXT    NOT NULL,
            recorded_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (run_id, turn_offset)
        );
        CREATE INDEX IF NOT EXISTS idx_turn_intents_run_id
            ON turn_intents (run_id);

        CREATE TABLE IF NOT EXISTS dedupe_store (
            idempotency_key TEXT PRIMARY KEY,
            state           TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS recovery_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT    NOT NULL,
            action_id       TEXT    NOT NULL,
            recovery_mode   TEXT    NOT NULL,
            reason          TEXT    NOT NULL,
            recorded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_recovery_outcomes_run_id
            ON recovery_outcomes (run_id);
        """,
    ),
    Migration(
        version=2,
        description="Add schema_version column to runtime_events for snapshotversioning",
        sql="""
        ALTER TABLE runtime_events ADD COLUMN schema_version TEXT NOT NULL
        DEFAULT '1';
        """,
    ),
]
