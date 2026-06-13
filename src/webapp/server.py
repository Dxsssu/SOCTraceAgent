from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, SeverityLevel, TTTNode, TracebackTaskTree
from src.storage import SQLiteStorage


load_dotenv()


DRIVING_MODE = {"mode": "auto"}


def create_app() -> tuple[Flask, SocketIO]:
    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )
    app.config["SECRET_KEY"] = os.environ.get("SOCAGENT_SECRET_KEY", "socagent-dev-key")

    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode="threading",
        ping_timeout=60,
        ping_interval=25,
    )

    watcher = SQLiteRealtimeWatcher(socketio=socketio)
    watcher.start()
    atexit.register(watcher.stop)

    register_routes(app)
    register_socket_events(socketio)
    return app, socketio


def register_routes(app: Flask) -> None:
    @app.route("/")
    def index() -> str:
        return render_template("index.html")

    @app.route("/warroom/<event_id>")
    def warroom(event_id: str) -> str:
        return render_template("warroom.html", event_id=event_id)

    @app.route("/health")
    def health() -> Any:
        return jsonify({"status": "success", "message": "SOCAgent web is healthy"})

    @app.route("/api/version")
    def version() -> Any:
        return jsonify(
            {
                "status": "success",
                "data": {
                    "version": "0.1.0",
                    "name": "SOCAgent",
                },
            }
        )

    @app.route("/api/state/driving-mode", methods=["GET", "PUT"])
    def driving_mode() -> Any:
        if request.method == "GET":
            return jsonify({"status": "success", "data": DRIVING_MODE})

        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"auto", "manual"}:
            return jsonify({"status": "error", "message": "Invalid mode"}), 400
        DRIVING_MODE["mode"] = mode
        return jsonify({"status": "success", "data": DRIVING_MODE})

    @app.route("/api/event/create", methods=["POST"])
    def create_event() -> Any:
        payload = request.get_json(silent=True) or {}
        message = str(payload.get("message") or "").strip()
        if not message:
            return jsonify({"status": "error", "message": "Event message cannot be empty"}), 400

        initialize_local_state()
        storage = SQLiteStorage()
        bus = SQLiteMessageBus()
        event_id = str(payload.get("event_id") or f"event_{uuid.uuid4().hex[:8]}")
        severity_raw = str(payload.get("severity") or SeverityLevel.MEDIUM.value).strip().lower()
        severity = SeverityLevel(severity_raw if severity_raw in {item.value for item in SeverityLevel} else SeverityLevel.UNKNOWN.value)

        event = Event(
            event_id=event_id,
            event_name=str(payload.get("event_name") or "SOCAgent Web Event").strip(),
            message=message,
            context=_parse_context(payload.get("context")),
            source=str(payload.get("source") or "web_manual").strip(),
            severity=severity,
        )
        storage.save_event(event)
        bus.publish(
            MessageEnvelope(
                event_id=event.event_id,
                round_id=event.current_round,
                from_role=RoleName.SYSTEM,
                message_type=MessageType.SYSTEM_INFO,
                payload={"text": f"System created security event: {event.event_name or event.event_id}"},
            )
        )
        return jsonify({"status": "success", "message": "Event created successfully", "data": event.to_dict()})

    @app.route("/api/event/list")
    def list_events() -> Any:
        initialize_local_state()
        storage = SQLiteStorage()
        events = storage.list_events_by_status(
            "pending",
            "planned",
            "executing",
            "reviewing",
            "replanning",
            "completed",
            "failed",
        )
        events_sorted = sorted(events, key=lambda item: item.created_at, reverse=True)
        return jsonify({"status": "success", "data": [event.to_dict() for event in events_sorted]})

    @app.route("/api/event/<event_id>")
    def get_event(event_id: str) -> Any:
        initialize_local_state()
        storage = SQLiteStorage()
        event = storage.get_event(event_id)
        if event is None:
            return jsonify({"status": "error", "message": "Event not found"}), 404
        return jsonify({"status": "success", "data": event.to_dict()})

    @app.route("/api/event/<event_id>", methods=["DELETE"])
    def delete_event(event_id: str) -> Any:
        initialize_local_state()
        storage = SQLiteStorage()
        deleted = storage.delete_event(event_id)
        if not deleted:
            return jsonify({"status": "error", "message": "Event not found"}), 404
        return jsonify({"status": "success", "message": "Event deleted successfully", "data": {"event_id": event_id}})

    @app.route("/api/event/<event_id>/messages")
    def get_event_messages(event_id: str) -> Any:
        after_rowid = request.args.get("after_rowid", default=0, type=int)
        role = request.args.get("role", default="", type=str).strip()
        messages = query_event_messages(event_id, after_rowid=after_rowid, role=role or None)
        return jsonify({"status": "success", "data": messages})

    @app.route("/api/event/<event_id>/stats")
    def get_event_stats(event_id: str) -> Any:
        storage = SQLiteStorage()
        ttt_store = TTTStore()
        event = storage.get_event(event_id)
        if event is None:
            return jsonify({"status": "error", "message": "Event not found"}), 404
        latest_ttt = ttt_store.get_latest_ttt(event_id)
        leaf_count = len(ttt_store.list_leaf_nodes(latest_ttt)) if latest_ttt is not None else 0
        executions = storage.list_executions(event_id)
        review = query_round_reviews(event_id)
        message_count = count_messages(event_id)
        return jsonify(
            {
                "status": "success",
                "data": {
                    "task_count": leaf_count,
                    "action_count": len(executions),
                    "command_count": len(review),
                    "message_count": message_count,
                },
            }
        )

    @app.route("/api/event/<event_id>/executions")
    def get_event_executions(event_id: str) -> Any:
        status = request.args.get("status", default="", type=str).strip().lower()
        storage = SQLiteStorage()
        event = storage.get_event(event_id)
        if event is None:
            return jsonify({"status": "error", "message": "Event not found"}), 404
        executions = storage.list_executions(event_id)
        if status:
            executions = [item for item in executions if item.execution_status.value == status]
        return jsonify({"status": "success", "data": [item.to_dict() for item in executions]})

    @app.route("/api/event/<event_id>/summaries")
    def get_event_summaries(event_id: str) -> Any:
        reviews = query_round_reviews(event_id)
        return jsonify({"status": "success", "data": reviews})

    @app.route("/api/event/<event_id>/hierarchy")
    def get_event_hierarchy(event_id: str) -> Any:
        rounds = query_round_hierarchy(event_id)
        return jsonify({"status": "success", "data": rounds})

    @app.route("/api/event/send_message/<event_id>", methods=["POST"])
    def send_message(event_id: str) -> Any:
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("message") or "").strip()
        if not text:
            return jsonify({"status": "error", "message": "Message content cannot be empty"}), 400
        storage = SQLiteStorage()
        bus = SQLiteMessageBus()
        event = storage.get_event(event_id)
        if event is None:
            return jsonify({"status": "error", "message": "Event not found"}), 404
        envelope = MessageEnvelope(
            event_id=event_id,
            round_id=event.current_round,
            from_role=RoleName.USER,
            message_type=MessageType.USER_MESSAGE,
            payload={"text": text},
        )
        bus.publish(envelope)
        return jsonify({"status": "success", "message": "Message sent successfully", "data": envelope.to_dict()})


