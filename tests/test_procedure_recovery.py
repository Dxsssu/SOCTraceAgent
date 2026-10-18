from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.recovery import is_time_recovery, time_scope, is_alert_device_recovery
from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.retrieval import ProcedureCardMemory
from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.build import compile_catalog
from src.memory.longterm_memory.procedural_memory.excytin_bench.cards.recovery import is_url_form_recovery, is_parent_role_recovery


SQL = "SELECT IPAddress, TimeGenerated FROM SigninLogs WHERE IPAddress = '192.0.2.1' AND TimeGenerated = '2024-01-01'"


def test_recovery_requires_preserving_non_time_predicates():
    assert is_time_recovery(SQL, "SELECT IPAddress FROM SigninLogs WHERE IPAddress = '192.0.2.1'")
    assert not is_time_recovery(SQL, "SELECT IPAddress FROM SigninLogs WHERE IPAddress = '192.0.2.2'")
    assert time_scope(SQL.replace(" AND ", " OR ")) is None
    assert time_scope("SELECT * FROM SigninLogs WHERE TimeGenerated = '2024-01-01'") is None


def test_only_latest_empty_success_with_time_and_anchor_triggers_recovery():
    memory = ProcedureCardMemory()
    task = {"context": "An incident was detected.", "question": "What was the login IP?"}
    execution = {"sql": SQL, "query_success": True, "result_status": "empty_result", "execution_id": "E4"}
    result = memory.retrieve(task, latest_execution=execution)
    assert result["card_retrieval"]["selected_ids"] == ["empty_time_scope_diagnostic"]
    assert result["card_retrieval"]["purpose"] == "scope_recovery"
    assert "ip" not in result["card_retrieval"]["known_types"]
    for changed in (dict(execution, query_success=False), dict(execution, result_status="rows_returned"),
                    dict(execution, sql="SELECT * FROM SigninLogs WHERE IPAddress='192.0.2.1'")):
        assert "empty_time_scope_diagnostic" not in memory.retrieve(task, latest_execution=changed)["card_retrieval"]["selected_ids"]
    assert ProcedureCardMemory(max_bytes=1).retrieve(task, latest_execution=execution)["procedure_cards"] == []


def test_recovery_provenance_is_a_pair_and_honors_incident_exclusion():
    _, audit = compile_catalog()
    recovery = audit["empty_time_scope_diagnostic"]
    assert len(recovery["recovery_pairs"]) == 3
    assert all(is_time_recovery(p["preceding_empty_sql"], p["sql"]) for p in recovery["recovery_pairs"])
    catalog, _ = compile_catalog(exclude_incidents=recovery["incidents"])
    assert "empty_time_scope_diagnostic" not in catalog["cards"]


def test_alert_recovery_preserves_alert_identity_and_does_not_bind_entities():
    before = "SELECT * FROM AlertEvidence WHERE AlertId IN ('a','b') AND DeviceName='host'"
    after = "SELECT AccountName FROM AlertEvidence WHERE AlertId IN ('a','b')"
    assert is_alert_device_recovery(before, after)
    assert not is_alert_device_recovery(before, after.replace("'b'", "'c'"))
    assert not is_alert_device_recovery(before.replace(' AND ', ' OR '), after)
    memory = ProcedureCardMemory()
    execution = {"sql": before, "query_success": True, "result_status": "empty_result"}
    result = memory.retrieve({"question": "What is the account SID?"}, latest_execution=execution)
    assert result["card_retrieval"]["selected_ids"] == ["empty_alert_device_scope_diagnostic"]
    assert 'account_sid' not in result["card_retrieval"]["known_types"]
    assert 'device_name' not in result["card_retrieval"]["known_types"]
    assert 'empty_alert_device_scope_diagnostic' not in memory.retrieve({}, latest_execution=dict(execution, result_status='rows_returned'))['card_retrieval']['selected_ids']


def test_url_and_parent_diagnostics_have_separate_narrow_triggers():
    memory = ProcedureCardMemory()
    for sql, expected in [
        ("SELECT RemoteUrl FROM DeviceNetworkEvents WHERE RemoteUrl='login.example.invalid'", 'empty_url_form_diagnostic'),
        ("SELECT * FROM DeviceProcessEvents WHERE InitiatingProcessFileName='tool.exe'", 'empty_parent_role_diagnostic')]:
        execution = {'sql': sql, 'query_success': True, 'result_status': 'empty_result'}
        result = memory.retrieve({}, latest_execution=execution)
        assert result['card_retrieval']['selected_ids'] == [expected]
        assert result['card_retrieval']['purpose'] == 'scope_recovery'
        assert expected not in memory.retrieve({}, latest_execution=dict(execution, query_success=False))['card_retrieval']['selected_ids']
    assert is_url_form_recovery("SELECT * FROM UrlClickEvents WHERE Url='https://host.example.invalid/a'",
                                "SELECT * FROM UrlClickEvents WHERE Url LIKE '%host.example.invalid%'")
    assert not is_url_form_recovery("SELECT * FROM UrlClickEvents WHERE Url='https://host.example.invalid/a'",
                                    "SELECT * FROM UrlClickEvents WHERE Url LIKE '%other.invalid%'")
    assert is_parent_role_recovery("SELECT * FROM DeviceProcessEvents WHERE InitiatingProcessFileName='tool.exe'",
                                   "SELECT * FROM DeviceProcessEvents WHERE FileName LIKE '%tool%' OR InitiatingProcessFileName LIKE '%tool%'")
    assert not is_parent_role_recovery("SELECT * FROM DeviceProcessEvents WHERE InitiatingProcessFileName='tool.exe'",
                                       "SELECT * FROM DeviceProcessEvents WHERE FileName LIKE '%other%' OR InitiatingProcessFileName LIKE '%other%'")
