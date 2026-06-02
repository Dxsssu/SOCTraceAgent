from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv

from src.schema import Event, Execution, RoundReview


load_dotenv()


class SQLiteStorage:
    """SOCAgent 共享 SQLite 仓储。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = db_path or os.environ.get("SOCAGENT_DB_PATH", "data/socagent.db")
        self._db_path = Path(resolved)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self) -> None:
        with self._lock:
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        event_name TEXT NOT NULL,
                        message TEXT NOT NULL,
                        context_json TEXT NOT NULL DEFAULT '{}',
                        source TEXT NOT NULL DEFAULT '',
                        severity TEXT NOT NULL,
                        event_status TEXT NOT NULL,
                        current_round INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS executions (
                        execution_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        round_id INTEGER NOT NULL,
                        node_id TEXT NOT NULL,
                        node_title TEXT NOT NULL,
                        tool_name TEXT NOT NULL DEFAULT '',
                        tool_input_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        execution_status TEXT NOT NULL,
                        error_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS round_reviews (
                        review_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        round_id INTEGER NOT NULL,
                        findings_json TEXT NOT NULL DEFAULT '[]',
                        gaps_json TEXT NOT NULL DEFAULT '[]',
                        recommendations_json TEXT NOT NULL DEFAULT '[]',
                        created_by TEXT NOT NULL DEFAULT '_reviewer',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(event_id, round_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_status_round ON events (event_status, current_round)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_executions_event_round ON executions (event_id, round_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_round_reviews_event_round ON round_reviews (event_id, round_id)"
                )
                conn.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def save_event(self, event: Event) -> Event:
        payload = event.to_dict()
        with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO events (
                        event_id, event_name, message, context_json, source, severity,
                        event_status, current_round, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["event_id"],
                        payload["event_name"],
                        payload["message"],
                        json.dumps(payload["context"], ensure_ascii=False, sort_keys=True),
                        payload["source"],
                        payload["severity"],
                        payload["event_status"],
                        payload["current_round"],
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                conn.commit()
        return event

    def get_event(self, event_id: str) -> Event | None:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
        return self._row_to_event(row) if row else None

    def list_events_by_status(self, *statuses: str) -> list[Event]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        sql = f"""
        SELECT * FROM events
        WHERE event_status IN ({placeholders})
        ORDER BY updated_at ASC, created_at ASC
        """
        with self._lock:
            with self.connect() as conn:
                rows = conn.execute(sql, list(statuses)).fetchall()
        return [self._row_to_event(row) for row in rows]

    def save_execution(self, execution: Execution) -> Execution:
        payload = execution.to_dict()
        with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO executions (
                        execution_id, event_id, round_id, node_id, node_title,
                        tool_name, tool_input_json, result_json, execution_status,
                        error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["execution_id"],
                        payload["event_id"],
                        payload["round_id"],
                        payload["node_id"],
                        payload["node_title"],
                        payload["tool_name"],
                        json.dumps(payload["tool_input"], ensure_ascii=False, sort_keys=True),
                        json.dumps(payload["result"], ensure_ascii=False, sort_keys=True),
                        payload["execution_status"],
                        payload["error_message"],
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                conn.commit()
        return execution

    def list_executions(self, event_id: str, round_id: int | None = None) -> list[Execution]:
        sql = "SELECT * FROM executions WHERE event_id = ?"
        params: list[object] = [event_id]
        if round_id is not None:
            sql += " AND round_id = ?"
            params.append(round_id)
        sql += " ORDER BY created_at ASC, updated_at ASC"
        with self._lock:
            with self.connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        return [self._row_to_execution(row) for row in rows]

    def get_execution_by_node(self, event_id: str, round_id: int, node_id: str) -> Execution | None:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM executions
                    WHERE event_id = ? AND round_id = ? AND node_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (event_id, round_id, node_id),
                ).fetchone()
        return self._row_to_execution(row) if row else None

    def save_round_review(self, review: RoundReview) -> RoundReview:
        payload = review.to_dict()
        with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO round_reviews (
                        review_id, event_id, round_id, findings_json, gaps_json,
                        recommendations_json, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["review_id"],
                        payload["event_id"],
                        payload["round_id"],
                        json.dumps(payload["findings"], ensure_ascii=False),
                        json.dumps(payload["gaps"], ensure_ascii=False),
                        json.dumps(payload["recommendations"], ensure_ascii=False),
                        payload["created_by"],
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                conn.commit()
        return review

    def get_round_review(self, event_id: str, round_id: int) -> RoundReview | None:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM round_reviews
                    WHERE event_id = ? AND round_id = ?
                    LIMIT 1
                    """,
                    (event_id, round_id),
                ).fetchone()
        return self._row_to_round_review(row) if row else None

    def get_latest_round_review(self, event_id: str) -> RoundReview | None:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM round_reviews
                    WHERE event_id = ?
                    ORDER BY round_id DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
        return self._row_to_round_review(row) if row else None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event.from_dict(
            {
                "event_id": row["event_id"],
                "event_name": row["event_name"],
                "message": row["message"],
                "context": json.loads(row["context_json"] or "{}"),
                "source": row["source"],
                "severity": row["severity"],
                "event_status": row["event_status"],
                "current_round": row["current_round"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> Execution:
        return Execution.from_dict(
            {
                "execution_id": row["execution_id"],
                "event_id": row["event_id"],
                "round_id": row["round_id"],
                "node_id": row["node_id"],
                "node_title": row["node_title"],
                "tool_name": row["tool_name"],
                "tool_input": json.loads(row["tool_input_json"] or "{}"),
                "result": json.loads(row["result_json"] or "{}"),
                "execution_status": row["execution_status"],
                "error_message": row["error_message"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    @staticmethod
    def _row_to_round_review(row: sqlite3.Row) -> RoundReview:
        return RoundReview.from_dict(
            {
                "review_id": row["review_id"],
                "event_id": row["event_id"],
                "round_id": row["round_id"],
                "findings": json.loads(row["findings_json"] or "[]"),
                "gaps": json.loads(row["gaps_json"] or "[]"),
                "recommendations": json.loads(row["recommendations_json"] or "[]"),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )


__all__ = ["SQLiteStorage"]
