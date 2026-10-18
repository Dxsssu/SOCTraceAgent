"""Bounded, no-LLM retrieval of typed evidence pivots for Planner only.

Search backwards from requested evidence to presently observed input types.
Intermediate outputs are marked unavailable until a real query provides them.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import ipaddress
import json
from pathlib import Path
import re

from src.schema.investigation import task_input
from .recovery import time_scope, alert_device_scope, url_form_scope, parent_role_scope

DEFAULT_CATALOG = Path(__file__).with_name("catalog.json")
_FIELD_TYPES = {
    "accountname": "account_name",
    "accountdisplayname": "account_display_name", "userdisplayname": "account_display_name", "publicip": "public_ip",
    "initiatingprocessfilename": "parent_filename",
    "processid": "process_id", "threatfamily": "malware_family",
    "networkmessageid": "message_id", "alertid": "alert_id", "url": "url", "remoteurl": "url",
    "senderfromaddress": "sender", "recipientemailaddress": "recipient",
    "ipaddress": "ip", "remoteip": "remote_ip", "localip": "ip", "senderipv4": "ip",
    "filename": "filename", "userid": "user_id", "accountobjectid": "account_object_id", "accountsid": "account_sid", "accountupn": "account_upn",
    "userprincipalname": "account_upn", "processcommandline": "command",
    "internetmessageid": "internet_message_id", "devicename": "device_name", "deviceid": "device_id", "aaddeviceid": "aad_device_id",
}


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def known_inputs(task, facts=()):
    # Initial user-supplied values are known inputs, not proven investigation
    # conclusions. Plans, failed observations and previous memory are excluded.
    initial = _text(task.get("context", "")) + "\n" + _text(task.get("question", ""))
    known = {}
    patterns = {
        "process_id": r"\b(?:ProcessId|PID)\b[\s:=`\"']+(\d+)\b",
        "account_sid": r"\bS-\d+(?:-\d+){1,}\b",
        "user_id": r"\bUserId\b[\s:=`\"']+([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
        "device_id": r"\bDeviceId\b[\s:=`\"']+([0-9a-f]{40}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
        "aad_device_id": r"\bAadDeviceId\b[\s:=`\"']+([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})",
        "device_name": r"\b(?:DeviceName|host|device|computer)\s*[:=]?\s*[`\"']([a-zA-Z0-9.-]+)[`\"']",
        "url": r"https?://[^\s`<>\"']+",
        "filename": r"\b[\w.-]+\.(?:exe|dll)\b",
        "message_id": r"\bNetworkMessageId\b[\s:=`\"']+([a-zA-Z0-9-]{12,})",
        "alert_id": r"\bAlertId\b[\s:=`\"']+([a-zA-Z0-9_-]{8,})",
        "internet_message_id": r"<[A-Za-z0-9._-]+@[A-Za-z0-9.-]+>",
        "command_fragment": r"\b[\w.-]+\.(?:ps1|bat|cmd|vbs)\b",
        "file_artifact": r"\b[\w.-]+\.(?:exe|dll|ps1|zip|lockbit)\b",
        "email_address": r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b",
    }
    for kind, pattern in patterns.items():
        if re.search(pattern, initial, re.I):
            known[kind] = ["initial_input"]
    for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", initial):
        try:
            ipaddress.ip_address(candidate)
            known["ip"] = ["initial_input"]
        except ValueError:
            pass
    if "email_address" in known:
        known["account_upn"] = ["initial_input_email_candidate"]
    if re.search(r"(?:remote\s*ip|远端\s*IP)[^\n]{0,30}(?:\d{1,3}\.){3}\d{1,3}", initial, re.I):
        known["remote_ip"] = ["initial_input_explicit_remote_role"]
    if re.search(r"(?:risk|risky|anonymous).{0,30}(?:sign.in|login)|风险登录|匿名登录", initial, re.I):
        known["risk_topic"] = ["initial_input_topic"]
    if re.search(r"(?:malicious|phishing).{0,20}(?:email|mail)|emails?.{0,35}malicious|恶意邮件|钓鱼邮件", initial, re.I):
        known["mail_threat_topic"] = ["initial_input_topic"]
    if re.search(r"\bLDAP\b|directory query|目录查询", initial, re.I):
        known["directory_query"] = ["initial_input_topic"]
    if re.search(r"\bDKM\b|\bADFS\b|directory (?:operation|activity)|目录操作", initial, re.I):
        known["directory_action"] = ["initial_input_topic"]
    public = re.search(r"(?:public\s*ip|公网\s*IP)[\s:=`\"']+((?:\d{1,3}\.){3}\d{1,3})", initial, re.I)
    if public:
        try:
            ipaddress.ip_address(public.group(1))
            known["public_ip"] = ["initial_input_explicit_public_role"]
        except ValueError:
            pass
    if re.search(r"inbox.{0,20}rule|mailbox.{0,20}rule|收件箱规则", initial, re.I):
        known["inbox_rule"] = ["initial_input"]
    if re.search(r"quarantin|\bzap\b|removed|隔离|移除|投递后", initial, re.I):
        known["delivery_action"] = ["initial_input"]
    if re.search(r"\balerts?\b|\bdetected\b|\battack\b|\bincident\b|\bsuspicious\b|告警|检测|攻击|事件", initial, re.I):
        known["alert_topic"] = ["initial_input"]
    for source, record in facts:
        # ReportId is table-local. A bare ReportId (even next to a URL) does
        # not establish which producer emitted it.
        if (record.get("Type") == "UrlClickEvents" or record.get("SourceTable") == "UrlClickEvents") and record.get("ReportId"):
            known.setdefault("url_click_report_id", []).append(source)
        for field, value in record.items():
            kind = _FIELD_TYPES.get(str(field).lower().replace("_", ""))
            if kind and value is not None and str(value).strip():
                known.setdefault(kind, []).append(source)
                if kind == "remote_ip":
                    known.setdefault("ip", []).append(source)
    return known


def target_types(question):
    question = str(question)
    # Focus on the final interrogative instead of incidental entities in a long background.
    # Relative clauses ("the URL which was removed") are not new questions.
    starts = list(re.finditer(r"(?:^|[.!?]\s+)(?:what|which|who|when|can you|could you|identify|determine|provide)\b|请问|请找", question, re.I))
    focus = question[starts[-1].start():] if starts else question
    patterns = {
        "sender": r"sender|sending ip|sent (?:the|these)|发件|发送者",
        "recipient": r"recipient|receiver|收件",
        "command": r"command.?line|command was|what command|命令行|执行命令",
        "click": r"who.*click|which.*(?:account|user).*click|点击者|谁.*点击",
        "account": r"aad.?user.?id|user id|account.*(?:id|name)|name of (?:the |a )?account|email of (?:the |a )?user|which user|who was|\bsid\b|security identifier|用户标识|用户 ID|账户",
        "device": r"which (?:host|device|computer)|(?:host|device|computer) ?name|aaddeviceid|主机名|设备标识",
        "process": r"process.?id|process identifier|created.?time(?:utc)?|进程标识|进程 ID",
        "application": r"(?:which|what) application|application (?:id|name)|\bapp id\b|应用标识|应用名称",
        "file_artifact": r"file ?name|(?:name|path) of (?:the |a )?(?:[\w-]+\s+){0,5}(?:file|script|process)\b|(?:which|what) file|文件(?:名|路径)",
        "ip": r"\bip address\b|\bIP\b|IP地址",
        "url": r"\burl\b|\blink\b|website|网址|链接",
    }
    targets = {k for k, regex in patterns.items() if re.search(regex, focus, re.I)}
    if re.search(r"aaddeviceid|AAD device (?:id|identifier)", focus, re.I):
        targets = {"aad_device_id"}
    elif re.search(r"registry|注册表", focus, re.I) and not re.search(r"(?:what|which|identify).{0,30}(?:ip address|account|SID)", focus, re.I):
        targets = {"registry"}
    elif re.search(r"\bsid\b|security identifier|安全标识符", focus, re.I):
        targets = {"account_sid"}
    elif "process" in targets:
        targets -= {"application"}
    if "sender" in targets or "recipient" in targets:
        targets -= {"url", "account", "ip"}
    if "click" in targets:
        targets -= {"account", "url"}
    if re.search(r"ip|address|地址", focus, re.I) and re.search(r"sign.in|login|accessed|登录", question, re.I) and "sender" not in targets:
        targets = {"signin_ip"}
    if re.search(r"\bip\b|地址", focus, re.I) and re.search(r"inbox.{0,20}rule|mailbox.{0,20}rule|收件箱规则", question, re.I):
        targets = {"audit_ip"}
    elif re.search(r"\bip\b|地址", focus, re.I) and re.search(r"risk|风险", question, re.I):
        targets = {"risk_ip"}
    elif re.search(r"risk (?:event|type|level)|风险(?:类型|等级|事件)", focus, re.I):
        targets = {"risk"}
    if re.search(r"incident (?:id|number)|事件编号", focus, re.I):
        targets = {"incident_record"}
    elif "sender" not in targets and re.search(r"(?:what|which).{0,25}(?:post.delivery action|delivery action)|投递后.*(?:动作|处理记录)", focus, re.I):
        targets = {"delivery_record"}
    elif not (targets & {"command", "process", "account", "sender"}) and re.search(r"network (?:connections?|activity)|网络连接|网络活动", focus, re.I):
        targets = {"network"}
    return targets or {"unclassified"}


class ProcedureCardMemory:
    def __init__(self, path=DEFAULT_CATALOG, *, max_cards=3, max_bytes=8000, max_depth=3):
        content = Path(path).read_bytes()
        self.catalog = json.loads(content)
        if self.catalog.get("format") != "evidence-pivot-cards-1":
            raise ValueError("Unsupported procedure-card catalog")
        self.fingerprint = hashlib.sha256(content).hexdigest()
        self.cards = self.catalog["cards"]
        self.max_cards, self.max_bytes, self.max_depth = max_cards, max_bytes, max_depth

    def retrieve(self, task, *, facts=(), available_schema=None, latest_execution=None):
        known = known_inputs(task, facts)
        targets = target_types(task.get("question", ""))
        cards = self.cards
        if available_schema is not None:
            cards = {key: card for key, card in cards.items()
                     if all(set(fields) <= set(available_schema.get(table, ()))
                            for table, fields in card["dependencies"].items())}
        plans = []

        def solve(target, visited, depth):
            if target in known:
                return [[]]
            if depth == 0 or target in visited:
                return []
            options = []
            for key, entry in cards.items():
                if target not in entry["produces"]:
                    continue
                paths = [[]]
                for needed in entry["requires"]:
                    upstream = solve(needed, visited | {target}, depth-1)
                    paths = [list(dict.fromkeys(a+b)) for a in paths for b in upstream]
                options.extend(list(dict.fromkeys(path+[key])) for path in paths)
            return options

        # A known URL in the background may differ from the requested URL.
        # Always seek an operation producing the requested evidence, not simply
        # answer from type-level input availability.
        for target in targets:
            for key, entry in cards.items():
                if target not in entry["produces"]:
                    continue
                paths = [[]]
                for needed in entry["requires"]:
                    upstream = solve(needed, {target}, self.max_depth-1)
                    paths = [list(dict.fromkeys(a+b)) for a in paths for b in upstream]
                for path in paths:
                    route = list(dict.fromkeys(path+[key]))
                    relevance = sum(any(term.lower() in str(task.get("question", "")).lower() for term in cards[k]["intents"]) for k in route)
                    # Prefer an observed identifier/URL over broad activity
                    # keywords; otherwise "removed" outranks the exact URL
                    # merely because it repeats in the question.
                    first_inputs = set(cards[route[0]]["requires"])
                    breadth = 2 * bool(first_inputs & {"delivery_action", "alert_topic", "risk_topic", "mail_threat_topic", "directory_query", "directory_action", "inbox_rule"})
                    breadth += bool(first_inputs & {"email_address"})
                    # Host-wide enumeration is broader than an available
                    # host + PID lookup, even when both take a single query.
                    breadth += "device_to_process_events" in route
                    # A SID requested for the clicking account must traverse
                    # click evidence, not merely a shorter unrelated IP login.
                    click_identity = "account_sid" in targets and bool(re.search(
                        r"click|accessed.{0,20}(?:link|url)|点击|访问.*链接", str(task.get("question", "")), re.I))
                    if click_identity and not facts and not set(route) & {"url_to_click", "account_to_click", "ip_to_click_events"}:
                        continue
                    plans.append((breadth, len(route), -relevance, tuple(route)))
        # If no typed answer route exists, provide a clearly labelled discovery
        # route. Generic alert entities must NOT masquerade as a UPN/IP/etc.
        # Downstream input types are still unlocked only by observed fields.
        retrieval_purpose = "answer_route"
        # A filename alone does not establish membership in the requested alert.
        # Resolve the explicit alert scope before taking a short attribute path.
        question = str(task.get("question", ""))
        scoped_target = bool(targets & {"process", "command", "device", "aad_device_id", "account_sid", "account", "ip", "file_artifact"})
        context = _text(task.get("context", ""))
        transition = bool(re.search(r"shortly after|followed by|another (?:alert|event|incident)|subsequently|此外|随后|另一(?:个|条).*告警", context, re.I))
        question_known = known_inputs({"question": question})
        topic_types = {"alert_topic", "delivery_action", "risk_topic", "mail_threat_topic", "directory_query", "directory_action", "inbox_rule"}
        has_question_identifier = bool(set(question_known) - topic_types)
        needs_event_scope = transition and scoped_target and not has_question_identifier
        needs_alert_scope = scoped_target and (bool(re.search(r"\balert\b|告警", question, re.I)) or needs_event_scope) and not facts
        if needs_alert_scope and ("alert_topic" in known or "alert_id" in known):
            discovery = solve("alert_entity", set(), self.max_depth)
            def target_support(entry):
                fields = {field for values in entry["dependencies"].values() for field in values}
                hints = set(entry["produces"])
                mapping = {"ProcessId": "process", "ProcessCommandLine": "command", "AccountSid": "account_sid",
                           "AccountName": "account", "DeviceName": "device", "AadDeviceId": "aad_device_id"}
                hints.update(kind for field, kind in mapping.items() if field in fields)
                if "Entities" in fields:
                    hints.update({"process", "command", "account", "account_sid", "device"})
                return bool(hints & targets)
            scope_plans = [(0 if target_support(cards[route[-1]]) else 1, len(route), 0, tuple(route)) for route in discovery
                           if route and set(cards[route[0]]["requires"]) & {"alert_topic", "alert_id"}]
            if scope_plans:
                plans = scope_plans
                retrieval_purpose = "scope_discovery"
        if not plans and ("alert_topic" in known or "alert_id" in known):
            discovery = solve("alert_entity", set(), self.max_depth)
            for route in discovery:
                if route:
                    plans.append((2, len(route), 0, tuple(route)))
            retrieval_purpose = "candidate_discovery"
        selected = []
        for _, _, _, route in sorted(set(plans)):
            if len(route) <= self.max_cards:
                selected = list(route)
                break
        result = []
        if (self.max_cards >= 1 and latest_execution
                and latest_execution.get("query_success") is True
                and latest_execution.get("result_status") == "empty_result"
                and latest_execution.get("sql")):
            for trigger, detector, required in (("empty_time_scope", time_scope, "temporal"),
                                                ("empty_alert_device_scope", alert_device_scope, "device"),
                                                ("empty_url_form", url_form_scope, "hostname"),
                                                ("empty_parent_role", parent_role_scope, "value")):
                recovery_id = trigger + "_diagnostic"
                scope = detector(latest_execution["sql"])
                if recovery_id in cards and scope and scope[required]:
                    selected = [recovery_id]
                    retrieval_purpose = "scope_recovery"
                    known[trigger] = [str(latest_execution.get("execution_id", "latest_execution"))]
                    break
        for key in selected:
            entry = cards[key]
            missing = sorted(set(entry["requires"]) - known.keys())
            item = {"id": key, "readiness": "needs_prior_observation" if missing else "input_type_available",
                    "missing_inputs": missing, "purpose": retrieval_purpose, **entry["card"]}
            result.append(item)
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        # Never truncate a field or silently drop a necessary dependency.
        over_budget = len(encoded.encode()) > self.max_bytes
        if over_budget:
            result = []
        return {"enabled": True, "allowed_memory_types": ["procedural"], "procedure_cards": result,
                "card_retrieval": {"catalog_sha256": self.fingerprint, "known_types": known,
                                   "target_types": sorted(targets), "selected_ids": [x["id"] for x in result],
                                   "status": "budget_exceeded" if over_budget else "selected" if result else "no_supported_path",
                                   "input_bytes": len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()),
                                   "model_calls": 0, "purpose": retrieval_purpose,
                                   "multi_event_scope_required": needs_event_scope, "schema_verified_online": available_schema is not None}}

    def build(self, event, *, review=None, ttt=None, executions=()):
        facts = []
        if ttt is not None:
            from src.schema.ttt_updates import walk
            for node, depth in walk(ttt):
                result = node.metadata.get("execution_result")
                if depth == 3 and isinstance(result, Mapping) and result.get("source", {}).get("query_success") is True:
                    for fact in result.get("facts", []):
                        if isinstance(fact, Mapping):
                            facts.append((str(result["source"].get("execution_id", node.node_id)), fact))
        latest = executions[-1] if executions else None
        if hasattr(latest, "to_dict"):
            latest = latest.to_dict()
        return self.retrieve(task_input(event), facts=facts, latest_execution=latest)
