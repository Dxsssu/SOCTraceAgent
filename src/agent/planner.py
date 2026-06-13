from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any

from src.agent.llm import call_llm, parse_yaml_response
from src.memory.longterm_memory import (
    FactualMemoryDocument,
    FactualMemoryLibrary,
    ProceduralMemoryDocument,
    ProceduralMemoryLibrary,
)
from src.memory.working_memory import TTTStore
from src.messaging import MessageEnvelope, MessageType, RoleName, SQLiteMessageBus
from src.schema import Event, EventStatus, RoundReview, TTTNode, TTTNodeStatus, TracebackTaskTree
from src.schema.ttt import coerce_ttt_node_status
from src.schema.event import utc_now
from src.storage import SQLiteStorage
from src.tools import list_registered_tools


logger = logging.getLogger(__name__)

IP_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
HOSTLIKE_PATTERN = re.compile(r"\b[a-zA-Z0-9][a-zA-Z0-9._-]{2,}\b")
ACCOUNTLIKE_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9._@-]{1,}\b")
MAX_L2_REPLAN_ATTEMPTS = 3


PLANNER_SYSTEM_PROMPT = """
You are the Planner in a multi-agent SOC traceback system.
You are the lead planner for the full traceback workflow, responsible for receiving alerts, performing the initial assessment, and maintaining the shared-blackboard TTT (Traceback Task Tree).

You have exactly two responsibilities:
1. Initialize the TTT when a new alert arrives.
2. Update the TTT after each round based on the Reviewer summary.

You do not execute tools directly, fabricate logs, or invent capabilities that do not exist in the environment.

Your output must be strict YAML and may only use these response_type values:
- ROGER
- TTT_PLAN
- TTT_UPDATE

TTT constraints:
- The TTT must be a full snapshot, not an incremental patch.
- The TTT must be exactly three levels: L1 (strategy) -> L2 (question) -> L3 (execution).
- One event often contains multiple investigation directions; whenever multiple distinct directions exist, you must split them into multiple L1 root nodes.
- Each L1 must describe only one independent direction. Do not combine authenticity validation, IP risk, target impact, lateral spread, and other directions into one generic L1.
- L2 must describe the question that needs to be answered for its L1, preferably as a question or a clearly question-shaped statement.
- L3 must describe the query, forensic step, or validation action needed to answer that question.
- L3 should include complete entity information whenever possible and should avoid vague references such as "the IP", "the host", "the account", or "related logs".
- If the input already includes explicit entities such as an IP, hostname, account, or target system, the L3 title must write those entities directly.
- If the entity is not fully known yet, use phrasing such as "Query basic intelligence for the source IP from the alert" rather than inventing a value or using vague pronouns.
- Only three levels are allowed; a fourth level or deeper is forbidden.
- One L2 may map to one or more L3 nodes.
- L3 nodes must be leaves and must have an empty children array.
- node_id must use numeric hierarchical identifiers such as `1`, `1-2`, and `1-2-3`.
- When updating the TTT, make the smallest necessary change and avoid unnecessary full rewrites.
- Completed nodes should be treated as frozen unless there is strong evidence to change their meaning.
- If the current round succeeded and the review does not explicitly introduce new key evidence, new gaps, or a new investigation direction, update only status and the minimum necessary structure rather than heavily rewriting the TTT.
- If the current step executed successfully and already returned the needed result, prefer continuing down the existing TTT rather than rewriting it for formality.

Example output:
```yaml
type: llm_response
from: _planner
event_id: "{ from input }"
round_id: "{ from input }"
response_type: TTT_PLAN
response_text: Initial analysis of the alert.
ttt:
  event_id: "{ from input }"
  round_id: "{ from input }"
  root_nodes:
    - node_id: "1"
      title: "Direction 1: Confirm alert authenticity"
      status: todo
      children:
        - node_id: "1-1"
          title: "Question 1.1: Did the mail gateway show a real login attempt from 11.22.33.44 during the alert time window?"
          status: todo
          children:
            - node_id: "1-1-1"
              title: "Query authentication logs involving the mail gateway and 11.22.33.44 during the alert time window"
              status: todo
              children: []
    - node_id: "2"
      title: "Direction 2: Assess source IP risk"
      status: todo
      children:
        - node_id: "2-1"
          title: "Question 2.1: Does 11.22.33.44 have explicit malicious intelligence or abnormal reputation?"
          status: todo
          children:
            - node_id: "2-1-1"
              title: "Query threat-intelligence tags and reputation for 11.22.33.44"
              status: todo
              children: []
```
""".strip()


