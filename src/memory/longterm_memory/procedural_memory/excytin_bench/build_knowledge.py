"""Build evidence-backed, skill-oriented ExCyTIn-Bench procedural memory."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import DEFAULT_KNOWLEDGE_PATH

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_SEMANTIC_KNOWLEDGE = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/semantic_memory/excytin_bench/semantic_memory.json"
)
DEFAULT_EPISODIC_KNOWLEDGE = (
    REPOSITORY_ROOT
    / "src/memory/longterm_memory/episodic_memory/excytin_bench/episodic_memory.json"
)
DEFAULT_INSIGHTS = (
    REPOSITORY_ROOT / "data/excytin-bench/github/secgym/agents/expel_train/insights.json"
)
DEFAULT_QUESTIONS_DIR = REPOSITORY_ROOT / "data/excytin-bench/huggingface/questions/train"

PROCEDURAL_CATALOG_ID = "excytin-procedures-v1"


PROCEDURE_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "slug": "alert-centered-investigation",
        "name": "Alert-Centered Investigation",
        "category": "base_workflow",
        "goal": "Trace an initial security alert through entities, raw logs, a bounded timeline, and validated evidence.",
        "description": "Default investigation workflow shared by all ExCyTIn-Bench security-log investigations.",
        "entry_entity_types": ["ALERT_ID", "INCIDENT_ID"],
        "applicable_log_types": [
            "incident_alert",
            "identity_signin",
            "endpoint_process",
            "endpoint_file",
            "endpoint_network",
            "email",
            "firewall_network",
            "cloud_application",
        ],
        "join_keys": [
            "alert_id",
            "system_alert_id",
            "device_id",
            "account_upn",
            "email_address",
            "ip_address",
            "network_message_id",
            "process_id",
        ],
        "insight_indexes": [0, 1, 4, 5, 9],
        "support_mode": "all",
        "phases": [
            {
                "slug": "ground-environment",
                "name": "Ground the environment",
                "goal": "Confirm the active log catalog before choosing evidence sources.",
                "required_log_types": [],
                "required_semantic_types": [],
                "join_keys": [],
                "query_kind": "schema_discovery",
                "success_condition": "Candidate tables and their valid fields are known.",
            },
            {
                "slug": "locate-initial-alert",
                "name": "Locate the initial alert",
                "goal": "Anchor the investigation in the alert described by the task context.",
                "required_log_types": ["incident_alert"],
                "required_semantic_types": ["ALERT_ID", "INCIDENT_ID", "TIMESTAMP"],
                "join_keys": ["alert_id", "system_alert_id"],
                "query_kind": "evidence_query",
                "success_condition": "The initial alert and its time range are identified.",
            },
            {
                "slug": "extract-evidence-entities",
                "name": "Extract evidence entities",
                "goal": "Extract accounts, devices, network indicators, messages, files, and processes from structured alert evidence.",
                "required_log_types": ["incident_alert"],
                "required_semantic_types": [
                    "ACCOUNT_ID",
                    "EMAIL_ADDRESS",
                    "DEVICE_ID",
                    "IP_ADDRESS",
                    "MESSAGE_ID",
                    "FILE_HASH",
                    "PROCESS_ID",
                ],
                "join_keys": ["alert_id"],
                "query_kind": "evidence_query",
                "success_condition": "At least one scoped pivot entity is supported by alert evidence.",
            },
            {
                "slug": "select-investigation-branch",
                "name": "Select an investigation branch",
                "goal": "Choose email, endpoint, identity, or network investigation according to the supported entity types.",
                "required_log_types": [],
                "required_semantic_types": [],
                "join_keys": [],
                "query_kind": None,
                "success_condition": "A branch is selected from evidence rather than keyword guessing.",
            },
            {
                "slug": "correlate-events",
                "name": "Correlate events",
                "goal": "Expand evidence only through Semantic Memory join keys and their required scopes.",
                "required_log_types": [],
                "required_semantic_types": [],
                "join_keys": [
                    "device_id",
                    "account_upn",
                    "email_address",
                    "ip_address",
                    "network_message_id",
                    "process_id",
                ],
                "query_kind": None,
                "success_condition": "Every cross-log pivot has a valid identifier and scope.",
            },
            {
                "slug": "build-timeline",
                "name": "Build a bounded timeline",
                "goal": "Order the correlated evidence within the incident time window.",
                "required_log_types": [],
                "required_semantic_types": ["TIMESTAMP"],
                "join_keys": [],
                "query_kind": "evidence_query",
                "success_condition": "Relevant events are temporally aligned with the initial alert.",
            },
            {
                "slug": "validate-conclusion",
                "name": "Validate the conclusion",
                "goal": "Require the final claim to reconnect to the initial alert through entities and time.",
                "required_log_types": [],
                "required_semantic_types": [],
                "join_keys": [],
                "query_kind": None,
                "success_condition": "The answer is supported by an explicit, scoped evidence chain.",
            },
        ],
    },
    {
        "slug": "email-threat-investigation",
        "name": "Email Threat Investigation",
        "category": "domain_workflow",
        "extends": "alert-centered-investigation",
        "goal": "Trace an email alert through message, URL, sender, recipient, delivery, and remediation evidence.",
        "description": "Use when alert evidence contains a URL, email address, or message identifier.",
        "entry_entity_types": ["URL", "EMAIL_ADDRESS", "MESSAGE_ID"],
        "applicable_log_types": ["email"],
        "join_keys": [
            "alert_id",
            "network_message_id",
            "internet_message_id",
            "email_address",
            "ip_address",
        ],
        "insight_indexes": [75, 100, 105, 115, 126],
        "phases": [
            {
                "slug": "anchor-email-alert",
                "name": "Anchor the email alert",
                "goal": "Confirm the alert and structured email evidence before searching broad activity logs.",
                "required_log_types": ["incident_alert"],
                "required_semantic_types": ["ALERT_ID", "URL", "EMAIL_ADDRESS", "MESSAGE_ID"],
                "join_keys": ["alert_id"],
                "query_kind": "evidence_query",
                "success_condition": "An alert-linked email, URL, or message entity is identified.",
            },
            {
                "slug": "resolve-message",
                "name": "Resolve the message",
                "goal": "Resolve URL or address evidence to a stable message identifier.",
                "required_log_types": ["email"],
                "required_semantic_types": ["URL", "EMAIL_ADDRESS", "MESSAGE_ID"],
                "join_keys": ["network_message_id", "internet_message_id"],
                "query_kind": "evidence_query",
                "success_condition": "A message identifier is supported by email evidence.",
            },
            {
                "slug": "inspect-delivery",
                "name": "Inspect delivery and participants",
                "goal": "Identify sender, recipient, delivery state, and message time.",
                "required_log_types": ["email"],
                "required_semantic_types": ["EMAIL_ADDRESS", "ACTION", "STATUS", "TIMESTAMP"],
                "join_keys": ["network_message_id", "email_address"],
                "query_kind": "evidence_query",
                "success_condition": "Message participants and delivery state are established.",
            },
            {
                "slug": "inspect-remediation",
                "name": "Inspect post-delivery remediation",
                "goal": "Determine whether and when the message was removed, quarantined, or otherwise remediated.",
                "required_log_types": ["email"],
                "required_semantic_types": ["ACTION", "STATUS", "MESSAGE_ID", "TIMESTAMP"],
                "join_keys": ["network_message_id"],
                "query_kind": "evidence_query",
                "success_condition": "The post-delivery action is connected to the same message.",
            },
            {
                "slug": "validate-email-chain",
                "name": "Validate the email chain",
                "goal": "Reconnect the message, participants, and remediation evidence to the original alert and time window.",
                "required_log_types": ["incident_alert", "email"],
                "required_semantic_types": ["ALERT_ID", "MESSAGE_ID", "TIMESTAMP"],
                "join_keys": ["alert_id", "network_message_id"],
                "query_kind": None,
                "success_condition": "The claimed email entity is supported across alert and email logs.",
            },
        ],
    },
    {
        "slug": "endpoint-process-investigation",
        "name": "Endpoint Process Investigation",
        "category": "domain_workflow",
        "extends": "alert-centered-investigation",
        "goal": "Trace an endpoint alert through device, process, command line, file, network, registry, and logon evidence.",
        "description": "Use when alert evidence contains a device, process, command line, file, or hash.",
        "entry_entity_types": ["DEVICE_ID", "DEVICE_NAME", "PROCESS_ID", "COMMAND_LINE", "FILE_HASH"],
        "applicable_log_types": ["endpoint_process", "endpoint_file", "endpoint_network"],
        "join_keys": [
            "alert_id",
            "device_id",
            "device_name",
            "process_id",
            "account_sid",
            "sha1",
            "sha256",
            "ip_address",
        ],
        "insight_indexes": [9, 68, 80, 137, 148],
        "phases": [
            {
                "slug": "establish-device-scope",
                "name": "Establish device and time scope",
                "goal": "Resolve the affected device and incident window before using process identifiers.",
                "required_log_types": ["incident_alert", "endpoint_network"],
                "required_semantic_types": ["DEVICE_ID", "DEVICE_NAME", "TIMESTAMP"],
                "join_keys": ["device_id", "device_name"],
                "query_kind": "evidence_query",
                "success_condition": "A device identity and bounded time window are known.",
            },
            {
                "slug": "locate-process",
                "name": "Locate the process",
                "goal": "Identify the relevant process, command line, account, and parent execution context.",
                "required_log_types": ["endpoint_process"],
                "required_semantic_types": ["PROCESS_ID", "COMMAND_LINE", "ACCOUNT_ID", "FILE_NAME"],
                "join_keys": ["device_id", "process_id", "account_sid"],
                "query_kind": "evidence_query",
                "success_condition": "The process is scoped by device and time, not PID alone.",
            },
            {
                "slug": "expand-endpoint-evidence",
                "name": "Expand endpoint evidence",
                "goal": "Follow the process into file, network, registry, and logon activity where relevant.",
                "required_log_types": ["endpoint_file", "endpoint_network"],
                "required_semantic_types": ["FILE_HASH", "FILE_PATH", "IP_ADDRESS", "ACTION"],
                "join_keys": ["device_id", "process_id", "sha1", "sha256", "ip_address"],
                "query_kind": "evidence_query",
                "success_condition": "Downstream activity remains tied to the same device, process context, and time.",
            },
            {
                "slug": "build-process-timeline",
                "name": "Build the process timeline",
                "goal": "Order parent, child, file, and network events around the suspicious execution.",
                "required_log_types": ["endpoint_process", "endpoint_file", "endpoint_network"],
                "required_semantic_types": ["TIMESTAMP", "PROCESS_ID", "COMMAND_LINE"],
                "join_keys": ["device_id", "process_id"],
                "query_kind": "evidence_query",
                "success_condition": "The execution chain is temporally coherent.",
            },
            {
                "slug": "validate-endpoint-chain",
                "name": "Validate the endpoint chain",
                "goal": "Confirm the attributed device, process, and activity reconnect to the alert evidence.",
                "required_log_types": ["incident_alert", "endpoint_process"],
                "required_semantic_types": ["ALERT_ID", "DEVICE_ID", "PROCESS_ID", "TIMESTAMP"],
                "join_keys": ["alert_id", "device_id", "process_id"],
                "query_kind": None,
                "success_condition": "No conclusion relies on an unscoped PID, device name, or shared indicator.",
            },
        ],
    },
    {
        "slug": "identity-signin-investigation",
        "name": "Identity and Sign-in Investigation",
        "category": "domain_workflow",
        "extends": "alert-centered-investigation",
        "goal": "Trace an identity alert through accounts, sign-ins, risk events, directory actions, and cloud activity.",
        "description": "Use when evidence contains an account, UPN, SID, sign-in IP, or identity-risk signal.",
        "entry_entity_types": ["ACCOUNT_ID", "ACCOUNT_NAME", "EMAIL_ADDRESS", "IP_ADDRESS"],
        "applicable_log_types": ["identity_signin", "cloud_application"],
        "join_keys": [
            "alert_id",
            "account_sid",
            "account_object_id",
            "account_upn",
            "email_address",
            "ip_address",
            "device_id",
        ],
        "insight_indexes": [79, 87, 89, 119, 135],
        "phases": [
            {
                "slug": "establish-account-scope",
                "name": "Establish account and time scope",
                "goal": "Resolve the account identifiers, tenant, source IP, and incident window.",
                "required_log_types": ["incident_alert", "identity_signin"],
                "required_semantic_types": ["ACCOUNT_ID", "EMAIL_ADDRESS", "IP_ADDRESS", "TIMESTAMP"],
                "join_keys": ["account_sid", "account_object_id", "account_upn", "email_address"],
                "query_kind": "evidence_query",
                "success_condition": "The account and time scope are supported by alert or sign-in evidence.",
            },
            {
                "slug": "inspect-signins",
                "name": "Inspect sign-ins",
                "goal": "Review interactive and non-interactive sign-ins, authentication outcome, application, IP, and risk.",
                "required_log_types": ["identity_signin"],
                "required_semantic_types": ["EMAIL_ADDRESS", "IP_ADDRESS", "APPLICATION_ID", "STATUS", "TIMESTAMP"],
                "join_keys": ["account_upn", "email_address", "ip_address"],
                "query_kind": "evidence_query",
                "success_condition": "Relevant sign-ins are separated from unrelated activity by account and time.",
            },
            {
                "slug": "inspect-risk-and-actions",
                "name": "Inspect risk and administrative actions",
                "goal": "Correlate risk events, directory changes, audit operations, and cloud activity.",
                "required_log_types": ["identity_signin", "cloud_application"],
                "required_semantic_types": ["ACCOUNT_ID", "ACTION", "STATUS", "TIMESTAMP"],
                "join_keys": ["account_object_id", "account_upn", "email_address"],
                "query_kind": "evidence_query",
                "success_condition": "Identity actions are tied to the scoped account rather than name similarity alone.",
            },
            {
                "slug": "correlate-identity-context",
                "name": "Correlate identity context",
                "goal": "Compare account, IP, device, application, tenant, and time before attributing activity.",
                "required_log_types": ["identity_signin", "cloud_application"],
                "required_semantic_types": ["ACCOUNT_ID", "IP_ADDRESS", "DEVICE_ID", "APPLICATION_ID", "TIMESTAMP"],
                "join_keys": ["account_object_id", "account_upn", "ip_address", "device_id"],
                "query_kind": "evidence_query",
                "success_condition": "The identity correlation uses multiple scoped attributes.",
            },
            {
                "slug": "validate-identity-chain",
                "name": "Validate the identity chain",
                "goal": "Reconnect the attributed account activity to the initial alert and incident window.",
                "required_log_types": ["incident_alert", "identity_signin"],
                "required_semantic_types": ["ALERT_ID", "ACCOUNT_ID", "TIMESTAMP"],
                "join_keys": ["alert_id", "account_sid", "account_object_id", "account_upn"],
                "query_kind": None,
                "success_condition": "The conclusion does not rely on an unscoped display name or shared IP.",
            },
        ],
    },
    {
        "slug": "network-activity-investigation",
        "name": "Network Activity Investigation",
        "category": "domain_workflow",
        "extends": "alert-centered-investigation",
        "goal": "Trace a network indicator through direction, device, process, firewall, and threat-intelligence evidence.",
        "description": "Use when evidence contains an IP, URL, domain, port, protocol, or network connection.",
        "entry_entity_types": ["IP_ADDRESS", "URL", "DOMAIN", "PORT", "PROTOCOL"],
        "applicable_log_types": ["endpoint_network", "firewall_network"],
        "join_keys": ["alert_id", "device_id", "device_name", "ip_address", "process_id"],
        "insight_indexes": [0, 23, 44, 68, 134],
        "phases": [
            {
                "slug": "establish-network-scope",
                "name": "Establish indicator and direction",
                "goal": "Resolve the network indicator, local/remote direction, protocol, port, and incident window.",
                "required_log_types": ["incident_alert", "endpoint_network", "firewall_network"],
                "required_semantic_types": ["IP_ADDRESS", "URL", "DOMAIN", "PORT", "PROTOCOL", "TIMESTAMP"],
                "join_keys": ["ip_address"],
                "query_kind": "evidence_query",
                "success_condition": "The indicator meaning and traffic direction are explicit.",
            },
            {
                "slug": "identify-device-process",
                "name": "Identify device and process context",
                "goal": "Find the device and initiating process responsible for the connection when endpoint evidence exists.",
                "required_log_types": ["endpoint_network", "endpoint_process"],
                "required_semantic_types": ["DEVICE_ID", "DEVICE_NAME", "PROCESS_ID", "COMMAND_LINE"],
                "join_keys": ["device_id", "device_name", "process_id"],
                "query_kind": "evidence_query",
                "success_condition": "The connection is tied to a scoped device and execution context.",
            },
            {
                "slug": "inspect-network-controls",
                "name": "Inspect network controls",
                "goal": "Correlate endpoint connections with firewall, DNS, bastion, or threat-intelligence activity.",
                "required_log_types": ["endpoint_network", "firewall_network"],
                "required_semantic_types": ["IP_ADDRESS", "URL", "DOMAIN", "ACTION", "STATUS", "TIMESTAMP"],
                "join_keys": ["ip_address", "device_id"],
                "query_kind": "evidence_query",
                "success_condition": "Network-control evidence refers to the same indicator and time window.",
            },
            {
                "slug": "build-network-timeline",
                "name": "Build the network timeline",
                "goal": "Order alert, endpoint, and network-control observations around the suspicious connection.",
                "required_log_types": ["endpoint_network", "firewall_network"],
                "required_semantic_types": ["TIMESTAMP", "IP_ADDRESS", "DEVICE_ID"],
                "join_keys": ["ip_address", "device_id"],
                "query_kind": "evidence_query",
                "success_condition": "The network activity is temporally coherent across sources.",
            },
            {
                "slug": "validate-network-chain",
                "name": "Validate the network chain",
                "goal": "Reconnect the indicator, direction, device, process, and timeline to the initial alert.",
                "required_log_types": ["incident_alert", "endpoint_network", "firewall_network"],
                "required_semantic_types": ["ALERT_ID", "IP_ADDRESS", "DEVICE_ID", "TIMESTAMP"],
                "join_keys": ["alert_id", "ip_address", "device_id"],
                "query_kind": None,
                "success_condition": "No conclusion relies on a shared IP without device, direction, and time context.",
            },
        ],
    },
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_procedure_id(slug: str) -> str:
    return f"{PROCEDURAL_CATALOG_ID}:procedure:{slug}"


def build_indexes(semantic: dict[str, Any]) -> dict[str, Any]:
    tables = {table["table_id"]: table for table in semantic["tables"]}
    fields = {
        field["field_id"]: field
        for table in semantic["tables"]
        for field in table["fields"]
    }
    join_keys = {item["name"]: item for item in semantic["join_keys"]}
    return {"tables": tables, "fields": fields, "join_keys": join_keys}


def attempt_metadata(
    attempt: dict[str, Any], indexes: dict[str, Any]
) -> tuple[set[str], set[str]]:
    log_types = {
        indexes["tables"][reference["table_id"]]["log_type"]
        for reference in attempt["table_refs"]
    }
    semantic_types = {
        indexes["fields"][reference["field_id"]]["semantic_type"]
        for reference in attempt["field_refs"]
    }
    return log_types, semantic_types


def select_exemplars(
    *,
    episodes: list[dict[str, Any]],
    indexes: dict[str, Any],
    required_log_types: set[str],
    required_semantic_types: set[str],
    query_kind: str | None,
    limit: int = 5,
) -> list[str]:
    if query_kind is None:
        return []
    candidates: list[tuple[int, str, str]] = []
    result_rank = {"rows_returned": 0, "schema_result": 1, "empty_result": 2, "error": 3}
    for episode in episodes:
        for attempt in episode["attempts"]:
            if attempt["query_kind"] != query_kind or attempt["execution_status"] != "executed":
                continue
            log_types, semantic_types = attempt_metadata(attempt, indexes)
            if required_log_types and not log_types.intersection(required_log_types):
                continue
            if required_semantic_types and not semantic_types.intersection(required_semantic_types):
                continue
            candidates.append(
                (
                    result_rank[attempt["result_status"]],
                    episode["episode_id"],
                    attempt["attempt_id"],
                )
            )
    selected: list[str] = []
    selected_episodes: set[str] = set()
    for _, episode_id, attempt_id in sorted(candidates):
        if episode_id in selected_episodes:
            continue
        selected.append(attempt_id)
        selected_episodes.add(episode_id)
        if len(selected) == limit:
            break
    return selected


def question_reference_counts(
    episodes: list[dict[str, Any]], questions_dir: Path
) -> tuple[int, int]:
    questions: dict[tuple[str, int], dict[str, Any]] = {}
    for path in sorted(questions_dir.glob("*.json")):
        for index, question in enumerate(load_json(path)):
            questions[(path.name, index)] = question
    solution_count = 0
    path_count = 0
    for episode in episodes:
        question = questions[(episode["source_question_file"], episode["source_question_index"])]
        solution_count += bool(question.get("solution"))
        path_count += bool(question.get("shortest_alert_path"))
    return solution_count, path_count


def build_knowledge(
    *,
    semantic_path: Path,
    episodic_path: Path,
    insights_path: Path,
    questions_dir: Path,
) -> dict[str, Any]:
    semantic = load_json(semantic_path)
    episodic = load_json(episodic_path)
    insights = load_json(insights_path)
    if not isinstance(insights, list) or not all(isinstance(item, str) for item in insights):
        raise TypeError("insights.json must contain a list of strings")
    indexes = build_indexes(semantic)
    episodes = episodic["episodes"]
    all_log_types = {table["log_type"] for table in semantic["tables"]}

    procedures: list[dict[str, Any]] = []
    for definition in PROCEDURE_DEFINITIONS:
        missing_types = set(definition["applicable_log_types"]).difference(all_log_types)
        if missing_types:
            raise ValueError(f"Unknown log types in {definition['slug']}: {sorted(missing_types)}")
        missing_keys = set(definition["join_keys"]).difference(indexes["join_keys"])
        if missing_keys:
            raise ValueError(f"Unknown join keys in {definition['slug']}: {sorted(missing_keys)}")
        if any(index >= len(insights) for index in definition["insight_indexes"]):
            raise ValueError(f"Unknown insight index in {definition['slug']}")

        procedure_types = set(definition["applicable_log_types"])
        support_episodes: list[dict[str, Any]] = []
        support_attempts: list[dict[str, Any]] = []
        for episode in episodes:
            matching_attempts = []
            for attempt in episode["attempts"]:
                log_types, _ = attempt_metadata(attempt, indexes)
                if definition.get("support_mode") == "all" or log_types.intersection(procedure_types):
                    matching_attempts.append(attempt)
            if definition.get("support_mode") == "all" or matching_attempts:
                support_episodes.append(episode)
                support_attempts.extend(matching_attempts)

        solution_count, path_count = question_reference_counts(support_episodes, questions_dir)
        procedure_id = stable_procedure_id(definition["slug"])
        phases: list[dict[str, Any]] = []
        for sequence_no, phase_definition in enumerate(definition["phases"], start=1):
            phase_types = set(phase_definition["required_log_types"])
            phase_semantic_types = set(phase_definition["required_semantic_types"])
            phase_join_keys = phase_definition["join_keys"]
            unknown_phase_types = phase_types.difference(all_log_types)
            unknown_phase_keys = set(phase_join_keys).difference(indexes["join_keys"])
            if unknown_phase_types or unknown_phase_keys:
                raise ValueError(
                    f"Invalid Semantic requirements in {definition['slug']}/{phase_definition['slug']}"
                )
            semantic_table_ids = sorted(
                table["table_id"]
                for table in semantic["tables"]
                if table["log_type"] in phase_types
            )
            semantic_join_key_ids = sorted(
                indexes["join_keys"][name]["join_key_id"] for name in phase_join_keys
            )
            phase_id = f"{procedure_id}:phase:{phase_definition['slug']}"
            phases.append(
                {
                    "phase_id": phase_id,
                    "sequence_no": sequence_no,
                    "name": phase_definition["name"],
                    "goal": phase_definition["goal"],
                    "required_log_types": phase_definition["required_log_types"],
                    "required_semantic_types": phase_definition["required_semantic_types"],
                    "semantic_table_ids": semantic_table_ids,
                    "semantic_join_key_ids": semantic_join_key_ids,
                    "exemplar_attempt_ids": select_exemplars(
                        episodes=support_episodes,
                        indexes=indexes,
                        required_log_types=phase_types,
                        required_semantic_types=phase_semantic_types,
                        query_kind=phase_definition["query_kind"],
                    ),
                    "success_condition": phase_definition["success_condition"],
                    "retrieval_text": " | ".join(
                        [
                            definition["name"],
                            phase_definition["name"],
                            phase_definition["goal"],
                            " ".join(phase_definition["required_log_types"]),
                            " ".join(phase_definition["required_semantic_types"]),
                        ]
                    ).strip(" |"),
                }
            )
        transitions = [
            {"from_phase_id": current["phase_id"], "to_phase_id": following["phase_id"]}
            for current, following in pairwise(phases)
        ]
        semantic_table_ids = sorted(
            table["table_id"]
            for table in semantic["tables"]
            if table["log_type"] in procedure_types
        )
        procedures.append(
            {
                "procedure_id": procedure_id,
                "skill_id": definition["slug"],
                "skill_path": f"skills/{definition['slug']}/SKILL.md",
                "name": definition["name"],
                "category": definition["category"],
                "goal": definition["goal"],
                "description": definition["description"],
                "entry_entity_types": definition["entry_entity_types"],
                "applicable_log_types": definition["applicable_log_types"],
                "semantic_table_ids": semantic_table_ids,
                "semantic_join_key_ids": sorted(
                    indexes["join_keys"][name]["join_key_id"]
                    for name in definition["join_keys"]
                ),
                "extends_procedure_id": (
                    stable_procedure_id(definition["extends"])
                    if definition.get("extends")
                    else None
                ),
                "source_insight_indexes": definition["insight_indexes"],
                "support_episode_ids": sorted(
                    episode["episode_id"] for episode in support_episodes
                ),
                "support_episode_count": len(support_episodes),
                "support_attempt_count": len(support_attempts),
                "support_incidents": sorted(
                    {episode["source_incident"] for episode in support_episodes}
                ),
                "reference_solution_count": solution_count,
                "reference_path_count": path_count,
                "status": "active",
                "retrieval_eligible": True,
                "retrieval_text": " | ".join(
                    [
                        definition["name"],
                        definition["goal"],
                        "entities: " + " ".join(definition["entry_entity_types"]),
                        "logs: " + " ".join(definition["applicable_log_types"]),
                    ]
                ),
                "phases": phases,
                "transitions": transitions,
            }
        )

    phases = [phase for procedure in procedures for phase in procedure["phases"]]
    support_edges = sum(len(procedure["support_episode_ids"]) for procedure in procedures)
    exemplar_edges = sum(len(phase["exemplar_attempt_ids"]) for phase in phases)
    return {
        "format_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "generation_policy": {
            "source_split": "train",
            "contains_sql": False,
            "contains_log_values": False,
            "contains_answers": False,
            "contains_test_data": False,
            "skill_oriented": True,
        },
        "environment_id": semantic["environment"]["environment_id"],
        "semantic_catalog": {
            "catalog_id": semantic["catalog"]["catalog_id"],
            "schema_hash": semantic["catalog"]["schema_hash"],
        },
        "episodic_catalog": {
            "episodic_catalog_id": episodic["catalog"]["episodic_catalog_id"],
            "source_sha256": episodic["catalog"]["source_sha256"],
        },
        "catalog": {
            "procedural_catalog_id": PROCEDURAL_CATALOG_ID,
            "name": "ExCyTIn-Bench Investigation Skills",
            "version": "1.0",
            "semantic_source": semantic_path.resolve().relative_to(REPOSITORY_ROOT).as_posix(),
            "semantic_source_sha256": sha256_file(semantic_path),
            "episodic_source": episodic_path.resolve().relative_to(REPOSITORY_ROOT).as_posix(),
            "episodic_source_sha256": sha256_file(episodic_path),
            "insights_source": insights_path.resolve().relative_to(REPOSITORY_ROOT).as_posix(),
            "insights_source_sha256": sha256_file(insights_path),
            "insight_count": len(insights),
            "procedure_count": len(procedures),
            "phase_count": len(phases),
            "support_edge_count": support_edges,
            "exemplar_edge_count": exemplar_edges,
        },
        "procedures": procedures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ExCyTIn-Bench procedural memory.")
    parser.add_argument("--semantic-knowledge", type=Path, default=DEFAULT_SEMANTIC_KNOWLEDGE)
    parser.add_argument("--episodic-knowledge", type=Path, default=DEFAULT_EPISODIC_KNOWLEDGE)
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS)
    parser.add_argument("--questions-dir", type=Path, default=DEFAULT_QUESTIONS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_KNOWLEDGE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    knowledge = build_knowledge(
        semantic_path=args.semantic_knowledge.resolve(),
        episodic_path=args.episodic_knowledge.resolve(),
        insights_path=args.insights.resolve(),
        questions_dir=args.questions_dir.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(knowledge, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {args.output.resolve()}: {knowledge['catalog']['procedure_count']} procedures, "
        f"{knowledge['catalog']['phase_count']} phases, "
        f"{knowledge['catalog']['support_edge_count']} episode supports, and "
        f"{knowledge['catalog']['exemplar_edge_count']} query exemplars."
    )


if __name__ == "__main__":
    main()
