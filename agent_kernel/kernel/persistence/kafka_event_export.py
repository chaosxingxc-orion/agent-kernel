"""Kafka-backed EventExportPort implementation."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal


class KafkaEventExportPort:
    """Export ActionCommit records to Kafka with run_id key ordering."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        topic: str = "agent-kernel-events",
        acks: Literal["0", "1", "all"] = "1",
        compression: Literal["none", "gzip", "snappy", "lz4"] = "lz4",
    ) -> None:
        """Initialize Kafka exporter configuration."""
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._acks = acks
        self._compression = compression
        self._producer: Any | None = None

    async def export_commit(self, commit: Any) -> None:
        """Serialize and send one ActionCommit to Kafka."""
        producer = await self._ensure_producer()
        payload = json.dumps(asdict(commit), sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = [("schema_version", b"1"), ("event_authority", b"authoritative_fact")]
        await producer.send_and_wait(
            self._topic,
            value=payload,
            key=str(commit.run_id).encode("utf-8"),
            headers=headers,
        )

    async def close(self) -> None:
        """Stop underlying Kafka producer when started."""
        if self._producer is None:
            return
        await self._producer.stop()
        self._producer = None

    async def _ensure_producer(self) -> Any:
        """Lazily initializes and returns the Kafka producer."""
        if self._producer is not None:
            return self._producer
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Kafka export requires aiokafka. Install with: pip install aiokafka"
            ) from exc
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            acks=self._acks,
            compression_type=self._compression if self._compression != "none" else None,
        )
        await self._producer.start()
        return self._producer