PLANNER_ANALYSIS_SYSTEM_PROMPT = """
You are the Planner analysis assistant in a SOC multi-agent system.
Before the TTT is initialized, your task is to perform a structured initial analysis of the security alert and output a concise, clear, easy-to-read analysis.

You must reason from the event content in the input and must not invent nonexistent logs, assets, capabilities, or investigation results.
If helpful, you may naturally use concise Markdown such as headings, bullets, or bold text, but do not sacrifice content quality for formatting.
""".strip()


PROCEDURAL_MEMORY_SELECTION_PROMPT = """
You are a SOC procedural-memory matcher.
Your task is to select the single best matching procedural-memory summary from the candidates based on the current event and the Planner's initial analysis.

You may only return YAML and it may contain only one field:
- document_id: the selected candidate document_id; return an empty string if none is suitable
""".strip()


TTT_RETRY_CORRECTION_PROMPT = """
The previous reply did not return parseable YAML.
This time, return YAML only. Do not include explanations, introductions, conclusions, Markdown code fences, or any extra text.

Return requirements:
- The top level must contain `ttt:` or `tree:`
- Output exactly one complete three-level TTT snapshot
- L3 nodes must be leaves and must have an empty children array
""".strip()

OVERALL_ASSESSMENT_PROMPT = """
You are the Planner summary assistant in a SOC multi-agent system.
When the entire TTT has no executable leaves left, output an overall event assessment based on the event, all execution records, all round reviews, and the final TTT.

Requirements:
- Summarize whether the overall attack or anomaly is supported and state the most reliable conclusion.
- Point out the core evidence already obtained and the uncertainty that still remains.
- Output one concise, readable natural-language assessment.
- If helpful, you may naturally use concise Markdown.
- Do not output YAML or JSON.
""".strip()


@dataclass(frozen=True, slots=True)
class PlannerAgent:
    """Role definition for the Planner agent."""

    role_name: str = "_planner"
    display_name: str = "Planner"
    description: str = (
        "Performs initial alert analysis, initializes the TTT, and updates the TTT after each round."
    )
    responsibilities: tuple[str, ...] = (
        "Analyze the initial alert context and establish traceback goals.",
        "Output a complete TTT snapshot as the shared system blackboard.",
        "Update node status, structure, and next-step priorities from Reviewer round summaries.",
    )
    allowed_response_types: tuple[str, ...] = (
        "ROGER",
        "TTT_PLAN",
        "TTT_UPDATE",
    )
    system_prompt: str = PLANNER_SYSTEM_PROMPT


