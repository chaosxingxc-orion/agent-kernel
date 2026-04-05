"""SQLite-backed TaskViewLog for durable Task View reference persistence."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from agent_kernel.kernel.contracts import RunPolicyVersions, TaskViewRecord


class SQLiteTaskViewLog:
    """Persists TaskViewRecord references for replay and postmortem analysis.

    Stores only references (evidence_refs, memory_refs, etc.), not content.
    Thread-safe: check_same_thread=False + threading.RLock + WAL mode.
    """

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        """Initialize the instance with configured dependencies."""
        self._database_path = str(database_path)
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def close(self) -> None:
        """Close the SQLite connection after checkpointing the WAL file."""
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()

    def write(self, record: TaskViewRecord) -> None:
        """Persist one TaskViewRecord.  Idempotent by task_view_id."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO task_view_log (
                      task_view_id, run_id, decision_ref, selected_model_role,
                      assembled_at, stage_id, branch_id, task_contract_ref,
                      evidence_refs, memory_refs, knowledge_refs,
                      policy_versions_json, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_view_id) DO NOTHING
                    """,
                    (
                        record.task_view_id,
                        record.run_id,
                        record.decision_ref,
                        record.selected_model_role,
                        record.assembled_at,
                        record.stage_id,
                        record.branch_id,
                        record.task_contract_ref,
                        json.dumps(record.evidence_refs),
                        json.dumps(record.memory_refs),
                        json.dumps(record.knowledge_refs),
                        json.dumps(_policy_versions_to_dict(record.policy_versions)),
                        record.schema_version,
                    ),
                )
                self._conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                raise

    def get_by_id(self, task_view_id: str) -> TaskViewRecord | None:
        """Return TaskViewRecord by task_view_id, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_view_log WHERE task_view_id = ?",
                (task_view_id,),
            ).fetchone()
            return _row_to_record(row) if row else None

    def get_by_decision(self, run_id: str, decision_ref: str) -> TaskViewRecord | None:
        """Return TaskViewRecord by run_id + decision_ref, or None."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM task_view_log
                WHERE run_id = ? AND decision_ref = ?
                ORDER BY assembled_at DESC LIMIT 1
                """,
                (run_id, decision_ref),
            ).fetchone()
            return _row_to_record(row) if row else None

    def list_for_run(self, run_id: str) -> list[TaskViewRecord]:
        """Return all TaskViewRecords for a run, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_view_log WHERE run_id = ? ORDER BY assembled_at",
                (run_id,),
            ).fetchall()
            return [_row_to_record(r) for r in rows if r]

    def bind_to_decision(self, task_view_id: str, decision_ref: str) -> None:
        """Update the decision_ref for an existing TaskViewRecord.

        This enables late-binding: a task view is recorded before the model
        call, then bound to the resulting decision_ref after the model responds.

        Args:
            task_view_id: Identifier of the record to update.
            decision_ref: Decision reference to bind (TurnIntentRecord.intent_commit_ref).

        """
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE task_view_log SET decision_ref = ? WHERE task_view_id = ?",
                    (decision_ref, task_view_id),
                )
                self._conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    self._conn.rollback()
                raise

    def _ensure_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS task_view_log (
              task_view_id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              decision_ref TEXT,
              selected_model_role TEXT NOT NULL,
              assembled_at TEXT NOT NULL,
              stage_id TEXT,
              branch_id TEXT,
              task_contract_ref TEXT,
              evidence_refs TEXT NOT NULL DEFAULT '[]',
              memory_refs TEXT NOT NULL DEFAULT '[]',
              knowledge_refs TEXT NOT NULL DEFAULT '[]',
              policy_versions_json TEXT,
              schema_version TEXT NOT NULL DEFAULT '1'
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tvl_run_decision ON task_view_log(run_id, decision_ref)"
        )
        self._conn.commit()


def _policy_versions_to_dict(pv: RunPolicyVersions | None) -> dict | None:
    if pv is None:
        return None
    return {
        "route_policy_version": pv.route_policy_version,
        "skill_policy_version": pv.skill_policy_version,
        "evaluation_policy_version": pv.evaluation_policy_version,
        "task_view_policy_version": pv.task_view_policy_version,
        "pinned_at": pv.pinned_at,
    }


def _row_to_record(row: sqlite3.Row) -> TaskViewRecord:
    pv_dict = json.loads(row["policy_versions_json"]) if row["policy_versions_json"] else None
    pv = RunPolicyVersions(**pv_dict) if pv_dict else None
    return TaskViewRecord(
        task_view_id=row["task_view_id"],
        run_id=row["run_id"],
        decision_ref=row["decision_ref"],
        selected_model_role=row["selected_model_role"],
        assembled_at=row["assembled_at"],
        stage_id=row["stage_id"],
        branch_id=row["branch_id"],
        task_contract_ref=row["task_contract_ref"],
        evidence_refs=json.loads(row["evidence_refs"]),
        memory_refs=json.loads(row["memory_refs"]),
        knowledge_refs=json.loads(row["knowledge_refs"]),
        policy_versions=pv,
        schema_version=row["schema_version"],
    )
