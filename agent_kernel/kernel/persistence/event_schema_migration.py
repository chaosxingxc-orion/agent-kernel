"""Runtime event schema migration utilities."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace

from agent_kernel.kernel.contracts import RuntimeEvent


class SchemaMigrationError(RuntimeError):
    """Raised when no valid schema migration path exists."""


class EventSchemaMigrator:
    """Composable migration engine for RuntimeEvent schema versions.

    Migration functions are pure ``RuntimeEvent -> RuntimeEvent`` transforms.
    The engine computes a path between source and target schema versions and
    applies each migration step in order.
    """

    def __init__(self) -> None:
        """Initialize empty migration registry."""
        self._migrations: dict[tuple[str, str], Callable[[RuntimeEvent], RuntimeEvent]] = {}

    def register(
        self,
        from_version: str,
        to_version: str,
        fn: Callable[[RuntimeEvent], RuntimeEvent],
    ) -> None:
        """Register one directed migration step.

        Args:
            from_version: Source schema version.
            to_version: Target schema version.
            fn: Migration function for this edge.

        Raises:
            ValueError: If ``from_version == to_version``.

        """
        if from_version == to_version:
            raise ValueError("from_version and to_version must differ.")
        self._migrations[(from_version, to_version)] = fn

    def migrate(self, event: RuntimeEvent, target_version: str) -> RuntimeEvent:
        """Migrate one event to the target schema version.

        Args:
            event: Source runtime event.
            target_version: Desired schema version.

        Returns:
            Migrated event. Original event is unchanged.

        Raises:
            SchemaMigrationError: If no migration path exists.

        """
        if event.schema_version == target_version:
            return event

        path = self._resolve_path(event.schema_version, target_version)
        if not path:
            raise SchemaMigrationError(
                f"No schema migration path from {event.schema_version!r} to {target_version!r}."
            )

        migrated = event
        for from_version, to_version in path:
            migrate_fn = self._migrations[(from_version, to_version)]
            migrated = migrate_fn(migrated)
            if migrated.schema_version != to_version:
                migrated = replace(migrated, schema_version=to_version)

        return self._attach_original_schema_version(event, migrated)

    def migrate_batch(
        self,
        events: list[RuntimeEvent],
        target_version: str,
    ) -> list[RuntimeEvent]:
        """Migrate a batch of events to the target version."""
        return [self.migrate(event, target_version) for event in events]

    def _resolve_path(
        self,
        from_version: str,
        target_version: str,
    ) -> list[tuple[str, str]]:
        """Resolve shortest directed migration path with BFS."""
        queue: deque[str] = deque([from_version])
        parent: dict[str, str | None] = {from_version: None}
        edge_to: dict[str, tuple[str, str]] = {}

        while queue:
            current = queue.popleft()
            if current == target_version:
                break
            for src, dst in self._migrations:
                if src != current or dst in parent:
                    continue
                parent[dst] = current
                edge_to[dst] = (src, dst)
                queue.append(dst)

        if target_version not in parent:
            return []

        path: list[tuple[str, str]] = []
        node = target_version
        while node != from_version:
            edge = edge_to[node]
            path.append(edge)
            node = parent[node]  # type: ignore[assignment]
        path.reverse()
        return path

    @staticmethod
    def _attach_original_schema_version(
        original: RuntimeEvent,
        migrated: RuntimeEvent,
    ) -> RuntimeEvent:
        """Attach original schema version for auditability when migrated."""
        if original.schema_version == migrated.schema_version:
            return migrated

        payload = dict(migrated.payload_json or {})
        payload.setdefault("original_schema_version", original.schema_version)
        return replace(migrated, payload_json=payload)
