from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from threading import RLock as Lock
from dataclasses import replace
from typing import Any

from dotenv import load_dotenv

from src.schema import TTTNode, TTTNodeStatus, TracebackTaskTree
from src.schema.event import utc_now
from src.schema.ttt_updates import next_task, walk


load_dotenv()


class TTTStore:
    """基于 SQLite 的 TTT 工作记忆存储。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = db_path or os.environ.get("SOCAGENT_DB_PATH", "runtime/socagent.db")
        self._db_path = Path(resolved)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ttt_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL,
                        round_id INTEGER NOT NULL,
                        ttt_version INTEGER NOT NULL,
                        schema_version TEXT NOT NULL,
                        updated_by TEXT NOT NULL,
                        tree_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(event_id, ttt_version)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ttt_event_round_version
                    ON ttt_snapshots (event_id, round_id, ttt_version)
                    """
                )
                conn.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def save_snapshot(self, tree: TracebackTaskTree, *, updated_by: str = "_planner", expected_version: int | None = None) -> TracebackTaskTree:
        with self._lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT COALESCE(MAX(ttt_version), 0) FROM ttt_snapshots WHERE event_id = ?", (tree.event_id,)).fetchone()
                if expected_version is not None and int(row[0]) != expected_version:
                    raise ValueError("Concurrent TTT update: version changed")
                next_version = int(row[0]) + 1
                if tree.version < next_version - 1:
                    raise ValueError("Cannot save stale TTT snapshot")
                snapshot = replace(tree, version=next_version, updated_at=utc_now())
                conn.execute(
                    """
                    INSERT INTO ttt_snapshots (
                        event_id, round_id, ttt_version, schema_version,
                        updated_by, tree_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.event_id,
                        snapshot.round_id,
                        next_version,
                        "2.0",
                        updated_by,
                        json.dumps(snapshot.to_dict(), ensure_ascii=False, sort_keys=True),
                        snapshot.created_at.isoformat(),
                        snapshot.updated_at.isoformat(),
                    ),
                )
                conn.commit()
        return snapshot

    def get_latest_ttt(self, event_id: str) -> TracebackTaskTree | None:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT tree_json, ttt_version FROM ttt_snapshots
                    WHERE event_id = ?
                    ORDER BY ttt_version DESC
                    LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
        if not row:
            return None
        payload = json.loads(row["tree_json"])
        payload["version"] = row["ttt_version"]
        tree = TracebackTaskTree.from_dict(payload)
        if "next_task_id" not in payload:  # Read old on-disk snapshots without rewriting them.
            first = next((n for n, d in walk(tree) if d == 3 and n.status == TTTNodeStatus.TODO), None)
            tree = replace(tree, next_task_id=first.node_id if first else None)
        return tree

    def list_leaf_nodes(self, tree: TracebackTaskTree) -> list[TTTNode]:
        return [node for node, depth in walk(tree) if depth == 3]

    def list_todo_leaf_nodes(self, event_id: str, round_id: int | None = None) -> list[TTTNode]:
        tree = self.get_latest_ttt(event_id)
        if tree is None:
            return []
        if round_id is not None and tree.round_id != round_id:
            return []
        leaves = self.list_leaf_nodes(tree)
        return [leaf for leaf in leaves if leaf.status == TTTNodeStatus.TODO]

    def has_open_work(self, event_id: str, round_id: int | None = None) -> bool:
        tree = self.get_latest_ttt(event_id)
        if tree is None:
            return False
        if round_id is not None and tree.round_id != round_id:
            return False
        return next_task(tree) is not None

    def all_leaves_terminal(self, event_id: str, round_id: int | None = None) -> bool:
        tree = self.get_latest_ttt(event_id)
        if tree is None:
            return False
        if round_id is not None and tree.round_id != round_id:
            return False
        leaves = self.list_leaf_nodes(tree)
        if not leaves:
            return False
        return all(leaf.status in {TTTNodeStatus.DONE, TTTNodeStatus.NOT_APPLICABLE} for leaf in leaves)

    def claim_next_todo_leaf(
        self,
        event_id: str,
        round_id: int,
        *,
        updated_by: str = "_executor",
    ) -> TTTNode | None:
        tree = self.get_latest_ttt(event_id)
        if tree is None or tree.round_id != round_id:
            return None
        target = next_task(tree)
        if target is None:
            return None
        self.update_node_status(
            event_id=event_id,
            node_id=target.node_id,
            new_status=TTTNodeStatus.IN_PROGRESS,
            updated_by=updated_by,
            round_id=round_id,
        )
        latest = self.get_latest_ttt(event_id)
        if latest is None:
            return target
        return self.find_node_by_id(latest, target.node_id)

    def update_node_status(
        self,
        *,
        event_id: str,
        node_id: str,
        new_status: TTTNodeStatus,
        updated_by: str,
        round_id: int | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> TracebackTaskTree | None:
        tree = self.get_latest_ttt(event_id)
        if tree is None:
            return None
        if round_id is not None and tree.round_id != round_id:
            return None

        mutable_tree = tree.to_dict()
        updated = self._update_node_in_dict(
            nodes=mutable_tree["root_nodes"],
            node_id=node_id,
            new_status=new_status.value,
            metadata_updates=metadata_updates or {},
        )
        if not updated:
            return None

        new_tree = TracebackTaskTree.from_dict(
            {
                **mutable_tree,
                "updated_at": utc_now().isoformat(),
            }
        )
        return self.save_snapshot(new_tree, updated_by=updated_by, expected_version=tree.version)

    def find_node_by_id(self, tree: TracebackTaskTree, node_id: str) -> TTTNode | None:
        def walk(node: TTTNode) -> TTTNode | None:
            if node.node_id == node_id:
                return node
            for child in node.children:
                found = walk(child)
                if found is not None:
                    return found
            return None

        for root in tree.root_nodes:
            found = walk(root)
            if found is not None:
                return found
        return None

    @staticmethod
    def _update_node_in_dict(
        *,
        nodes: list[dict[str, Any]],
        node_id: str,
        new_status: str,
        metadata_updates: dict[str, Any],
    ) -> bool:
        for node in nodes:
            if str(node.get("node_id")) == node_id:
                node["status"] = new_status
                metadata = dict(node.get("metadata") or {})
                metadata.update(metadata_updates)
                node["metadata"] = metadata
                if metadata_updates.get("last_execution_id"):
                    node["evidence_refs"] = list(dict.fromkeys([*node.get("evidence_refs", []), metadata_updates["last_execution_id"]]))
                    node["result_summary"] = metadata_updates.get("result_summary", metadata_updates.get("last_execution_status", ""))
                return True
            children = node.get("children") or []
            if TTTStore._update_node_in_dict(
                nodes=children,
                node_id=node_id,
                new_status=new_status,
                metadata_updates=metadata_updates,
            ):
                return True
        return False

    def _get_next_version(self, event_id: str) -> int:
        with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(MAX(ttt_version), 0) AS max_version FROM ttt_snapshots WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
        return int(row["max_version"]) + 1 if row else 1


__all__ = ["TTTStore"]
