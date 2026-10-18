from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.build import compile_catalog, FIELDS
from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import ProcedureCardMemory
from src.schema.itt import planner_prompt
from src.agent.planner import PLANNER_SYSTEM_PROMPT
from src.benchmarks.excytin_bench.workflow import ExcytinBenchWorkflow
from src.workflow.context import MemoryContextService


def test_source_grounding_and_schema_dependencies():
    catalog, audit = compile_catalog()
    assert len(catalog["cards"]) >= 10
    assert all(set(c["card"]) == FIELDS for c in catalog["cards"].values())
    assert all(a["sources"] and a["trajectory_count"] > 0 for a in audit.values())
    excluded, evidence = compile_catalog(exclude_incidents=["incident_322"])
    assert all("incident_322" not in a["incidents"] for a in evidence.values())


def test_dependency_route_does_not_turn_future_input_into_fact():
    memory = ProcedureCardMemory()
    result = memory.retrieve({"context": "URL https://example.invalid/mail", "question": "Who sent the email?"})
    assert result["card_retrieval"]["selected_ids"] == ["url_to_message", "message_to_sender"]
    assert result["procedure_cards"][0]["readiness"] == "input_type_available"
    assert result["procedure_cards"][1]["missing_inputs"] == ["message_id"]
    assert "message_id" not in result["card_retrieval"]["known_types"]
    next_result = memory.retrieve({"context": "", "question": "Who sent the email?"},
                                  facts=[("exec-1", {"NetworkMessageId": "new-id"})])
    assert next_result["card_retrieval"]["selected_ids"] == ["message_to_sender"]


def test_no_unsupported_path_and_schema_filter_and_budget():
    task = {"context": "URL https://example.invalid/mail", "question": "Who sent the email?"}
    memory = ProcedureCardMemory()
    assert memory.retrieve({"context": "", "question": "Who sent the email?"})["procedure_cards"] == []
    assert memory.retrieve(task, available_schema={})["procedure_cards"] == []
    limited = ProcedureCardMemory(max_bytes=10).retrieve(task)
    assert limited["card_retrieval"]["status"] == "budget_exceeded"
    assert limited["procedure_cards"] == []


def test_sender_recipient_and_click_are_distinct():
    memory = ProcedureCardMemory()
    for question, last in [("Who is the recipient of this email?", "message_to_recipient"),
                           ("Who clicked this URL?", "url_to_click"),
                           ("What is the sender IP?", "message_to_sender")]:
        result = memory.retrieve({"context": "https://example.invalid/a", "question": question})
        assert result["card_retrieval"]["selected_ids"][-1] == last


def test_file_event_pivot_needs_observed_initiating_process_role():
    memory = ProcedureCardMemory()
    task = {"question": "Can you identify the name of the suspicious file?"}
    result = memory.retrieve(task, facts=[("e1", {"InitiatingProcessFileName": "powershell.exe"})])
    assert result["card_retrieval"]["target_types"] == ["file_artifact"]
    assert result["card_retrieval"]["selected_ids"] == ["parent_process_to_file_events"]
    unbound = memory.retrieve(task, facts=[("e1", {"FileName": "powershell.exe"})])
    assert "parent_process_to_file_events" not in unbound["card_retrieval"]["selected_ids"]


def test_click_report_identifier_requires_explicit_table_scope():
    from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import known_inputs
    for record in ({"ReportId": "r1", "Url": "https://example.invalid"},
                   {"ReportId": "r1", "Type": "DeviceEvents"}):
        assert "url_click_report_id" not in known_inputs({}, [("e1", record)])
    known = known_inputs({}, [("e1", {"ReportId": "r1", "Type": "UrlClickEvents"})])
    assert known["url_click_report_id"] == ["e1"]


def test_logged_on_account_is_candidate_device_lookup():
    result = ProcedureCardMemory().retrieve({"question": "What is the AAD device ID?"},
                                           facts=[("e1", {"AccountName": "observed-user"})])
    assert result["card_retrieval"]["selected_ids"] == ["logged_on_account_to_device"]


def test_benchmark_compound_field_names_are_requested_targets():
    from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import target_types
    assert target_types("What is the ProcessId__CreatedTimeUtc__CommandLine value?") == {"process", "command"}
    assert target_types("What is the hostname of the device?") == {"device"}


def test_unknown_target_is_not_labelled_as_answer_route():
    result = ProcedureCardMemory().retrieve({"context": "A suspicious incident was detected.",
                                          "question": "Explain the unusual sequence."})
    assert result["card_retrieval"]["target_types"] == ["unclassified"]
    assert result["card_retrieval"]["purpose"] == "candidate_discovery"


