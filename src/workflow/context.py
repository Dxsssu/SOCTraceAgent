from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from neo4j import GraphDatabase

from src.memory.longterm_memory.episodic_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH as EXCYTIN_EPISODIC_PATH,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH as EXCYTIN_PROCEDURAL_PATH,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH as EXCYTIN_SEMANTIC_PATH,
)
from src.messaging import RoleName
from src.schema import Event, RoundReview, TracebackTaskTree

logger = logging.getLogger(__name__)


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


DEFAULT_ROLE_MEMORY_POLICY: Mapping[RoleName, frozenset[MemoryType]] = {
    RoleName.PLANNER: frozenset({MemoryType.SEMANTIC, MemoryType.PROCEDURAL}),
    RoleName.EXECUTOR: frozenset({MemoryType.SEMANTIC, MemoryType.EPISODIC}),
    RoleName.REVIEWER: frozenset({MemoryType.SEMANTIC}),
}


@dataclass(frozen=True, slots=True)
class MemoryAccessPolicy:
    permissions: Mapping[RoleName, frozenset[MemoryType]] = dataclass_field(
        default_factory=lambda: DEFAULT_ROLE_MEMORY_POLICY
    )

    def allowed(self, role: RoleName, memory_type: MemoryType) -> bool:
        return memory_type in self.permissions.get(role, frozenset())

    def require(self, role: RoleName, *memory_types: MemoryType) -> None:
        denied = [item.value for item in memory_types if not self.allowed(role, item)]
        if denied:
            raise PermissionError(
                f"Role {role.value} cannot access memory types: {', '.join(denied)}"
            )


class PlannerContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        review: RoundReview | None = None,
        ttt: TracebackTaskTree | None = None,
    ) -> dict[str, Any]: ...


class ExecutorContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = True,
        episodic_query: str | None = None,
    ) -> dict[str, Any]: ...


class ReviewerContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]: ...


class NullPlannerContextProvider:
    def build(
        self,
        event: Event,
        *,
        review: RoundReview | None = None,
        ttt: TracebackTaskTree | None = None,
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


class NullExecutorContextProvider:
    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = True,
        episodic_query: str | None = None,
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


class NullReviewerContextProvider:
    def build(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


_TOKEN_RE = re.compile(r"[a-z][a-z0-9_:-]{1,}|[\u4e00-\u9fff]{2,}", re.IGNORECASE)
_PROFILE_ALIASES = {
    "excytin": "excytin_bench",
    "excytin-bench": "excytin_bench",
    "excytin_bench": "excytin_bench",
}
_ENTITY_HINTS: Mapping[str, tuple[str, ...]] = {
    "ALERT_ID": ("alert", "incident", "告警", "事件"),
    "IP_ADDRESS": ("ip", "address", "network", "connection", "网络", "地址", "连接"),
    "URL": ("url", "link", "网址", "链接"),
    "DOMAIN": ("domain", "dns", "域名"),
    "PORT": ("port", "端口"),
    "PROTOCOL": ("protocol", "协议"),
    "EMAIL_ADDRESS": ("email", "mail", "sender", "recipient", "邮件", "发件", "收件"),
    "MESSAGE_ID": ("message", "邮件", "消息"),
    "DEVICE_ID": ("device", "host", "endpoint", "主机", "设备", "终端"),
    "DEVICE_NAME": ("hostname", "device name", "主机名", "设备名"),
    "PROCESS_ID": ("process", "pid", "进程"),
    "COMMAND_LINE": ("command", "powershell", "cmd", "命令"),
    "FILE_HASH": ("hash", "sha1", "sha256", "md5", "哈希"),
    "FILE_NAME": ("file", "filename", "文件"),
    "ACCOUNT_ID": ("account", "user", "identity", "sid", "账户", "用户", "身份"),
    "TIMESTAMP": ("time", "date", "window", "时间", "日期"),
}
_DOMAIN_HINTS: Mapping[str, tuple[str, ...]] = {
    "email-threat-investigation": (
        "email",
        "mail",
        "url",
        "message",
        "邮件",
        "链接",
        "发件",
        "收件",
    ),
    "endpoint-process-investigation": (
        "endpoint",
        "device",
        "host",
        "process",
        "command",
        "file",
        "hash",
        "终端",
        "设备",
        "主机",
        "进程",
        "命令",
        "文件",
    ),
    "identity-signin-investigation": (
        "identity",
        "account",
        "user",
        "signin",
        "login",
        "authentication",
        "身份",
        "账户",
        "用户",
        "登录",
        "认证",
    ),
    "network-activity-investigation": (
        "network",
        "ip",
        "domain",
        "dns",
        "port",
        "protocol",
        "connection",
        "网络",
        "域名",
        "端口",
        "协议",
        "连接",
    ),
}


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value or "")}