def register_socket_events(socketio: SocketIO) -> None:
    @socketio.on("connect")
    def handle_connect() -> None:
        emit("status", {"status": "connected"})

    @socketio.on("join")
    def handle_join(data: dict[str, Any]) -> None:
        event_id = str((data or {}).get("event_id") or "").strip()
        if not event_id:
            emit("error", {"message": "Missing event_id"})
            return
        join_room(event_id)
        emit("status", {"status": "joined", "event_id": event_id})
        event = SQLiteStorage().get_event(event_id)
        if event is not None:
            emit(
                "status",
                {
                    "event_status": event.event_status.value,
                    "event_round": event.current_round,
                },
            )

    @socketio.on("leave")
    def handle_leave(data: dict[str, Any]) -> None:
        event_id = str((data or {}).get("event_id") or "").strip()
        if event_id:
            leave_room(event_id)
            emit("status", {"status": "left", "event_id": event_id})


class SQLiteRealtimeWatcher:
    def __init__(self, *, socketio: SocketIO, interval: float = 1.0) -> None:
        self.socketio = socketio
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_message_rowid = 0
        self._event_state: dict[str, tuple[str, int, str]] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="socagent-web-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                for message in query_new_messages(self._last_message_rowid):
                    self._last_message_rowid = max(self._last_message_rowid, int(message["rowid"]))
                    self.socketio.emit("new_message", message, room=message["event_id"])
                for event_id, status_payload in query_event_status_changes(self._event_state).items():
                    self.socketio.emit("status", status_payload, room=event_id)
            except Exception:
                pass
            self._stop_event.wait(self.interval)