def test_common_inverted_entity_questions_have_explicit_targets():
    from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import target_types
    for question, target in [("What is the name of the account?", "account"),
                             ("Identify the AadUserId.", "account"),
                             ("What is the file name of the process?", "file_artifact"),
                             ("Can you determine the name of the script file?", "file_artifact"),
                             ("What is the IP address involved in lateral movement?", "ip")]:
        assert target in target_types(question)


def test_relative_clause_does_not_erase_the_requested_url():
    result = ProcedureCardMemory().retrieve({"context": "", "question":
        "Can you identify the URL which was included in email messages later removed after delivery?"})
    assert result["card_retrieval"]["target_types"] == ["url"]
    assert result["card_retrieval"]["selected_ids"] == ["delivery_to_message", "message_to_url"]


def test_precise_url_is_preferred_to_broad_delivery_search():
    result = ProcedureCardMemory().retrieve({"context": "An email contained https://example.invalid/a and was removed.",
        "question": "What is the sender IP for the emails removed after delivery?"})
    assert result["card_retrieval"]["selected_ids"] == ["url_to_message", "message_to_sender"]


def test_failed_observation_and_planned_parameters_do_not_unlock_procedure():
    from src.schema import Event
    from src.schema.planner_operations import apply_planner_operations
    from src.schema.ttt_updates import change_node
    event = Event(event_id="test-grounding", event_name="test", message="Who sent the email?", context={})
    tree = apply_planner_operations({"operations": [
        {"op": "add_goal", "ref": "g", "objective": "identify sender"},
        {"op": "add_entity", "ref": "e", "goal_id": "$g", "objective": "unknown email"},
        {"op": "add_task", "entity_id": "$e", "objective": "NetworkMessageId guessed-identifier-123"},
    ]}, event.event_id, 1)
    memory = ProcedureCardMemory()
    failed = {"source": {"query_success": False}, "facts": [{"NetworkMessageId": "guessed-identifier-123"}]}
    tree = change_node(tree, "T1", metadata={"execution_result": failed})
    assert memory.build(event, ttt=tree)["procedure_cards"] == []
    succeeded = {"source": {"query_success": True, "execution_id": "exec-1"}, "facts": [{"network_message_id": "observed-identifier-456"}]}
    tree = change_node(tree, "T1", metadata={"execution_result": succeeded})
    assert memory.build(event, ttt=tree)["card_retrieval"]["selected_ids"] == ["message_to_sender"]


def test_workflow_ignores_legacy_pm_environment(monkeypatch):
    monkeypatch.setenv("SOCAGENT_PLANNER_PM_ENABLED", "true")
    monkeypatch.setenv("SOCAGENT_LTM_ENABLED", "true")
    calls = []
    def fake(system, prompt):
        calls.append((system, prompt))
        return '''operations:
- {op: add_goal, ref: goal, objective: 查邮件发送者}
- {op: add_entity, ref: url, goal_id: $goal, objective: URL https://example.invalid/a}
- {op: add_task, entity_id: $url, objective: 在 EmailUrlInfo 按 Url 查询邮件标识}
'''
    workflow = ExcytinBenchWorkflow(llm=fake)
    workflow.start({"context": "https://example.invalid/a", "question": "Who sent the email?"})
    assert "url_to_message" not in calls[0][0]
    assert "needs_prior_observation" not in calls[0][0]
    logging = workflow.get_logging()
    assert not logging["planner_pm_enabled"] and not logging["ltm_enabled"]
    assert logging["memory_retrievals"] == []
    assert planner_prompt(PLANNER_SYSTEM_PROMPT, {"enabled": True, "procedure_cards": []}) == PLANNER_SYSTEM_PROMPT


def test_optional_executor_format_repair_preserves_two_field_input():
    import json
    calls = []
    def fake(system, prompt):
        if "系统中的 Planner" in system:
            return '''operations:
- {op: add_goal, ref: goal, objective: 查邮件}
- {op: add_entity, ref: email, goal_id: $goal, objective: 邮件样例}
- {op: add_task, entity_id: $email, objective: 执行 MySQL SELECT 1}
'''
        payload = json.loads(prompt)
        assert set(payload) == {"id", "objective"}
        calls.append(payload)
        return "Here is a KQL explanation" if len(calls) == 1 else "sql: SELECT 1"
    workflow = ExcytinBenchWorkflow(llm=fake,
                                   executor_format_attempts=2)
    workflow.start({"context": "", "question": "查邮件"})
    action = workflow.propose_next_action()
    assert action.content == "SELECT 1;"
    assert len(calls) == 2 and "输出格式修正" in calls[1]["objective"]
    assert workflow.state.actions_issued == 1


