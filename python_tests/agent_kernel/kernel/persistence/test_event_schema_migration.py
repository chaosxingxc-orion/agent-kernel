"""Tests for EventSchemaMigrator."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_kernel.kernel.contracts import RuntimeEvent
from agent_kernel.kernel.persistence.event_schema_migration import (
    EventSchemaMigrator,
    SchemaMigrationError,
)


def _event(schema_version: str = "1") -> RuntimeEvent:
    return RuntimeEvent(
        run_id="run-1",
        event_id="evt-1",
        commit_offset=1,
        event_type="run.created",
        event_class="fact",
        event_authority="authoritative_fact",
        ordering_key="run-1",
        wake_policy="wake_actor",
        created_at="2026-04-05T00:00:00Z",
        payload_json={"k": "v"},
        schema_version=schema_version,
    )


def test_migrate_single_step_adds_original_schema_version_marker() -> None:
    migrator = EventSchemaMigrator()
    migrator.register(
        "1",
        "2",
        lambda event: replace(
            event,
            payload_json={**(event.payload_json or {}), "migrated": True},
        ),
    )
    migrated = migrator.migrate(_event("1"), "2")
    assert migrated.schema_version == "2"
    assert migrated.payload_json is not None
    assert migrated.payload_json["migrated"] is True
    assert migrated.payload_json["original_schema_version"] == "1"


def test_migrate_raises_when_no_path_exists() -> None:
    migrator = EventSchemaMigrator()
    with pytest.raises(SchemaMigrationError):
        migrator.migrate(_event("1"), "3")


def test_migrate_batch_supports_multi_hop_path() -> None:
    migrator = EventSchemaMigrator()
    migrator.register("1", "2", lambda event: replace(event))
    migrator.register("2", "3", lambda event: replace(event))
    migrated = migrator.migrate_batch([_event("1"), _event("3")], target_version="3")
    assert [event.schema_version for event in migrated] == ["3", "3"]


def test_migrate_chooses_shortest_available_path() -> None:
    migrator = EventSchemaMigrator()
    hop_calls: list[str] = []
    migrator.register(
        "1",
        "2",
        lambda event: (hop_calls.append("1->2"), replace(event))[1],
    )
    migrator.register(
        "2",
        "3",
        lambda event: (hop_calls.append("2->3"), replace(event))[1],
    )
    migrator.register(
        "1",
        "3",
        lambda event: (hop_calls.append("1->3"), replace(event))[1],
    )
    migrated = migrator.migrate(_event("1"), "3")
    assert migrated.schema_version == "3"
    assert hop_calls == ["1->3"]


def test_migrate_preserves_existing_original_schema_marker() -> None:
    migrator = EventSchemaMigrator()
    migrator.register(
        "1",
        "2",
        lambda event: replace(
            event,
            payload_json={**(event.payload_json or {}), "original_schema_version": "legacy"},
        ),
    )
    migrated = migrator.migrate(_event("1"), "2")
    assert migrated.payload_json is not None
    assert migrated.payload_json["original_schema_version"] == "legacy"
