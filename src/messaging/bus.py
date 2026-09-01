from __future__ import annotations

from abc import ABC, abstractmethod
import json
import os
import sqlite3
from threading import Lock
from pathlib import Path

from dotenv import load_dotenv

from .models import MessageEnvelope, MessageQuery


load_dotenv()


class MessageBus(ABC):
    """最小消息总线接口。

    注意：
    - 它不是业务状态机本身
    - 它只负责发布和查询结构化消息
    """

    @abstractmethod
    def publish(self, message: MessageEnvelope) -> MessageEnvelope:
        raise NotImplementedError

    @abstractmethod
    def list_messages(self, query: MessageQuery) -> list[MessageEnvelope]:
        raise NotImplementedError


class InMemoryMessageBus(MessageBus):
    """内存版消息总线。

    适合当前骨架阶段、本地调试和单进程测试。
    不适合作为跨进程最终实现。
    """

    def __init__(self) -> None:
        self._messages: list[MessageEnvelope] = []
        self._lock = Lock()

    def publish(self, message: MessageEnvelope) -> MessageEnvelope:
        with self._lock:
            self._messages.append(message)
        return message

    def list_messages(self, query: MessageQuery) -> list[MessageEnvelope]:
        with self._lock:
            messages = list(self._messages)

        filtered = [message for message in messages if self._matches(message, query)]
        if query.limit is not None and query.limit >= 0:
            return filtered[-query.limit :]
        return filtered

    @staticmethod
    def _matches(message: MessageEnvelope, query: MessageQuery) -> bool:
        if message.event_id != query.event_id:
            return False
        if query.round_id is not None and message.round_id != query.round_id:
            return False
        if query.from_role is not None and message.from_role != query.from_role:
            return False
        if query.to_role is not None and message.to_role != query.to_role:
            return False
        if query.message_type is not None and message.message_type != query.message_type:
            return False
        return True


class SQLiteMessageBus(MessageBus):
    """基于 SQLite 的持久化消息总线。

    适合当前阶段的单机、多进程消息留痕需求。
    它的职责仅是保存和查询结构化消息，不负责业务状态推进。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = db_path or os.environ.get("SOCAGENT_DB_PATH", "runtime/socagent.db")
        self._db_path = Path(resolved)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def publish(self, message: MessageEnvelope) -> MessageEnvelope:
        payload_text = json.dumps(message.payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        message_id,
                        event_id,
                        round_id,
                        from_role,
                        to_role,
                        message_type,
                        payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.message_id,
                        message.event_id,
                        message.round_id,
                        message.from_role.value,
                        message.to_role.value if message.to_role else None,
                        message.message_type.value,
                        payload_text,
                        message.created_at.isoformat(),
                    ),
                )
                conn.commit()
        return message

    def list_messages(self, query: MessageQuery) -> list[MessageEnvelope]:
        sql = """
        SELECT
            message_id,
            event_id,
            round_id,
            from_role,
            to_role,
            message_type,
            payload_json,
            created_at
        FROM messages
        WHERE event_id = ?
        """
        params: list[object] = [query.event_id]

        if query.round_id is not None:
            sql += " AND round_id = ?"
            params.append(query.round_id)
        if query.from_role is not None:
            sql += " AND from_role = ?"
            params.append(query.from_role.value)
        if query.to_role is not None:
            sql += " AND to_role = ?"
            params.append(query.to_role.value)
        if query.message_type is not None:
            sql += " AND message_type = ?"
            params.append(query.message_type.value)

        sql += " ORDER BY created_at ASC, rowid ASC"

        if query.limit is not None and query.limit >= 0:
            sql += " LIMIT ?"
            params.append(query.limit)

        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()

        return [self._row_to_message(row) for row in rows]

    def _initialize(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        message_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        round_id INTEGER,
                        from_role TEXT NOT NULL,
                        to_role TEXT,
                        message_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_event_round_created
                    ON messages (event_id, round_id, created_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_event_type_created
                    ON messages (event_id, message_type, created_at)
                    """
                )
                conn.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageEnvelope:
        payload_raw = row["payload_json"]
        payload = json.loads(payload_raw) if payload_raw else {}
        return MessageEnvelope.from_dict(
            {
                "message_id": row["message_id"],
                "event_id": row["event_id"],
                "round_id": row["round_id"],
                "from_role": row["from_role"],
                "to_role": row["to_role"],
                "message_type": row["message_type"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )


__all__ = ["MessageBus", "InMemoryMessageBus", "SQLiteMessageBus"]
