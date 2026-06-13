from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import load_dotenv

from src.schema import TTTNode, TTTNodeStatus, TracebackTaskTree
from src.schema.event import utc_now


load_dotenv()


class TTTStore:
    """SQLite-backed working-memory store for TTT snapshots."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        resolved = db_path or os.environ.get("SOCAGENT_DB_PATH", "data/socagent.db")
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

    def save_snapshot(self, tree: TracebackTaskTree, *, updated_by: str = "_planner") -> TracebackTaskTree:
        next_version = self._get_next_version(tree.event_id)
        snapshot = TracebackTaskTree(
            event_id=tree.event_id,
            round_id=tree.round_id,
            root_nodes=tree.root_nodes,
            metadata=tree.metadata,
            created_at=tree.created_at,
            updated_at=utc_now(),
        )
        with self._lock:
            with self.connect() as conn:
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
                        "1.0",
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
                    SELECT tree_json FROM ttt_snapshots
                    WHERE event_id = ?
                    ORDER BY ttt_version DESC
                    LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
        if not row:
            return None
        return TracebackTaskTree.from_dict(json.loads(row["tree_json"]))

    def list_leaf_nodes(self, tree: TracebackTaskTree) -> list[TTTNode]:
        leaves: list[TTTNode] = []

        def walk(node: TTTNode) -> None:
            if not node.children:
                leaves.append(node)
                return
            for child in node.children:
                walk(child)

        for root in tree.root_nodes:
            walk(root)
        return leaves

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
        for leaf in self.list_leaf_nodes(tree):
            if leaf.status in {TTTNodeStatus.TODO, TTTNodeStatus.IN_PROGRESS}:
                return True
        return False

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
        todo_leaves = sorted(
            self.list_leaf_nodes(tree),
            key=lambda item: (item.node_id, item.title),
        )
        target = next((leaf for leaf in todo_leaves if leaf.status == TTTNodeStatus.TODO), None)
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
        return self.save_snapshot(new_tree, updated_by=updated_by)

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