def query_new_messages(last_rowid: int) -> list[dict[str, Any]]:
    db_path = resolve_db_path()
    if not db_path.exists():
        return []
    sql = """
    SELECT rowid, message_id, event_id, round_id, from_role, to_role, message_type, payload_json, created_at
    FROM messages
    WHERE rowid > ?
    ORDER BY rowid ASC
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (last_rowid,)).fetchall()
    messages: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        messages.append(
            {
                "rowid": int(row["rowid"]),
                "message_id": row["message_id"],
                "event_id": row["event_id"],
                "round_id": row["round_id"],
                "message_from": row["from_role"],
                "to_role": row["to_role"],
                "message_type": row["message_type"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return messages


def query_event_status_changes(state: dict[str, tuple[str, int, str]]) -> dict[str, dict[str, Any]]:
    db_path = resolve_db_path()
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT event_id, event_status, current_round, updated_at FROM events"
        ).fetchall()
    changed: dict[str, dict[str, Any]] = {}
    for row in rows:
        signature = (str(row["event_status"]), int(row["current_round"]), str(row["updated_at"]))
        event_id = str(row["event_id"])
        if state.get(event_id) == signature:
            continue
        state[event_id] = signature
        changed[event_id] = {
            "event_id": event_id,
            "event_status": signature[0],
            "event_round": signature[1],
        }
    return changed


def query_event_messages(event_id: str, *, after_rowid: int = 0, role: str | None = None) -> list[dict[str, Any]]:
    db_path = resolve_db_path()
    if not db_path.exists():
        return []
    sql = """
    SELECT rowid, message_id, event_id, round_id, from_role, to_role, message_type, payload_json, created_at
    FROM messages
    WHERE event_id = ? AND rowid > ?
    """
    params: list[Any] = [event_id, after_rowid]
    if role:
        sql += " AND from_role = ?"
        params.append(role)
    sql += " ORDER BY rowid ASC"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        result.append(
            {
                "id": int(row["rowid"]),
                "message_id": row["message_id"],
                "event_id": row["event_id"],
                "round_id": row["round_id"],
                "message_from": row["from_role"],
                "to_role": row["to_role"],
                "message_type": row["message_type"],
                "message_content": payload,
                "created_at": row["created_at"],
            }
        )
    return result


def query_round_reviews(event_id: str) -> list[dict[str, Any]]:
    db_path = resolve_db_path()
    if not db_path.exists():
        return []
    sql = """
    SELECT review_id, event_id, round_id, summary_text, findings_json, gaps_json, recommendations_json, created_by, created_at, updated_at
    FROM round_reviews
    WHERE event_id = ?
    ORDER BY round_id ASC
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (event_id,)).fetchall()
    reviews: list[dict[str, Any]] = []
    for row in rows:
        reviews.append(
            {
                "review_id": row["review_id"],
                "event_id": row["event_id"],
                "round_id": int(row["round_id"]),
                "summary_text": row["summary_text"] if "summary_text" in row.keys() else "",
                "findings": json.loads(row["findings_json"] or "[]"),
                "gaps": json.loads(row["gaps_json"] or "[]"),
                "recommendations": json.loads(row["recommendations_json"] or "[]"),
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return reviews


def query_round_hierarchy(event_id: str) -> list[dict[str, Any]]:
    db_path = resolve_db_path()
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ttt_rows = conn.execute(
            """
            SELECT event_id, round_id, tree_json, updated_at
            FROM ttt_snapshots
            WHERE event_id = ?
            ORDER BY round_id ASC, ttt_version DESC
            """,
            (event_id,),
        ).fetchall()
        execution_rows = conn.execute(
            """
            SELECT execution_id, event_id, round_id, node_id, node_title, tool_name, result_json, execution_status, error_message, created_at, updated_at
            FROM executions
            WHERE event_id = ?
            ORDER BY round_id ASC, created_at ASC
            """,
            (event_id,),
        ).fetchall()
    latest_ttt_by_round: dict[int, dict[str, Any]] = {}
    for row in ttt_rows:
        round_id = int(row["round_id"])
        if round_id not in latest_ttt_by_round:
            latest_ttt_by_round[round_id] = {
                "round_id": round_id,
                "tree": json.loads(row["tree_json"]),
                "executions": [],
                "reviews": [],
            }
    reviews_by_round = defaultdict(list)
    for review in query_round_reviews(event_id):
        reviews_by_round[int(review["round_id"])].append(review)
    for row in execution_rows:
        round_id = int(row["round_id"])
        if round_id not in latest_ttt_by_round:
            latest_ttt_by_round[round_id] = {
                "round_id": round_id,
                "tree": None,
                "executions": [],
                "reviews": [],
            }
        latest_ttt_by_round[round_id]["executions"].append(
            {
                "execution_id": row["execution_id"],
                "node_id": row["node_id"],
                "node_title": row["node_title"],
                "tool_name": row["tool_name"],
                "execution_status": row["execution_status"],
                "error_message": row["error_message"],
                "result": json.loads(row["result_json"] or "{}"),
            }
        )
    for round_id, reviews in reviews_by_round.items():
        if round_id not in latest_ttt_by_round:
            latest_ttt_by_round[round_id] = {
                "round_id": round_id,
                "tree": None,
                "executions": [],
                "reviews": [],
            }
        latest_ttt_by_round[round_id]["reviews"] = reviews
    return [latest_ttt_by_round[round_id] for round_id in sorted(latest_ttt_by_round)]


def count_messages(event_id: str) -> int:
    db_path = resolve_db_path()
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM messages WHERE event_id = ?", (event_id,)).fetchone()
    return int(row[0]) if row else 0


def resolve_db_path() -> Path:
    return Path(os.environ.get("SOCAGENT_DB_PATH", "data/socagent.db"))


def initialize_local_state() -> None:
    SQLiteStorage()
    TTTStore()
    SQLiteMessageBus()


def _parse_context(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"note": text}


def run_web_server() -> None:
    initialize_local_state()
    app, socketio = create_app()
    host = os.environ.get("SOCAGENT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("SOCAGENT_WEB_PORT", "5008"))
    socketio.run(
        app,
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )


__all__ = ["create_app", "run_web_server"]