class PlannerRuntime:
    """Runtime implementation for the Planner role."""

    def __init__(
        self,
        *,
        storage: SQLiteStorage | None = None,
        ttt_store: TTTStore | None = None,
        bus: SQLiteMessageBus | None = None,
        procedural_memory: ProceduralMemoryLibrary | None = None,
        factual_memory: FactualMemoryLibrary | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.agent = PlannerAgent()
        self.storage = storage or SQLiteStorage()
        self.ttt_store = ttt_store or TTTStore()
        self.bus = bus or SQLiteMessageBus()
        self.procedural_memory = procedural_memory or ProceduralMemoryLibrary()
        self.factual_memory = factual_memory or FactualMemoryLibrary()
        self.poll_interval = poll_interval
        self.running = False

    def run_once(self) -> bool:
        did_work = False

        pending_events = self.storage.list_events_by_status(EventStatus.PENDING.value)
        for event in pending_events:
            self.process_initial_plan(event)
            did_work = True

        replan_events = self.storage.list_events_by_status(EventStatus.REPLANNING.value)
        for event in replan_events:
            self.process_replanning(event)
            did_work = True

        return did_work

    def run_forever(self) -> None:
        self.running = True
        logger.info("Planner runtime started")
        while self.running:
            try:
                did_work = self.run_once()
                if not did_work:
                    time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                logger.info("Planner runtime interrupted")
                self.running = False
            except Exception:
                logger.exception("Planner runtime loop failed")
                time.sleep(self.poll_interval)

    def stop(self) -> None:
        self.running = False

    def process_initial_plan(self, event: Event) -> Event:
        logger.info("Planner processing initial plan for event=%s", event.event_id)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.SYSTEM_INFO,
            payload={"text": "planner_start_initial_planning", "event_name": event.event_name},
        )

        try:
            analysis = self._analyze_event(event)
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.PLANNER_ANALYSIS_COMPLETED,
                payload={
                    "analysis": analysis,
                    "selected_procedural_memory": None,
                },
            )
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={"text": "planner_start_procedural_memory_lookup"},
            )
            selected_memory = self._select_procedural_memory(event)
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={
                    "text": "planner_procedural_memory_lookup_completed",
                    "selected_procedural_memory": selected_memory.summary_payload() if selected_memory else None,
                },
            )
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={"text": "planner_start_ttt_initialization"},
            )
            candidate_tree = self._generate_initial_ttt(
                event,
                selected_memory=selected_memory,
                factual_memories=self.factual_memory.list_documents(),
                available_tools=self._describe_available_tools(),
            )
        except Exception as exc:
            logger.exception("Planner initial TTT generation failed")
            return self._fail_event(event, f"Planner initial TTT generation failed: {exc}")
        snapshot = self.ttt_store.save_snapshot(candidate_tree)

        next_status = (
            EventStatus.PLANNED if self.ttt_store.has_open_work(event.event_id, snapshot.round_id) else EventStatus.COMPLETED
        )
        updated_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=next_status,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(updated_event)
        self._publish_overall_assessment_if_completed(
            event=updated_event,
            latest_ttt=snapshot,
        )
        self._publish(
            event_id=event.event_id,
            round_id=snapshot.round_id,
            message_type=MessageType.TTT_INITIALIZED,
            payload={"ttt": snapshot.to_dict(), "event_status": updated_event.event_status.value},
        )
        return updated_event

    def process_replanning(self, event: Event) -> Event:
        logger.info("Planner processing replanning for event=%s round=%s", event.event_id, event.current_round)
        latest_review = self.storage.get_round_review(event.event_id, event.current_round)
        latest_ttt = self.ttt_store.get_latest_ttt(event.event_id)
        executions = self.storage.list_executions(event.event_id, event.current_round)
        if latest_review is None or latest_ttt is None:
            logger.warning("Planner replanning skipped for event=%s due to missing review or ttt", event.event_id)
            return event

        next_round = event.current_round + 1
        try:
            self._publish(
                event_id=event.event_id,
                round_id=event.current_round,
                message_type=MessageType.SYSTEM_INFO,
                payload={
                    "text": "planner_start_ttt_replanning",
                    "review_round": latest_review.round_id,
                },
            )
            if self._should_reuse_existing_ttt(
                review=latest_review,
                executions=executions,
                latest_ttt=latest_ttt,
            ):
                candidate_tree = self._reuse_existing_ttt_for_next_round(
                    latest_ttt=latest_ttt,
                    next_round=next_round,
                )
            else:
                candidate_tree = self._generate_updated_ttt(
                    event=event,
                    latest_review=latest_review,
                    latest_ttt=latest_ttt,
                    executions=executions,
                    next_round=next_round,
                )
            candidate_tree = self._apply_l2_replan_policy(
                candidate_tree,
                previous_tree=latest_ttt,
            )
        except Exception as exc:
            logger.exception("Planner TTT update failed")
            fallback_tree = self._fallback_replanning_tree(
                event=event,
                latest_ttt=latest_ttt,
                next_round=next_round,
                error=exc,
            )
            if fallback_tree is None:
                return self._fail_event(event, f"Planner TTT update failed: {exc}")
            candidate_tree = fallback_tree
        snapshot = self.ttt_store.save_snapshot(candidate_tree)
        next_status = (
            EventStatus.PLANNED if self.ttt_store.has_open_work(event.event_id, snapshot.round_id) else EventStatus.COMPLETED
        )
        updated_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=next_status,
            current_round=next_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(updated_event)
        self._publish_overall_assessment_if_completed(
            event=updated_event,
            latest_ttt=snapshot,
        )
        self._publish(
            event_id=event.event_id,
            round_id=snapshot.round_id,
            message_type=MessageType.TTT_UPDATED,
            payload={"ttt": snapshot.to_dict(), "based_on_review": latest_review.to_dict()},
        )
        return updated_event

    def _analyze_event(self, event: Event) -> str:
        user_prompt = "\n".join(
            [
                "Perform an initial structured analysis of the security alert below and return only the analysis text itself.",
                "If helpful, you may use concise Markdown naturally; do not force a rigid format.",
                "Do not output YAML or JSON.",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            PLANNER_ANALYSIS_SYSTEM_PROMPT,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        analysis = (response_text or "").strip()
        if not analysis:
            raise ValueError("Planner analysis returned empty content")
        return analysis

    def _select_procedural_memory(
        self,
        event: Event,
    ) -> ProceduralMemoryDocument | None:
        documents = self.procedural_memory.list_documents()
        if not documents:
            return None

        user_prompt = "\n".join(
            [
                "Choose the single best matching procedural-memory summary for the current event from the candidates below.",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(
                    [document.summary_payload() for document in documents],
                    ensure_ascii=False,
                    indent=2,
                ),
            ]
        )
        parsed = parse_yaml_response(
            call_llm(
                PROCEDURAL_MEMORY_SELECTION_PROMPT,
                user_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
        )
        document_id = str((parsed or {}).get("document_id") or "").strip()
        if not document_id:
            return None
        return self.procedural_memory.get_document(document_id)

    def _generate_initial_ttt(
        self,
        event: Event,
        *,
        selected_memory: ProceduralMemoryDocument | None,
        factual_memories: list[FactualMemoryDocument],
        available_tools: list[dict[str, Any]],
    ) -> TracebackTaskTree:
        prompt_parts = [
            "Initialize the TTT for the security alert below and return YAML only.",
            "Understand the event first, then use the procedural memory, factual memory, and current MCP tool capabilities to build a better-fitting initial TTT.",
            "procedural memory provides workflow guidance; factual memory provides enterprise and data-environment background; MCP tools represent the currently executable capabilities, so L3 nodes should align closely with them.",
            "If the event has multiple clear investigation directions, split them into multiple L1 root nodes rather than forcing them into one generic L1.",
            "L3 nodes should name explicit entities whenever possible, such as concrete IPs, hostnames, or accounts, and should avoid vague phrasing like 'the IP', 'the host', or 'the account'.",
            json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
        ]
        if selected_memory is not None:
            prompt_parts.extend(
                [
                    "Matched procedural-memory document:",
                    json.dumps(selected_memory.summary_payload(), ensure_ascii=False, indent=2),
                    selected_memory.content,
                ]
            )
        if factual_memories:
            prompt_parts.extend(
                [
                    "Injected factual-memory documents:",
                    json.dumps(
                        [document.summary_payload() for document in factual_memories],
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "\n\n".join(
                        [
                            f"## {document.title}\n{document.content}"
                            for document in factual_memories
                        ]
                    ),
                ]
            )
        if available_tools:
            prompt_parts.extend(
                [
                    "Currently available MCP servers and tools:",
                    json.dumps(available_tools, ensure_ascii=False, indent=2),
                ]
            )
        user_prompt = "\n".join(prompt_parts)
        ttt_payload = self._request_ttt_payload(user_prompt)
        entity_context = self._extract_entity_context(event=event)
        return self._normalize_ttt_payload(
            event_id=event.event_id,
            round_id=event.current_round,
            ttt_payload=ttt_payload,
            metadata={
                "selected_procedural_memory": selected_memory.summary_payload() if selected_memory else None,
                "factual_memories": [document.summary_payload() for document in factual_memories],
                "available_mcp_tools": available_tools,
            },
            entity_context=entity_context,
            apply_l2_policy=False,
        )

    def _generate_updated_ttt(
        self,
        *,
        event: Event,
        latest_review: RoundReview,
        latest_ttt: TracebackTaskTree,
        executions: list[Any],
        next_round: int,
    ) -> TracebackTaskTree:
        entity_context = self._extract_entity_context(event=event, analysis=latest_review.summary_text)
        replanning_policy = self._build_replanning_policy_hint(
            review=latest_review,
            executions=executions,
        )
        user_prompt = "\n".join(
            [
                "Update the next-round TTT from the event, previous-round TTT, and Reviewer summary below, and return YAML only.",
                replanning_policy,
                "If the event contains multiple clear investigation directions, preserve or split them into multiple L1 root nodes as needed.",
                "Nodes already marked done must remain done; do not revert them to todo or in_progress in the new round.",
                "L3 nodes should use explicit entities whenever possible. If the input already includes an IP, hostname, or account, do not rewrite it as 'the IP', 'the host', or 'the account'.",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps([execution.to_dict() for execution in executions], ensure_ascii=False, indent=2),
                json.dumps(latest_review.to_dict(), ensure_ascii=False, indent=2),
            ]
        )
        ttt_payload = self._request_ttt_payload(user_prompt)
        return self._normalize_ttt_payload(
            event_id=event.event_id,
            round_id=next_round,
            ttt_payload=ttt_payload,
            entity_context=entity_context,
            previous_tree=latest_ttt,
            apply_l2_policy=False,
        )

    def _request_ttt_payload(self, user_prompt: str) -> dict[str, Any]:
        attempts = (
            user_prompt,
            "\n\n".join([user_prompt, TTT_RETRY_CORRECTION_PROMPT]),
        )
        last_error = "Planner returned empty or non-YAML content"
        for attempt_prompt in attempts:
            response_text = call_llm(
                self.agent.system_prompt,
                attempt_prompt,
                extra_body={"thinking": {"type": "enabled"}},
            )
            parsed = parse_yaml_response(response_text)
            if not parsed:
                last_error = "Planner returned empty or non-YAML content"
                continue
            ttt_payload = parsed.get("ttt") or parsed.get("tree")
            if isinstance(ttt_payload, dict):
                return ttt_payload
            last_error = "Planner response missing `ttt`/`tree` object"
        raise ValueError(last_error)

    def _publish_overall_assessment_if_completed(
        self,
        *,
        event: Event,
        latest_ttt: TracebackTaskTree,
    ) -> None:
        if event.event_status != EventStatus.COMPLETED:
            return
        try:
            summary_text = self._generate_overall_assessment(event=event, latest_ttt=latest_ttt)
        except Exception:
            logger.exception("Planner overall assessment generation failed")
            return
        self._publish(
            event_id=event.event_id,
            round_id=latest_ttt.round_id,
            message_type=MessageType.OVERALL_ASSESSMENT_CREATED,
            payload={"summary_text": summary_text},
        )

    def _generate_overall_assessment(
        self,
        *,
        event: Event,
        latest_ttt: TracebackTaskTree,
    ) -> str:
        executions = self.storage.list_executions(event.event_id)
        round_reviews: list[RoundReview] = []
        for round_id in range(1, latest_ttt.round_id + 1):
            review = self.storage.get_round_review(event.event_id, round_id)
            if review is not None:
                round_reviews.append(review)
        user_prompt = "\n".join(
            [
                "Based on the complete traceback process below, output an overall event assessment.",
                json.dumps(event.to_dict(), ensure_ascii=False, indent=2),
                json.dumps(latest_ttt.to_dict(), ensure_ascii=False, indent=2),
                json.dumps([execution.to_dict() for execution in executions], ensure_ascii=False, indent=2),
                json.dumps([review.to_dict() for review in round_reviews], ensure_ascii=False, indent=2),
            ]
        )
        response_text = call_llm(
            OVERALL_ASSESSMENT_PROMPT,
            user_prompt,
            extra_body={"thinking": {"type": "enabled"}},
        )
        summary_text = (response_text or "").strip()
        if not summary_text:
            raise ValueError("Planner overall assessment returned empty content")
        return summary_text

    def _describe_available_tools(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in list_registered_tools():
            grouped.setdefault(tool.server_name, []).append(
                {
                    "server_name": tool.server_name,
                    "name": tool.name,
                    "description": tool.description,
                    "when_to_use": tool.when_to_use,
                    "limitations": tool.limitations,
                }
            )
        return [
            {"server_name": server_name, "tools": tools}
            for server_name, tools in sorted(grouped.items())
        ]

    def _build_replanning_policy_hint(
        self,
        *,
        review: RoundReview,
        executions: list[Any],
    ) -> str:
        if not executions:
            return "This round has no execution records. You may supplement the next-round TTT from the review, but it must still keep a three-level structure and complete entities."

        successes = [
            execution
            for execution in executions
            if str(getattr(execution, "execution_status", "")).lower() == "completed"
        ]
        has_failures = len(successes) != len(executions)
        review_text = (review.summary_text or "").strip()
        if successes and not has_failures:
            return "\n".join(
                [
                    "Replanning policy hint: this round executed successfully overall.",
                    "If the review does not explicitly raise new key evidence, new gaps, or a new investigation direction, update only node status and the minimum necessary structure rather than adding, reordering, or heavily rewriting the TTT.",
                    "If the current step executed successfully and returned the needed result, avoid changing the TTT and continue down the remaining nodes of the current tree.",
                    f"Current review text: {review_text}",
                ]
            )
        return "\n".join(
            [
                "Replanning policy hint: this round contains failed executions, blockers, or unfinished items. You may add new questions, L3 nodes, or new L1 directions from the review, but still keep the change set minimal.",
                f"Current review text: {review_text}",
            ]
        )

    def _should_reuse_existing_ttt(
        self,
        *,
        review: RoundReview,
        executions: list[Any],
        latest_ttt: TracebackTaskTree,
    ) -> bool:
        if not executions:
            return False
        if not self.ttt_store.has_open_work(latest_ttt.event_id, latest_ttt.round_id):
            return False

        successful_with_results = [
            execution
            for execution in executions
            if str(getattr(execution, "execution_status", "")).lower() == "completed"
            and bool(getattr(execution, "result", None))
            and not bool(getattr(execution, "result", {}).get("no_data_found"))
        ]
        if len(successful_with_results) != len(executions):
            return False

        review_text = (review.summary_text or "").lower()
        change_indicators = (
            "new direction",
            "added direction",
            "new investigation direction",
            "expand",
            "pivot",
            "new question",
            "new l1",
            "new l2",
            "new l3",
            "supplemental new question",
            "new key evidence",
        )
        return not any(indicator in review_text for indicator in change_indicators)

    def _reuse_existing_ttt_for_next_round(
        self,
        *,
        latest_ttt: TracebackTaskTree,
        next_round: int,
    ) -> TracebackTaskTree:
        metadata = dict(latest_ttt.metadata or {})
        metadata["planner_replanning_strategy"] = "reuse_existing_ttt"
        return TracebackTaskTree(
            event_id=latest_ttt.event_id,
            round_id=next_round,
            root_nodes=tuple(self._reset_reusable_node_statuses(node) for node in latest_ttt.root_nodes),
            metadata=metadata,
            created_at=latest_ttt.created_at,
            updated_at=utc_now(),
        )

    def _fallback_replanning_tree(
        self,
        *,
        event: Event,
        latest_ttt: TracebackTaskTree,
        next_round: int,
        error: Exception,
    ) -> TracebackTaskTree | None:
        error_text = str(error or "").strip()
        if not error_text:
            return None
        lowered_error = error_text.lower()
        fallback_indicators = (
            "non-yaml",
            "missing `ttt`/`tree` object",
            "timeout",
            "timed out",
            "readtimeout",
            "connecttimeout",
            "connection timed out",
        )
        if not any(indicator in lowered_error for indicator in fallback_indicators):
            return None

        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.SYSTEM_WARNING,
            payload={
                "text": "planner_ttt_replanning_fallback_reuse_existing_tree",
                "reason": error_text,
            },
        )
        return self._apply_l2_replan_policy(
            self._reuse_existing_ttt_for_next_round(
                latest_ttt=latest_ttt,
                next_round=next_round,
            ),
            previous_tree=latest_ttt,
        )

    def _reset_reusable_node_statuses(self, node: TTTNode) -> TTTNode:
        # Cross-round reuse must not carry old in_progress markers forward,
        # otherwise the next round becomes planned-without-claimable-work.
        reset_status = TTTNodeStatus.TODO if node.status == TTTNodeStatus.IN_PROGRESS else node.status
        return TTTNode(
            node_id=node.node_id,
            title=node.title,
            status=reset_status,
            children=tuple(self._reset_reusable_node_statuses(child) for child in node.children),
            metadata=dict(node.metadata or {}),
        )

    def _extract_entity_context(self, *, event: Event, analysis: str | None = None) -> dict[str, str]:
        text_parts = [
            event.event_name,
            event.message,
            json.dumps(event.context or {}, ensure_ascii=False),
        ]
        if analysis:
            text_parts.append(analysis)
        combined = "\n".join(part for part in text_parts if part)

        ip_matches = []
        seen_ips: set[str] = set()
        for match in IP_PATTERN.findall(combined):
            if match not in seen_ips:
                seen_ips.add(match)
                ip_matches.append(match)

        host_candidates = self._collect_named_values(
            event.context,
            keys=("host", "hostname", "server", "dest", "dest_host", "target_host", "asset"),
        )
        if not host_candidates:
            host_candidates = [
                token
                for token in HOSTLIKE_PATTERN.findall(combined)
                if any(sep in token for sep in ("-", "_", ".")) and not IP_PATTERN.fullmatch(token)
            ]

        account_candidates = self._collect_named_values(
            event.context,
            keys=("user", "username", "account", "account_name"),
        )
        if not account_candidates:
            account_candidates = [
                token
                for token in ACCOUNTLIKE_PATTERN.findall(combined)
                if "@" in token or token.lower().startswith(("user", "svc_", "adm_", "admin"))
            ]

        context: dict[str, str] = {}
        if ip_matches:
            context["ip"] = ip_matches[0]
        if host_candidates:
            context["host"] = host_candidates[0]
        if account_candidates:
            context["account"] = account_candidates[0]
        return context

    def _collect_named_values(self, context: dict[str, Any], *, keys: tuple[str, ...]) -> list[str]:
        values: list[str] = []
        if not isinstance(context, dict):
            return values
        for key in keys:
            value = context.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text and text not in values:
                values.append(text)
        return values

    def _normalize_leaf_title(self, title: str, entity_context: dict[str, str]) -> str:
        normalized = title.strip()
        ip_value = entity_context.get("ip")
        host_value = entity_context.get("host")
        account_value = entity_context.get("account")

        replacements = [
            ("the IP", ip_value),
            ("this IP", ip_value),
            ("source IP", ip_value),
            ("the host", host_value),
            ("target host", host_value),
            ("the account", account_value),
            ("the user", account_value),
        ]
        for needle, replacement in replacements:
            if replacement and needle in normalized:
                normalized = normalized.replace(needle, replacement)

        if ip_value and "IP" in normalized and ip_value not in normalized and "from the alert" not in normalized:
            normalized = normalized.replace("IP", f"IP {ip_value}", 1)
        if host_value and "host" in normalized and host_value not in normalized and "from the alert" not in normalized:
            normalized = normalized.replace("host", f"host {host_value}", 1)
        if account_value and ("account" in normalized or "user" in normalized) and account_value not in normalized and "from the alert" not in normalized:
            if "account" in normalized:
                normalized = normalized.replace("account", f"account {account_value}", 1)
            else:
                normalized = normalized.replace("user", f"user {account_value}", 1)

        fallback_replacements = {
            "the IP": "source IP from the alert",
            "this IP": "source IP from the alert",
            "source IP": "source IP from the alert",
            "the host": "related host from the alert",
            "target host": "target host from the alert",
            "the account": "related account from the alert",
            "the user": "related user from the alert",
        }
        for needle, replacement in fallback_replacements.items():
            if needle in normalized:
                normalized = normalized.replace(needle, replacement)
        return normalized

    def _normalize_ttt_payload(
        self,
        *,
        event_id: str,
        round_id: int,
        ttt_payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        entity_context: dict[str, str] | None = None,
        previous_tree: TracebackTaskTree | None = None,
        apply_l2_policy: bool = True,
    ) -> TracebackTaskTree:
        roots = ttt_payload.get("root_nodes") or ttt_payload.get("nodes") or []
        resolved_entity_context = entity_context or {}
        frozen_done_nodes = self._collect_frozen_done_nodes(previous_tree)
        normalized_roots = tuple(
            self._normalize_node(
                node,
                path=str(index),
                entity_context=resolved_entity_context,
                frozen_done_nodes=frozen_done_nodes,
            )
            for index, node in enumerate(roots, start=1)
        )
        tree = TracebackTaskTree(
            event_id=event_id,
            round_id=round_id,
            root_nodes=normalized_roots,
            metadata=dict(metadata or ttt_payload.get("metadata") or {}),
        )
        if previous_tree is not None:
            tree = self._enforce_linear_progression_statuses(tree, previous_tree=previous_tree)
        if apply_l2_policy:
            tree = self._apply_l2_replan_policy(tree, previous_tree=previous_tree)
        return tree

    def _normalize_node(
        self,
        node: dict[str, Any],
        *,
        path: str,
        entity_context: dict[str, str],
        frozen_done_nodes: set[tuple[str, str]],
    ) -> TTTNode:
        title = str(node.get("title") or path)
        children_raw = node.get("children") or []
        if not children_raw:
            title = self._normalize_leaf_title(title, entity_context)
        status = coerce_ttt_node_status(node.get("status"))
        if (path, title) in frozen_done_nodes or ("", title) in frozen_done_nodes:
            status = TTTNodeStatus.DONE
        children = tuple(
            self._normalize_node(
                child,
                path=f"{path}-{child_index}",
                entity_context=entity_context,
                frozen_done_nodes=frozen_done_nodes,
            )
            for child_index, child in enumerate(children_raw, start=1)
        )
        return TTTNode(
            node_id=path,
            title=title,
            status=status,
            children=children,
            metadata=dict(node.get("metadata") or {}),
        )

    def _collect_frozen_done_nodes(
        self,
        previous_tree: TracebackTaskTree | None,
    ) -> set[tuple[str, str]]:
        if previous_tree is None:
            return set()

        frozen: set[tuple[str, str]] = set()

        def walk(node: TTTNode) -> None:
            if node.status == TTTNodeStatus.DONE:
                frozen.add((node.node_id, node.title))
                frozen.add(("", node.title))
            for child in node.children:
                walk(child)

        for root in previous_tree.root_nodes:
            walk(root)
        return frozen

    def _enforce_linear_progression_statuses(
        self,
        tree: TracebackTaskTree,
        *,
        previous_tree: TracebackTaskTree,
    ) -> TracebackTaskTree:
        previous_nodes_by_id: dict[str, TTTNode] = {}
        previous_nodes_by_title: dict[str, TTTNode] = {}

        def collect(node: TTTNode) -> None:
            previous_nodes_by_id[node.node_id] = node
            previous_nodes_by_title[node.title] = node
            for child in node.children:
                collect(child)

        for root in previous_tree.root_nodes:
            collect(root)

        def transform(node: TTTNode) -> TTTNode:
            children = tuple(transform(child) for child in node.children)
            metadata = dict(node.metadata or {})
            previous_node = previous_nodes_by_id.get(node.node_id) or previous_nodes_by_title.get(node.title)
            explicit_skip = bool(metadata.get("skip_reason"))
            status = node.status

            if explicit_skip:
                status = TTTNodeStatus.DONE if status == TTTNodeStatus.DONE else TTTNodeStatus.NOT_APPLICABLE
            elif previous_node is not None and previous_node.status in {TTTNodeStatus.DONE, TTTNodeStatus.NOT_APPLICABLE}:
                status = previous_node.status
            else:
                status = TTTNodeStatus.TODO

            return TTTNode(
                node_id=node.node_id,
                title=node.title,
                status=status,
                children=children,
                metadata=metadata,
            )

        return TracebackTaskTree(
            event_id=tree.event_id,
            round_id=tree.round_id,
            root_nodes=tuple(transform(root) for root in tree.root_nodes),
            metadata=dict(tree.metadata or {}),
            created_at=tree.created_at,
            updated_at=tree.updated_at,
        )

    def _apply_l2_replan_policy(
        self,
        tree: TracebackTaskTree,
        *,
        previous_tree: TracebackTaskTree | None,
    ) -> TracebackTaskTree:
        previous_l2_by_node_id: dict[str, TTTNode] = {}
        previous_l2_by_title: dict[str, TTTNode] = {}

        if previous_tree is not None:
            for previous_l2 in self._iter_l2_nodes(previous_tree.root_nodes):
                previous_l2_by_node_id[previous_l2.node_id] = previous_l2
                previous_l2_by_title[previous_l2.title] = previous_l2

        def transform(node: TTTNode) -> TTTNode:
            children = tuple(transform(child) for child in node.children)
            metadata = dict(node.metadata or {})
            status = node.status

            if self._node_depth(node.node_id) == 2:
                previous_l2 = previous_l2_by_node_id.get(node.node_id) or previous_l2_by_title.get(node.title)
                previous_count = 0
                if previous_l2 is not None:
                    previous_count = int((previous_l2.metadata or {}).get("replan_attempt_count") or 0)
                attempt_count = previous_count
                if previous_tree is not None:
                    if previous_l2 is None:
                        attempt_count = 1
                    elif self._l2_subtree_signature(node) != self._l2_subtree_signature(previous_l2):
                        attempt_count = previous_count + 1
                metadata["replan_attempt_count"] = attempt_count
                if attempt_count > MAX_L2_REPLAN_ATTEMPTS:
                    metadata["skip_reason"] = "l2_replan_cap_reached"
                    status = TTTNodeStatus.NOT_APPLICABLE
                    children = tuple(self._skip_subtree(child) for child in children)

            return TTTNode(
                node_id=node.node_id,
                title=node.title,
                status=status,
                children=children,
                metadata=metadata,
            )

        return TracebackTaskTree(
            event_id=tree.event_id,
            round_id=tree.round_id,
            root_nodes=tuple(transform(root) for root in tree.root_nodes),
            metadata=dict(tree.metadata or {}),
            created_at=tree.created_at,
            updated_at=tree.updated_at,
        )

    def _l2_subtree_signature(self, node: TTTNode) -> tuple[Any, ...]:
        return (
            node.title,
            tuple(self._l2_subtree_signature(child) for child in node.children),
        )

    def _skip_subtree(self, node: TTTNode) -> TTTNode:
        children = tuple(self._skip_subtree(child) for child in node.children)
        metadata = dict(node.metadata or {})
        metadata["skip_reason"] = "l2_replan_cap_reached"
        status = node.status if node.status == TTTNodeStatus.DONE else TTTNodeStatus.NOT_APPLICABLE
        return TTTNode(
            node_id=node.node_id,
            title=node.title,
            status=status,
            children=children,
            metadata=metadata,
        )

    @staticmethod
    def _node_depth(node_id: str) -> int:
        return len([part for part in str(node_id).split("-") if part])

    def _iter_l2_nodes(self, nodes: tuple[TTTNode, ...]) -> list[TTTNode]:
        l2_nodes: list[TTTNode] = []

        def walk(node: TTTNode) -> None:
            if self._node_depth(node.node_id) == 2:
                l2_nodes.append(node)
            for child in node.children:
                walk(child)

        for root in nodes:
            walk(root)
        return l2_nodes

    def _fail_event(self, event: Event, reason: str) -> Event:
        failed_event = Event(
            event_id=event.event_id,
            event_name=event.event_name,
            message=event.message,
            context=event.context,
            source=event.source,
            severity=event.severity,
            event_status=EventStatus.FAILED,
            current_round=event.current_round,
            created_at=event.created_at,
            updated_at=utc_now(),
        )
        self.storage.save_event(failed_event)
        self._publish(
            event_id=event.event_id,
            round_id=event.current_round,
            message_type=MessageType.SYSTEM_ERROR,
            payload={"text": reason},
        )
        return failed_event

    def _publish(
        self,
        *,
        event_id: str,
        round_id: int,
        message_type: MessageType,
        payload: dict[str, Any],
        to_role: RoleName | None = None,
    ) -> None:
        self.bus.publish(
            MessageEnvelope(
                event_id=event_id,
                round_id=round_id,
                from_role=RoleName.PLANNER,
                to_role=to_role,
                message_type=message_type,
                payload=payload,
            )
        )


def run_planner(poll_interval: float = 5.0) -> None:
    PlannerRuntime(poll_interval=poll_interval).run_forever()


__all__ = [
    "PLANNER_SYSTEM_PROMPT",
    "PlannerAgent",
    "PlannerRuntime",
    "run_planner",
]
