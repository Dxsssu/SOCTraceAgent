from __future__ import annotations

import json
from copy import deepcopy
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
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
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import (
    catalog as procedure_catalog,
    evidence_sources,
)
from src.memory.longterm_memory.procedural_memory.excytin_bench.retrieval import (
    retrieve as retrieve_operations,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench import (
    DEFAULT_KNOWLEDGE_PATH as EXCYTIN_SEMANTIC_PATH,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench.runtime_view import (
    retrieval_view,
)
from src.memory.longterm_memory.semantic_memory.excytin_bench.store import SemanticMemoryStore, load_knowledge
from src.messaging import RoleName
from src.schema import Event, RoundReview, TracebackTaskTree
from src.schema.investigation import task_input
from src.schema.itt import itt_payload

logger = logging.getLogger(__name__)


class MemoryType(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


DEFAULT_ROLE_MEMORY_POLICY: Mapping[RoleName, frozenset[MemoryType]] = {
    RoleName.PLANNER: frozenset({MemoryType.PROCEDURAL, MemoryType.SEMANTIC}),
    RoleName.VERIFIER: frozenset(),
    RoleName.EXECUTOR: frozenset(
        {MemoryType.SEMANTIC, MemoryType.EPISODIC, MemoryType.PROCEDURAL}
    ),
    RoleName.REVIEWER: frozenset({MemoryType.SEMANTIC, MemoryType.PROCEDURAL}),
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
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]: ...


class ExecutorContextProvider(Protocol):
    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = False,
        episodic_query: str | None = None,
        episodic_background: str | None = None,
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
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        return {"enabled": False, "reason": "no_memory_profile"}


class NullExecutorContextProvider:
    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = False,
        episodic_query: str | None = None,
        episodic_background: str | None = None,
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


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(value or "")}


def _contains_hint(text: str, hint: str) -> bool:
    return hint.lower() in text.lower()


def _contains_identifier(text: str, identifier: str) -> bool:
    """Match a schema identifier, not e.g. Id inside Identify or OS inside host."""
    return bool(
        re.search(
            r"(?<![\w])" + re.escape(identifier) + r"(?![\w])", text, re.IGNORECASE
        )
    )


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
    parts = [json.dumps(task_input(event), ensure_ascii=False)]
    if review is not None:
        parts.append(json.dumps(review.to_dict(), ensure_ascii=False))
    if ttt is not None:
        parts.append(json.dumps(itt_payload(ttt), ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _retrieval_task(event, *, review=None, ttt=None, executions=(), node=None):
    task = task_input(event)
    if review is not None:
        task["review"] = {k: list(getattr(review, k)) for k in ("findings", "gaps", "recommendations")}
    if ttt is not None:
        task["itt"] = itt_payload(ttt)
    if node is not None:
        task["current_task"] = {"id": node.node_id, "objective": node.title}
    task["executions"] = [e.to_dict() if hasattr(e, "to_dict") else e for e in executions]
    return task


def _procedure_context(repository, task, *, role, selector):
    report = {}
    procedures = repository.retrieve_procedures(task, role=role, selector=selector, limit=1, diagnostics=report)
    result = {"procedures": procedures}
    result["retrieval"] = report
    return result


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
        self.semantic_store = SemanticMemoryStore(semantic_path)
        self._semantic = None
        self.procedural = json.loads(procedural_path.read_text(encoding="utf-8"))
        self.episodic = json.loads(episodic_path.read_text(encoding="utf-8"))
        self._initialize_indexes()

    def _initialize_indexes(self) -> None:
        from src.memory.longterm_memory.episodic_memory.excytin_bench.retrieval import RecoveryIndex
        self.recovery_index = RecoveryIndex(self.episodic)
        self._semantic = None

    @property
    def semantic(self):
        # Compatibility for offline/legacy retrieval. Online SM uses L1/L2 directly.
        if self._semantic is None:
            self._semantic = retrieval_view(load_knowledge(self.semantic_store.l1_path))
        return self._semantic

    def _initialize_semantic_indexes(self):
        self._tables_by_id = {t["table_id"]: t for t in self.semantic["tables"]}
        self._tables_by_name = {t["name"].lower(): t for t in self.semantic["tables"]}
        self._join_keys_by_id = {k["join_key_id"]: k for k in self.semantic["join_keys"]}

    def procedure_catalog(self) -> list[dict[str, str]]:
        return procedure_catalog(self.procedural)

    def retrieve_procedures(
        self, text: str | Mapping[str, Any], *, limit: int = 3, role="planner", selector=None, diagnostics=None
    ) -> list[dict[str, Any]]:
        return retrieve_operations(
            self.procedural, text, role=role, selector=selector, limit=limit, diagnostics=diagnostics
        )

    def retrieve_semantic(
        self,
        text: str,
        *,
        procedures: Sequence[Mapping[str, Any]] = (),
        table_hints: Sequence[str] = (),
        table_limit: int = 12,
    ) -> dict[str, Any]:
        self._initialize_semantic_indexes()
        semantic_types = _infer_semantic_types(text)
        query_tokens = _tokens(text)
        referenced_table_ids: set[str] = set()
        referenced_join_key_ids: set[str] = set()
        required_log_types: set[str] = set()
        referenced_fields: dict[str, set[str]] = {}
        # Explicit links load schema only after PM selection and prerequisite checks.
        for procedure in procedures:
            for ref in procedure.get("semantic_refs", []):
                table = self._tables_by_name.get(ref["table"].lower())
                if table is not None:
                    referenced_table_ids.add(table["table_id"])
                    referenced_fields.setdefault(table["table_id"], set()).update(
                        ref["fields"]
                    )

        hinted_ids: set[str] = set()
        for hint in table_hints:
            table = self._tables_by_id.get(hint) or self._tables_by_name.get(
                str(hint).lower()
            )
            if table:
                hinted_ids.add(table["table_id"])

        scored_tables: list[tuple[float, dict[str, Any]]] = []
        queryable = {t["table"] for t in self.semantic_store.catalog()["log_sources"]}
        for table in self.semantic["tables"]:
            if table["name"] not in queryable:
                continue
            score = 0.0
            table_name = str(table.get("name") or "")
            if table_name and _contains_identifier(text, table_name):
                score += 30.0
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
            # A question often names the desired field (for example,
            # AadDeviceId) without naming its table. Promote the table that
            # actually owns that field before applying the result limit.
            score += 40.0 * sum(
                bool(field.get("name"))
                and _contains_identifier(text, str(field["name"]))
                for field in table.get("fields", [])
            )
            score += 12.0 * sum(
                _contains_hint(text, hint) for hint in table.get("retrieval_hints", [])
            )
            if score > 0:
                scored_tables.append((score, table))
        scored_tables.sort(
            key=lambda item: (
                item[1]["table_id"] not in (referenced_table_ids | hinted_ids),
                -item[0],
                item[1]["name"],
            )
        )
        selected_tables = [item[1] for item in scored_tables[:table_limit]]

        selected_field_ids: set[str] = set()
        table_views: list[dict[str, Any]] = []
        for table in selected_tables:
            scored_fields: list[tuple[float, dict[str, Any]]] = []
            for field in table.get("fields", []):
                score = (
                    100.0
                    if field["name"] in referenced_fields.get(table["table_id"], set())
                    else 0.0
                )
                if _contains_identifier(text, field["name"]):
                    score += 24.0
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
            fields = [item[1] for item in scored_fields]
            selected_field_ids.update(field["field_id"] for field in fields)
            table_views.append(
                {
                    "table_id": table["table_id"],
                    "name": table["name"],
                    "description": table.get("description", ""),
                    "description_zh": table.get("description_zh", ""),
                    "field": [
                        {
                            "field_id": field["field_id"],
                            "name": field["name"],
                            "data_type": field.get("data_type"),
                            "description": field.get("description", ""),
                            "description_zh": field.get("description_zh", ""),
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


class _ExcytinBenchGraphRepository(_ExcytinBenchRepositoryBase):
    """Load PM/EM from Neo4j; SM always uses local L1/L2 files."""

    def __init__(
        self,
        *,
        uri: str,
        username: str,
        password: str,
        database: str = "neo4j",
    ) -> None:
        self.semantic_store = SemanticMemoryStore()
        driver = GraphDatabase.driver(uri, auth=(username, password))
        try:
            driver.verify_connectivity()
            with driver.session(database=database) as session:
                self.procedural = self._load_procedural_graph(session)
                self.episodic = self._load_episodic_graph(session)
        finally:
            driver.close()
        self._initialize_indexes()

    @staticmethod
    def _load_procedural_graph(session: Any) -> dict[str, Any]:
        record = session.run(
            "MATCH (catalog:ProcedureCatalog {procedural_catalog_id: 'excytin-query-templates-v5'}) "
            "RETURN catalog.snapshot_json AS snapshot_json"
        ).single()
        if record is None:
            raise ValueError("Neo4j is missing Procedural v5; rebuild and import it")
        from src.memory.longterm_memory.procedural_memory.excytin_bench.import_neo4j import (
            validate_knowledge,
        )

        knowledge = json.loads(record["snapshot_json"])
        validate_knowledge(knowledge)
        return knowledge

    @staticmethod
    def _load_episodic_graph(session: Any) -> dict[str, Any]:
        from src.memory.longterm_memory.episodic_memory.excytin_bench.build_knowledge import CATALOG_ID
        records = session.run(
            "MATCH (:EpisodeCatalog {episodic_catalog_id: $catalog_id})"
            "-[:CONTAINS_RECOVERY]->(r:FailureRecovery) "
            "RETURN r.background AS background, r.error AS error, r.repair AS repair "
            "ORDER BY r.position", catalog_id=CATALOG_ID,
        )
        entries = [dict(r) for r in records]
        if not entries:
            raise ValueError("Neo4j is missing failure-recovery EM v2; reimport memory")
        return {"format_version": "2.0", "entries": entries}

    def retrieve_recoveries(self, error: str, *, background: str = "",
                            table_hints: Sequence[str] = (), limit: int = 3):
        return self.recovery_index.retrieve(error, background=background,
            table_hints=table_hints, limit=limit)


class ExcytinBenchNeo4jRepository(_ExcytinBenchGraphRepository):
    """Public Neo4j-backed ExCyTIn repository."""


class ExcytinBenchSnapshotRepository(_ExcytinBenchGraphRepository):
    """Load PM/EM JSON snapshots and the local L1/L2 semantic store."""

    def __init__(
        self,
        *,
        semantic_path: Path = EXCYTIN_SEMANTIC_PATH,
        procedural_path: Path = EXCYTIN_PROCEDURAL_PATH,
        episodic_path: Path = EXCYTIN_EPISODIC_PATH,
    ) -> None:
        self.semantic_store = SemanticMemoryStore(semantic_path)
        self._semantic = None
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
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        return self.__service._build_planner(
            event, review=review, ttt=ttt, executions=executions
        )


class ExecutorMemoryView:
    def __init__(self, service: MemoryContextService) -> None:
        self.__service = service

    def build(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any] = (),
        include_episodic: bool = False,
        episodic_query: str | None = None,
        episodic_background: str | None = None,
    ) -> dict[str, Any]:
        return self.__service._build_executor(
            event,
            node=node,
            executions=executions,
            include_episodic=include_episodic,
            episodic_query=episodic_query,
            episodic_background=episodic_background,
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
        procedure_selector: Callable[[str, str], str] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.enabled = enabled if enabled is not None else os.getenv(
            "SOCAGENT_LTM_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._planner_pm_cache = {}
        self._planner_sm_cache = {}
        self.procedure_selector = procedure_selector
        self.policy = policy or MemoryAccessPolicy()
        self.repositories = dict(
            (build_default_repositories() if repositories is None else repositories)
            if self.enabled else {}
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

    def executor_recoveries(self, event: Event, *, error: str, sql: str) -> list[dict[str, Any]]:
        """Error-only EM access; never builds SM or PM context."""
        self.policy.require(RoleName.EXECUTOR, MemoryType.EPISODIC)
        repository = self._repository(event)
        if repository is None:
            return []
        return repository.retrieve_recoveries(error, background=sql, limit=1)

    def _build_planner(
        self,
        event: Event,
        *,
        review: RoundReview | None,
        ttt: TracebackTaskTree | None,
        executions: Sequence[Any] = (),
    ) -> dict[str, Any]:
        self.policy.require(
            RoleName.PLANNER, MemoryType.PROCEDURAL
        )
        repository = self._repository(event)
        if repository is None:
            return {"enabled": False, "reason": "no_memory_profile"}
        from src.schema.ttt_updates import walk
        nodes = [n for n, depth in walk(ttt) if depth == 3] if ttt else []
        failed = bool(executions and not getattr(executions[-1], "query_success", True))
        verifier_gap = bool(review and any("Verifier" in g for g in review.gaps))
        pending_without_refs = any(n.status.value == "todo" and not n.metadata.get("semantic_refs") for n in nodes)
        retained = any(n.metadata.get("semantic_refs") for n in nodes)
        if retained and not failed and not verifier_gap and not pending_without_refs:
            procedural = {"procedures": [], "reuse": "retained_itt_knowledge"}
        else:
            task = _retrieval_task(event, review=review, ttt=ttt, executions=executions)
            # New plans and repeated errors are not new binding evidence. Cache
            # negative results until facts, question, knowledge or verifier gap changes.
            cache_task = {**task_input(event), "sources": evidence_sources(task),
                          "verifier_gap": list(review.gaps) if verifier_gap else []}
            key = (event.event_id, repository.profile,
                   json.dumps(repository.procedural, sort_keys=True, ensure_ascii=False),
                   json.dumps(cache_task, sort_keys=True, ensure_ascii=False))
            if key in self._planner_pm_cache:
                procedural = deepcopy(self._planner_pm_cache[key])
                procedural["reuse"] = "unchanged_evidence"
            else:
                procedural = _procedure_context(repository, task,
                    role="planner", selector=self.procedure_selector)
                if len(self._planner_pm_cache) >= 128:
                    self._planner_pm_cache.pop(next(iter(self._planner_pm_cache)))
                self._planner_pm_cache[key] = deepcopy(procedural)
        semantic = None
        if not procedural.get("procedures") and procedural.get("reuse") != "retained_itt_knowledge":
            self.policy.require(RoleName.PLANNER, MemoryType.SEMANTIC)
            from src.memory.longterm_memory.semantic_memory.excytin_bench.fallback import retrieve
            sm_task = task_input(event)
            sm_key = (event.event_id, repository.profile,
                      repository.semantic_store.fingerprint,
                      json.dumps(sm_task, sort_keys=True, ensure_ascii=False))
            if sm_key not in self._planner_sm_cache:
                if len(self._planner_sm_cache) >= 128:
                    self._planner_sm_cache.pop(next(iter(self._planner_sm_cache)))
                self._planner_sm_cache[sm_key] = retrieve(repository, sm_task, self.procedure_selector)
            else:
                semantic = {**deepcopy(self._planner_sm_cache[sm_key]), "reuse": "unchanged_query"}
            if semantic is None:
                semantic = deepcopy(self._planner_sm_cache[sm_key])
        return {
            "enabled": True,
            "profile": repository.profile,
            "role": RoleName.PLANNER.value,
            "allowed_memory_types": [MemoryType.PROCEDURAL.value] + ([MemoryType.SEMANTIC.value] if semantic is not None else []),
            "procedural_memory": procedural,
            **({"semantic_memory": semantic} if semantic is not None else {}),
        }

    def _build_executor(
        self,
        event: Event,
        *,
        node: Any,
        executions: Sequence[Any],
        include_episodic: bool,
        episodic_query: str | None,
        episodic_background: str | None,
    ) -> dict[str, Any]:
        required = [MemoryType.SEMANTIC, MemoryType.PROCEDURAL]
        from src.memory.longterm_memory.episodic_memory.excytin_bench.sql_parser import classify_error
        include_episodic = include_episodic and classify_error(episodic_query or "")[0] is not None
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
        task_text = str(getattr(node, "title", ""))
        task_semantic = repository.retrieve_semantic(
            task_text, table_hints=table_hints, table_limit=3
        )
        resolved_table_hints = table_hints or [
            table["name"] for table in task_semantic["tables"]
        ]
        procedural = _procedure_context(repository,
            _retrieval_task(event, node=node, executions=executions[-5:]),
            role="executor", selector=self.procedure_selector)
        procedures = procedural["procedures"]
        semantic = repository.retrieve_semantic(
            text, procedures=procedures, table_hints=resolved_table_hints, table_limit=8
        )
        result = {
            "procedural_memory": procedural,
            "enabled": True,
            "profile": repository.profile,
            "role": RoleName.EXECUTOR.value,
            "allowed_memory_types": [item.value for item in required],
            "semantic_memory": semantic,
        }
        if include_episodic:
            recoveries = repository.retrieve_recoveries(
                episodic_query or "", background=episodic_background or task_text,
                table_hints=resolved_table_hints,
            )
            result["episodic_memory"] = {"recoveries": recoveries}
        return result

    def _build_reviewer(
        self,
        event: Event,
        *,
        ttt: TracebackTaskTree,
        executions: Sequence[Any],
    ) -> dict[str, Any]:
        self.policy.require(
            RoleName.REVIEWER, MemoryType.SEMANTIC, MemoryType.PROCEDURAL
        )
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
        procedural = _procedure_context(repository,
            _retrieval_task(event, ttt=ttt, executions=executions),
            role="reviewer", selector=self.procedure_selector)
        procedures = procedural["procedures"]
        semantic = repository.retrieve_semantic(
            text, procedures=procedures, table_limit=8
        )
        return {
            "enabled": True,
            "profile": repository.profile,
            "role": RoleName.REVIEWER.value,
            "allowed_memory_types": [
                MemoryType.SEMANTIC.value,
                MemoryType.PROCEDURAL.value,
            ],
            "semantic_memory": semantic,
            "procedural_memory": procedural,
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