def _contains_hint(text: str, hint: str) -> bool:
    return hint.lower() in text.lower()


def _infer_semantic_types(text: str) -> set[str]:
    return {
        semantic_type
        for semantic_type, hints in _ENTITY_HINTS.items()
        if any(_contains_hint(text, hint) for hint in hints)
    }


def _event_text(
    event: Event,
    *,
    review: RoundReview | None = None,
    ttt: TracebackTaskTree | None = None,
) -> str:
    parts = [
        event.event_name,
        event.message,
        json.dumps(event.context, ensure_ascii=False),
    ]
    if review is not None:
        parts.append(json.dumps(review.to_dict(), ensure_ascii=False))
    if ttt is not None:
        parts.append(json.dumps(ttt.to_dict(), ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _normalize_profile(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return _PROFILE_ALIASES.get(normalized)


def resolve_memory_profile(event: Event) -> str | None:
    for key in ("memory_profile", "benchmark", "dataset", "source_dataset"):
        profile = _normalize_profile(event.context.get(key))
        if profile:
            return profile
    return _normalize_profile(os.environ.get("SOCAGENT_MEMORY_PROFILE"))


class _ExcytinBenchRepositoryBase:
    """Read-only retrieval view over the committed, sanitized LTM snapshots."""

    profile = "excytin_bench"

    def __init__(
        self,
        *,
        semantic_path: Path = EXCYTIN_SEMANTIC_PATH,
        procedural_path: Path = EXCYTIN_PROCEDURAL_PATH,
        episodic_path: Path = EXCYTIN_EPISODIC_PATH,
    ) -> None:
        self.semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
        self.procedural = json.loads(procedural_path.read_text(encoding="utf-8"))
        self.episodic = json.loads(episodic_path.read_text(encoding="utf-8"))
        self._initialize_indexes()

    def _initialize_indexes(self) -> None:
        self._tables_by_id = {
            table["table_id"]: table for table in self.semantic["tables"]
        }
        self._tables_by_name = {
            table["name"].lower(): table for table in self.semantic["tables"]
        }
        self._join_keys_by_id = {
            join_key["join_key_id"]: join_key for join_key in self.semantic["join_keys"]
        }

    def retrieve_procedures(self, text: str, *, limit: int = 3) -> list[dict[str, Any]]:
        semantic_types = _infer_semantic_types(text)
        query_tokens = _tokens(text)
        scored: list[tuple[float, dict[str, Any]]] = []
        base: dict[str, Any] | None = None
        for procedure in self.procedural["procedures"]:
            if (
                not procedure.get("retrieval_eligible", True)
                or procedure.get("status") != "active"
            ):
                continue
            if procedure.get("category") == "base_workflow":
                base = procedure
                continue
            score = 4.0 * len(
                semantic_types.intersection(procedure.get("entry_entity_types", []))
            )
            score += float(
                len(
                    query_tokens.intersection(
                        _tokens(procedure.get("retrieval_text", ""))
                    )
                )
            )
            score += 3.0 * sum(
                1
                for hint in _DOMAIN_HINTS.get(procedure.get("skill_id", ""), ())
                if _contains_hint(text, hint)
            )
            if score > 0:
                scored.append((score, procedure))
        scored.sort(key=lambda item: (-item[0], item[1]["skill_id"]))
        selected = ([base] if base is not None else []) + [
            item[1] for item in scored[: max(0, limit - 1)]
        ]
        return [self._planner_procedure_view(procedure) for procedure in selected]

    def retrieve_semantic(
        self,
        text: str,
        *,
        procedures: Sequence[Mapping[str, Any]] = (),
        table_hints: Sequence[str] = (),
        table_limit: int = 12,
        field_limit: int = 12,
    ) -> dict[str, Any]:
        semantic_types = _infer_semantic_types(text)
        query_tokens = _tokens(text)
        referenced_table_ids: set[str] = set()
        referenced_join_key_ids: set[str] = set()
        required_log_types: set[str] = set()
        for procedure in procedures:
            required_log_types.update(procedure.get("applicable_log_types", []))
            for phase in procedure.get("phases", []):
                referenced_table_ids.update(phase.get("semantic_table_ids", []))
                referenced_join_key_ids.update(phase.get("semantic_join_key_ids", []))
                semantic_types.update(phase.get("required_semantic_types", []))

        hinted_ids: set[str] = set()
        for hint in table_hints:
            table = self._tables_by_id.get(hint) or self._tables_by_name.get(
                str(hint).lower()
            )
            if table:
                hinted_ids.add(table["table_id"])

        scored_tables: list[tuple[float, dict[str, Any]]] = []
        for table in self.semantic["tables"]:
            score = 0.0
            if table["table_id"] in hinted_ids:
                score += 30.0
            if table["table_id"] in referenced_table_ids:
                score += 6.0
            if table.get("log_type") in required_log_types:
                score += 4.0
            score += float(
                len(query_tokens.intersection(_tokens(table.get("semantic_text", ""))))
            )
            field_types = {
                field.get("semantic_type") for field in table.get("fields", [])
            }
            score += 1.5 * len(semantic_types.intersection(field_types))
            score += 12.0 * sum(
                _contains_hint(text, hint)
                for hint in table.get("retrieval_hints", [])
            )
            if score > 0:
                scored_tables.append((score, table))
        scored_tables.sort(key=lambda item: (-item[0], item[1]["name"]))
        selected_tables = [item[1] for item in scored_tables[:table_limit]]

        selected_field_ids: set[str] = set()
        table_views: list[dict[str, Any]] = []
        for table in selected_tables:
            scored_fields: list[tuple[float, dict[str, Any]]] = []
            for field in table.get("fields", []):
                score = 0.0
                if field.get("semantic_type") in semantic_types:
                    score += 8.0
                if field.get("is_identifier"):
                    score += 3.0
                if field.get("is_time_field"):
                    score += 3.0
                if field.get("join_key"):
                    score += 2.0
                score += float(
                    len(
                        query_tokens.intersection(
                            _tokens(field.get("semantic_text", ""))
                        )
                    )
                )
                scored_fields.append((score, field))
            scored_fields.sort(key=lambda item: (-item[0], item[1]["name"]))
            fields = [item[1] for item in scored_fields[:field_limit]]
            selected_field_ids.update(field["field_id"] for field in fields)
            table_views.append(
                {
                    "table_id": table["table_id"],
                    "name": table["name"],
                    "log_type": table.get("log_type"),
                    "description": table.get("description_en")
                    or table.get("description_zh"),
                    "default_time_field": table.get("default_time_field"),
                    "fields": [
                        {
                            "field_id": field["field_id"],
                            "name": field["name"],
                            "data_type": field.get("data_type"),
                            "semantic_type": field.get("semantic_type"),
                            "join_key": field.get("join_key"),
                            "is_time_field": field.get("is_time_field", False),
                        }
                        for field in fields
                    ],
                }
            )

        join_key_views: list[dict[str, Any]] = []
        for join_key in self.semantic["join_keys"]:
            selected_fields = sorted(
                set(join_key.get("field_ids", [])).intersection(selected_field_ids)
            )
            if (
                join_key["join_key_id"] not in referenced_join_key_ids
                and len(selected_fields) < 2
            ):
                continue
            join_key_views.append(
                {
                    "join_key_id": join_key["join_key_id"],
                    "name": join_key.get("name"),
                    "semantic_type": join_key.get("semantic_type"),
                    "join_scope": join_key.get("join_scope"),
                    "confidence": join_key.get("confidence"),
                    "selected_field_ids": selected_fields[:12],
                }
            )
        join_key_views.sort(key=lambda item: item["join_key_id"])
        return {
            "catalog_id": self.semantic["catalog"]["catalog_id"],
            "schema_hash": self.semantic["catalog"]["schema_hash"],
            "tables": table_views,
            "join_keys": join_key_views[:12],
        }


class _ExcytinBenchRepositoryWithEpisodes(_ExcytinBenchRepositoryBase):
    """Load the same read model from the imported Neo4j knowledge graph."""

    def __init__(
        self,
        *,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
    ) -> None:
        driver = GraphDatabase.driver(uri, auth=(username, password))
        try:
            driver.verify_connectivity()
            with driver.session(database=database) as session:
                self.semantic = self._load_semantic_graph(session)
                self.procedural = self._load_procedural_graph(session)
                self.episodic = self._load_episodic_graph(session)
        finally:
            driver.close()
        self._initialize_indexes()

    @staticmethod
    def _load_semantic_graph(session: Any) -> dict[str, Any]:
        catalog_record = session.run(
            "MATCH (catalog:LogCatalog {catalog_id: 'excytin-schema-v1'}) "
            "RETURN properties(catalog) AS catalog"
        ).single()
        if catalog_record is None:
            raise ValueError("Neo4j is missing Semantic catalog excytin-schema-v1")
        table_records = session.run(
            """
            MATCH (:LogCatalog {catalog_id: 'excytin-schema-v1'})
                  -[:CONTAINS_TABLE]->(table:LogTable)
            OPTIONAL MATCH (table)-[:HAS_FIELD]->(field:Field)
            RETURN properties(table) AS table,
                   [item IN collect(field) WHERE item IS NOT NULL | properties(item)] AS fields
            ORDER BY table.name
            """
        )
        tables = []
        for record in table_records:
            table = dict(record["table"])
            table["fields"] = [dict(field) for field in record["fields"]]
            tables.append(table)
        join_records = session.run(
            """
            MATCH (:LogCatalog {catalog_id: 'excytin-schema-v1'})
                  -[:DEFINES_JOIN_KEY]->(join_key:JoinKey)
            OPTIONAL MATCH (field:Field)-[:MAPS_TO_JOIN_KEY]->(join_key)
            RETURN properties(join_key) AS join_key,
                   [item IN collect(field.field_id) WHERE item IS NOT NULL] AS field_ids
            ORDER BY join_key.name
            """
        )
        join_keys = []
        for record in join_records:
            join_key = dict(record["join_key"])
            join_key["field_ids"] = list(record["field_ids"])
            join_keys.append(join_key)
        return {
            "catalog": dict(catalog_record["catalog"]),
            "tables": tables,
            "join_keys": join_keys,
        }

    @staticmethod
    def _load_procedural_graph(session: Any) -> dict[str, Any]:
        procedure_records = session.run(
            """
            MATCH (:ProcedureCatalog {procedural_catalog_id: 'excytin-procedures-v1'})
                  -[:CONTAINS_PROCEDURE]->(procedure:InvestigationProcedure)
            RETURN properties(procedure) AS procedure
            ORDER BY procedure.skill_id
            """
        )
        procedures = [dict(record["procedure"]) for record in procedure_records]
        if not procedures:
            raise ValueError(
                "Neo4j is missing Procedural catalog excytin-procedures-v1"
            )
        by_id = {procedure["procedure_id"]: procedure for procedure in procedures}
        for procedure in procedures:
            procedure["phases"] = []
        phase_records = session.run(
            """
            MATCH (procedure:InvestigationProcedure)-[:HAS_PHASE]->(phase:ProcedurePhase)
            WHERE procedure.procedural_catalog_id = 'excytin-procedures-v1'
            OPTIONAL MATCH (phase)-[:REQUIRES_TABLE]->(table:LogTable)
            WITH procedure, phase, collect(DISTINCT table.table_id) AS table_ids
            OPTIONAL MATCH (phase)-[:REQUIRES_JOIN_KEY]->(join_key:JoinKey)
            RETURN procedure.procedure_id AS procedure_id,
                   properties(phase) AS phase,
                   [item IN table_ids WHERE item IS NOT NULL] AS table_ids,
                   [item IN collect(DISTINCT join_key.join_key_id) WHERE item IS NOT NULL] AS join_key_ids
            ORDER BY procedure.procedure_id, phase.sequence_no
            """
        )
        for record in phase_records:
            phase = dict(record["phase"])
            phase["semantic_table_ids"] = list(record["table_ids"])
            phase["semantic_join_key_ids"] = list(record["join_key_ids"])
            by_id[record["procedure_id"]]["phases"].append(phase)
        return {"procedures": procedures}

    @staticmethod
    def _load_episodic_graph(session: Any) -> dict[str, Any]:
        episode_records = session.run(
            """
            MATCH (:EpisodeCatalog {episodic_catalog_id: 'excytin-episodes-v1'})
                  -[:CONTAINS_EPISODE]->(episode:InvestigationEpisode)
            RETURN properties(episode) AS episode
            ORDER BY episode.episode_id
            """
        )
        episodes = [dict(record["episode"]) for record in episode_records]
        if not episodes:
            raise ValueError("Neo4j is missing Episodic catalog excytin-episodes-v1")
        by_id = {episode["episode_id"]: episode for episode in episodes}
        for episode in episodes:
            episode["attempts"] = []
            episode["transitions"] = []
        attempt_records = session.run(
            """
            MATCH (episode:InvestigationEpisode)-[:HAS_ATTEMPT]->(attempt:QueryAttempt)
            WHERE episode.episodic_catalog_id = 'excytin-episodes-v1'
            OPTIONAL MATCH (attempt)-[target:TARGETS_TABLE]->(table:LogTable)
            RETURN episode.episode_id AS episode_id,
                   properties(attempt) AS attempt,
                   collect(DISTINCT {
                       table_id: table.table_id,
                       roles: target.roles
                   }) AS table_refs
            ORDER BY episode.episode_id, attempt.sequence_no
            """
        )
        for record in attempt_records:
            attempt = dict(record["attempt"])
            attempt["table_refs"] = [
                dict(reference)
                for reference in record["table_refs"]
                if reference.get("table_id") is not None
            ]
            by_id[record["episode_id"]]["attempts"].append(attempt)
        transition_records = session.run(
            """
            MATCH (source:QueryAttempt)-[:REPAIRED_BY]->(target:QueryAttempt)
            WHERE source.episodic_catalog_id = 'excytin-episodes-v1'
            RETURN source.episode_id AS episode_id,
                   source.attempt_id AS from_attempt_id,
                   target.attempt_id AS to_attempt_id
            """
        )
        for record in transition_records:
            by_id[record["episode_id"]]["transitions"].append(
                {
                    "from_attempt_id": record["from_attempt_id"],
                    "to_attempt_id": record["to_attempt_id"],
                    "relation_type": "REPAIRED_BY",
                }
            )
        return {"episodes": episodes}

    def retrieve_episodes(
        self,
        text: str,
        *,
        table_hints: Sequence[str] = (),
        episode_limit: int = 3,
        attempt_limit: int = 5,
    ) -> list[dict[str, Any]]:
        query_tokens = _tokens(text)
        hinted_names = {
            self._tables_by_id[hint]["name"]
            if hint in self._tables_by_id
            else str(hint)
            for hint in table_hints
        }
        scored: list[tuple[float, dict[str, Any]]] = []
        for episode in self.episodic["episodes"]:
            if episode.get("source_split") != "train" or not episode.get(
                "retrieval_eligible"
            ):
                continue
            episode_tables = {
                reference["table_id"].split(":", 1)[-1]
                for attempt in episode.get("attempts", [])
                for reference in attempt.get("table_refs", [])
            }
            score = float(
                len(
                    query_tokens.intersection(
                        _tokens(episode.get("retrieval_text", ""))
                    )
                )
            )
            score += 8.0 * len(hinted_names.intersection(episode_tables))
            if episode.get("outcome") == "success":
                score += 0.5
            if score > 0:
                scored.append((score, episode))
        scored.sort(key=lambda item: (-item[0], item[1]["episode_id"]))
        return [
            self._executor_episode_view(
                episode, query_tokens, hinted_names, attempt_limit
            )
            for _, episode in scored[:episode_limit]
        ]

    @staticmethod
    def _planner_procedure_view(procedure: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "procedure_id": procedure["procedure_id"],
            "skill_id": procedure["skill_id"],
            "name": procedure["name"],
            "category": procedure.get("category"),
            "goal": procedure.get("goal"),
            "description": procedure.get("description"),
            "entry_entity_types": list(procedure.get("entry_entity_types", [])),
            "applicable_log_types": list(procedure.get("applicable_log_types", [])),
            "phases": [
                {
                    "phase_id": phase["phase_id"],
                    "sequence_no": phase["sequence_no"],
                    "name": phase["name"],
                    "goal": phase.get("goal"),
                    "required_log_types": list(phase.get("required_log_types", [])),
                    "required_semantic_types": list(
                        phase.get("required_semantic_types", [])
                    ),
                    "semantic_table_ids": list(phase.get("semantic_table_ids", [])),
                    "semantic_join_key_ids": list(
                        phase.get("semantic_join_key_ids", [])
                    ),
                    "success_condition": phase.get("success_condition"),
                }
                for phase in procedure.get("phases", [])
            ],
        }

    @staticmethod
    def _executor_episode_view(
        episode: Mapping[str, Any],
        query_tokens: set[str],
        hinted_names: set[str],
        attempt_limit: int,
    ) -> dict[str, Any]:
        attempts = list(episode.get("attempts", []))

        def attempt_score(attempt: Mapping[str, Any]) -> tuple[float, int]:
            attempt_tables = {
                reference["table_id"].split(":", 1)[-1]
                for reference in attempt.get("table_refs", [])
            }
            score = float(
                len(
                    query_tokens.intersection(
                        _tokens(attempt.get("retrieval_text", ""))
                    )
                )
            )
            score += 8.0 * len(hinted_names.intersection(attempt_tables))
            if attempt.get("result_status") in {"rows_returned", "error"}:
                score += 1.0
            return score, -int(attempt.get("sequence_no", 0))

        ranked = sorted(attempts, key=attempt_score, reverse=True)[:attempt_limit]
        ranked.sort(key=lambda item: item.get("sequence_no", 0))
        return {
            "episode_id": episode["episode_id"],
            "source_split": episode.get("source_split"),
            "task_template": episode.get("task_template"),
            "outcome": episode.get("outcome"),
            "attempts": [
                {
                    key: attempt.get(key)
                    for key in (
                        "attempt_id",
                        "sequence_no",
                        "query_kind",
                        "sql_template",
                        "parameter_types",
                        "execution_status",
                        "result_status",
                        "unresolved_field_names",
                        "error_detail",
                    )
                    if attempt.get(key) is not None
                }
                for attempt in ranked
            ],
            "transitions": [
                transition
                for transition in episode.get("transitions", [])
                if transition.get("relation_type") == "REPAIRED_BY"
            ],
        }


class ExcytinBenchNeo4jRepository(_ExcytinBenchRepositoryWithEpisodes):
    """Public Neo4j-backed ExCyTIn repository."""


class ExcytinBenchSnapshotRepository(_ExcytinBenchRepositoryWithEpisodes):
    """Load the ExCyTIn read model from committed JSON snapshots."""

    def __init__(
        self,
        *,
        semantic_path: Path = EXCYTIN_SEMANTIC_PATH,
        procedural_path: Path = EXCYTIN_PROCEDURAL_PATH,
        episodic_path: Path = EXCYTIN_EPISODIC_PATH,
    ) -> None:
        self.semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
        self.procedural = json.loads(procedural_path.read_text(encoding="utf-8"))
        self.episodic = json.loads(episodic_path.read_text(encoding="utf-8"))
        self._initialize_indexes()


def build_default_repositories() -> dict[str, _ExcytinBenchRepositoryBase]:
    backend = os.environ.get("SOCAGENT_LTM_BACKEND", "auto").strip().lower()
    if backend not in {"auto", "neo4j", "snapshot"}:
        raise ValueError("SOCAGENT_LTM_BACKEND must be auto, neo4j, or snapshot")

    password = os.environ.get("NEO4J_PASSWORD", "").strip()
    use_neo4j = backend == "neo4j" or (backend == "auto" and bool(password))
    if use_neo4j:
        if not password:
            raise ValueError(
                "NEO4J_PASSWORD is required when SOCAGENT_LTM_BACKEND=neo4j"
            )
        try:
            repository = ExcytinBenchNeo4jRepository(
                uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                username=os.environ.get("NEO4J_USERNAME", "neo4j"),
                password=password,
                database=os.environ.get("NEO4J_DATABASE", "neo4j"),
            )
            return {repository.profile: repository}
        except Exception:
            if backend == "neo4j":
                raise
            logger.warning(
                "Neo4j LTM auto-detection failed; using committed snapshots",
                exc_info=True,
            )

    repository = ExcytinBenchSnapshotRepository()
    return {repository.profile: repository}


class PlannerMemoryView:
    def __init__(self, service: MemoryContextService) -> None:
        self.__service = service

    def build(
        self,
        event: Event,
        *,
        review: RoundReview | None = None,
        ttt: TracebackTaskTree | None = None,
    ) -> dict[str, Any]:
        return self.__service._build_planner(event, review=review, ttt=ttt)


class ExecutorMemoryView:
    def __init__(self, service: MemoryContextService) -> None:
        self.__service = service

    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = True,
        episodic_query: str | None = None,
    ) -> dict[str, Any]:
        return self.__service._build_executor(
            event,
            node=node,
            executions=executions,
            include_episodic=include_episodic,
            episodic_query=episodic_query,
        )


class ReviewerMemoryView:
    def __init__(self, service: MemoryContextService) -> None:
        self.__service = service

    def build(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        return self.__service._build_reviewer(event, ttt=ttt, executions=executions)


class MemoryContextService:
    """Build role-scoped prompt contexts; agents never receive a generic memory API."""

    def __init__(
        self,
        *,
        policy: MemoryAccessPolicy | None = None,
        repositories: Mapping[str, _ExcytinBenchRepositoryBase] | None = None,
    ) -> None:
        self.policy = policy or MemoryAccessPolicy()
        self.repositories = dict(
            build_default_repositories() if repositories is None else repositories
        )

    def planner_view(self) -> PlannerMemoryView:
        return PlannerMemoryView(self)

    def executor_view(self) -> ExecutorMemoryView:
        return ExecutorMemoryView(self)

    def reviewer_view(self) -> ReviewerMemoryView:
        return ReviewerMemoryView(self)

    def _repository(self, event: Event) -> _ExcytinBenchRepositoryBase | None:
        profile = resolve_memory_profile(event)
        return self.repositories.get(profile or "")

    def _build_planner(
        self,
        event: Event,
        *,
        review: RoundReview | None,
        ttt: TracebackTaskTree | None,
    ) -> dict[str, Any]:
        self.policy.require(
            RoleName.PLANNER, MemoryType.PROCEDURAL, MemoryType.SEMANTIC
        )
        repository = self._repository(event)
        if repository is None:
            return {"enabled": False, "reason": "no_memory_profile"}
        text = _event_text(event, review=review, ttt=ttt)
        procedures = repository.retrieve_procedures(text)
        semantic = repository.retrieve_semantic(text, procedures=procedures)
        prompt_procedures = self._compact_planner_procedures(procedures, semantic)
        return {
            "enabled": True,
            "profile": repository.profile,
            "role": RoleName.PLANNER.value,
            "allowed_memory_types": [
                MemoryType.PROCEDURAL.value,
                MemoryType.SEMANTIC.value,
            ],
            "procedural_memory": {"procedures": prompt_procedures},
            "semantic_memory": semantic,
        }

    @staticmethod
    def _compact_planner_procedures(
        procedures: Sequence[Mapping[str, Any]],
        semantic: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        selected_tables = {
            table["table_id"]: table["name"] for table in semantic.get("tables", [])
        }
        compact: list[dict[str, Any]] = []
        for procedure in procedures:
            procedure_view = {
                key: value for key, value in procedure.items() if key != "phases"
            }
            procedure_view["phases"] = []
            for phase in procedure.get("phases", []):
                phase_view = {
                    key: value
                    for key, value in phase.items()
                    if key != "semantic_table_ids"
                }
                phase_view["candidate_tables"] = [
                    selected_tables[table_id]
                    for table_id in phase.get("semantic_table_ids", [])
                    if table_id in selected_tables
                ]
                procedure_view["phases"].append(phase_view)
            compact.append(procedure_view)
        return compact

    def _build_executor(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any],
        include_episodic: bool,
        episodic_query: str | None,
    ) -> dict[str, Any]:
        required = [MemoryType.SEMANTIC]
        if include_episodic:
            required.append(MemoryType.EPISODIC)
        self.policy.require(RoleName.EXECUTOR, *required)
        repository = self._repository(event)
        if repository is None:
            return {"enabled": False, "reason": "no_memory_profile"}
        metadata = dict(getattr(node, "metadata", {}) or {})
        table_hints = list(metadata.get("candidate_tables") or [])
        for reference in metadata.get("semantic_memory_refs") or []:
            value = str(reference)
            if value.startswith("table:"):
                table_hints.append(value.split(":", 1)[1])
        previous = [
            execution.to_dict() if hasattr(execution, "to_dict") else execution
            for execution in executions[-5:]
        ]
        text = "\n".join(
            [
                _event_text(event),
                str(getattr(node, "title", "")),
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(previous, ensure_ascii=False),
            ]
        )
        semantic = repository.retrieve_semantic(
            text, table_hints=table_hints, table_limit=8
        )
        resolved_table_hints = table_hints or [
            table["name"] for table in semantic["tables"][:4]
        ]
        result = {
            "enabled": True,
            "profile": repository.profile,
            "role": RoleName.EXECUTOR.value,
            "allowed_memory_types": [item.value for item in required],
            "semantic_memory": semantic,
        }
        if include_episodic:
            episodes = repository.retrieve_episodes(
                episodic_query or text,
                table_hints=resolved_table_hints,
            )
            result["episodic_memory"] = {"episodes": episodes}
        return result

    def _build_reviewer(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any],
    ) -> dict[str, Any]:
        self.policy.require(RoleName.REVIEWER, MemoryType.SEMANTIC)
        repository = self._repository(event)
        if repository is None:
            return {"enabled": False, "reason": "no_memory_profile"}
        text = "\n".join(
            [
                _event_text(event, ttt=ttt),
                json.dumps(
                    [
                        item.to_dict() if hasattr(item, "to_dict") else item
                        for item in executions
                    ],
                    ensure_ascii=False,
                ),
            ]
        )
        return {
            "enabled": True,
            "profile": repository.profile,
            "role": RoleName.REVIEWER.value,
            "allowed_memory_types": [MemoryType.SEMANTIC.value],
            "semantic_memory": repository.retrieve_semantic(text, table_limit=8),
        }


__all__ = [
    "DEFAULT_ROLE_MEMORY_POLICY",
    "ExcytinBenchNeo4jRepository",
    "ExcytinBenchSnapshotRepository",
    "ExecutorContextProvider",
    "ExecutorMemoryView",
    "MemoryAccessPolicy",
    "MemoryContextService",
    "MemoryType",
    "NullExecutorContextProvider",
    "NullPlannerContextProvider",
    "NullReviewerContextProvider",
    "PlannerContextProvider",
    "PlannerMemoryView",
    "ReviewerContextProvider",
    "ReviewerMemoryView",
    "build_default_repositories",
    "resolve_memory_profile",
]