def test_alert_discovery_does_not_claim_to_satisfy_missing_typed_inputs():
    result = ProcedureCardMemory().retrieve({"context": "A suspicious sign-in alert was detected.",
        "question": "What is the account SID?"})
    assert result["card_retrieval"]["purpose"] == "candidate_discovery"
    assert result["procedure_cards"]
    assert "account" not in result["card_retrieval"]["known_types"]
    assert all(c["purpose"] == "candidate_discovery" for c in result["procedure_cards"])
    assert "account_to_signin" not in result["card_retrieval"]["selected_ids"]


def test_specific_target_identity_beats_incidental_context():
    memory = ProcedureCardMemory()
    sid = memory.retrieve({"context": "Suspicious sign-in IP 192.0.2.4", "question":
        "Can you identify the account SID that accessed the link?"})
    assert sid["card_retrieval"]["target_types"] == ["account_sid"]
    assert sid["card_retrieval"]["selected_ids"] == ["ip_to_click_events", "upn_to_sid"]
    assert sid["procedure_cards"][-1]["missing_inputs"] == ["account_upn"]
    process = memory.retrieve({"context": "source IP 192.0.2.4", "question":
        "What is the Process ID and created time of powershell.exe used in the application window discovery alert?"})
    assert process["card_retrieval"]["selected_ids"] == ["topic_to_security_alert"]
    assert process["card_retrieval"]["purpose"] == "scope_discovery"
    assert process["card_retrieval"]["target_types"] == ["process"]


def test_identity_and_device_types_require_real_mapping():
    memory = ProcedureCardMemory()
    user = memory.retrieve({"question": "What is the account SID?"}, facts=[("e1", {"UserId": "00000000-0000-0000-0000-000000000001"})])
    assert "account_upn" not in user["card_retrieval"]["known_types"]
    assert user["procedure_cards"] == []
    upn = memory.retrieve({"question": "What is the account SID?"}, facts=[("e1", {"AccountUpn": "person@example.invalid"})])
    assert upn["card_retrieval"]["selected_ids"] == ["upn_to_sid"]
    device = memory.retrieve({"question": "What is the AadDeviceId?"}, facts=[("e1", {"DeviceId": "observed-device-id"})])
    assert device["card_retrieval"]["selected_ids"] == ["device_id_to_aad"]
    assert "aad_device_id" not in device["card_retrieval"]["known_types"]


def test_inbox_rule_retrieval_keeps_audit_ip_separate_from_signin_ip():
    result = ProcedureCardMemory().retrieve({"question": "What IP address created the suspicious inbox rule?"})
    assert result["card_retrieval"]["target_types"] == ["audit_ip"]
    assert result["card_retrieval"]["selected_ids"] == ["inbox_rule_to_audit"]


def test_pid_pivot_requires_host_and_malware_family_stays_distinct():
    memory = ProcedureCardMemory()
    result = memory.retrieve({"question": "What is the process command line?"}, facts=[("e1", {"ProcessId": 123})])
    assert result["procedure_cards"] == []
    scoped = memory.retrieve({"question": "What is the process command line?"}, facts=[("e1", {"ProcessId": 123, "DeviceName": "observed-host"})])
    assert scoped["card_retrieval"]["selected_ids"] == ["process_id_to_context"]
    family = memory.retrieve({"question": "What is the account SID?"}, facts=[("e1", {"ThreatFamily": "observed-family"})])
    assert family["card_retrieval"]["selected_ids"] == ["threat_family_to_account"]
    assert "account_sid" not in family["card_retrieval"]["known_types"]


def test_display_name_and_public_ip_do_not_become_identity_assertions():
    memory = ProcedureCardMemory()
    risk = memory.retrieve({"question": "What is the risk level?"}, facts=[("e1", {"UserDisplayName": "Observed Person"})])
    assert risk["card_retrieval"]["selected_ids"] == ["display_name_to_risky_user"]
    assert "account_upn" not in risk["card_retrieval"]["known_types"]
    ordinary = memory.retrieve({"context": "IP 192.0.2.7", "question": "Which device name is associated?"})
    assert "public_ip" not in ordinary["card_retrieval"]["known_types"]
    public = memory.retrieve({"context": "PublicIP: 192.0.2.7", "question": "Which device name is associated?"})
    assert public["card_retrieval"]["selected_ids"] == ["public_ip_to_device"]


def test_multi_event_context_does_not_bind_prior_device_ip_to_later_login():
    task = {"context": "A credential theft tool ran from device IP 192.0.2.7 and triggered an alert. Shortly after, another sign-in attempt with a deceptive user was detected.",
            "question": "What is the SID of the deceptive user involved in the sign-in attempt?"}
    result = ProcedureCardMemory().retrieve(task)
    assert result["card_retrieval"]["purpose"] == "scope_discovery"
    assert result["card_retrieval"]["multi_event_scope_required"]
    assert "ip_to_signin" not in result["card_retrieval"]["selected_ids"]
    assert result["procedure_cards"][0]["id"] in {"topic_to_alert_evidence", "topic_to_security_alert"}
