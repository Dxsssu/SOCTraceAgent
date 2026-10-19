from copy import deepcopy
import json

import pytest
import yaml

from src.memory.longterm_memory.semantic_memory.v2 import (
    ConceptQuery as Q, SemanticMemoryV2, executor_context, render_view,
)
from src.memory.longterm_memory.semantic_memory.v2.build import SOURCE, build


@pytest.fixture
def store():
    return SemanticMemoryV2.load()


def test_email_id_sender_minimal_projection(store):
    result = store.retrieve(known=[Q("邮件ID", "internet_message_id")], wanted=[Q("发件人", "header_from")])
    assert {c["table"] for c in result["candidates"]} == {"EmailEvents", "EmailPostDeliveryEvents"}
    selected = executor_context(result, "EmailEvents")
    assert {f["field"] for f in selected["fields"]} == {"InternetMessageId", "SenderFromAddress"}
    assert selected["time_field"]["field"] == "TimeGenerated"
    assert all(f["type"] == "TEXT" for f in selected["fields"])
    assert "AuthenticationDetails" not in render_view(result)
    assert "sample" not in json.dumps(result)


def test_network_id_retrieves_actual_attachment_sender(store):
    result = store.retrieve(known=[Q("Email_ID", "network_message_id")], wanted=[Q("Email_Sender", "header_from")])
    selected = executor_context(result, "EmailAttachmentInfo")
    assert {f["field"] for f in selected["fields"]} == {"NetworkMessageId", "SenderFromAddress"}
    assert result["total"] == 6  # Includes AlertEvidence after full-schema coverage.


def test_unqualified_id_does_not_silently_become_a_filter(store):
    result = store.retrieve(known=[Q("邮件ID")], wanted=[Q("发件人")])
    assert any(a["concept"] == "Email_ID" for a in result["ambiguities"])
    assert any(a["concept"] == "Email_Sender" for a in result["ambiguities"])
    with pytest.raises(ValueError, match="identifier kinds"):
        executor_context(result, "EmailEvents")


def test_hash_algorithm_and_initiating_process_are_not_conflated(store):
    result = store.retrieve(known=[Q("文件哈希", "sha256")], wanted=[Q("文件夹路径")])
    assert all(f["field"] not in {"SHA1", "MD5", "InitiatingProcessSHA256"}
               for c in result["candidates"] for f in c["fields"])
    result = store.retrieve(known=[Q("PID")], wanted=[Q("命令行")])
    assert all(f["field"] not in {"InitiatingProcessId", "InitiatingProcessCommandLine"}
               for c in result["candidates"] for f in c["fields"])


def test_distinct_physical_names_share_an_explicit_concept(store):
    result = store.retrieve(known=[Q("账户UPN")], wanted=[Q("登录IP")], limit=30)
    fields = {c["table"]: {f["field"] for f in c["fields"]} for c in result["candidates"]}
    assert "UserPrincipalName" in fields["SigninLogs"]
    assert "AccountUpn" in fields["DeviceProcessEvents"]
    assert result["candidates"][0]["capability"] == "direct"


def test_unknown_concept_or_kind_does_not_drop_a_constraint(store):
    for unknown in [Q("不存在的概念"), Q("邮件ID", "typo")]:
        result = store.retrieve(known=[Q("邮件ID", "network_message_id"), unknown], wanted=[Q("发件人")])
        assert result["unresolved"]
        assert all(c["capability"] == "partial" for c in result["candidates"])
        selected = executor_context(result, "EmailEvents")
        assert selected["unresolved"] and selected["capability"] == "partial"
    result = store.retrieve(known=[Q("不存在的概念")], wanted=[Q("发件人")])
    assert result["candidates"] == []


def test_multi_input_requires_all_fields_in_same_table(store):
    result = store.retrieve(known=[Q("邮件ID", "network_message_id"), Q("主机名")], wanted=[Q("发件人")], limit=30)
    assert result["candidates"]
    assert all(c["capability"] == "partial" for c in result["candidates"])
    assert all(c["missing"] for c in result["candidates"])


def test_missing_output_never_claims_an_unproven_join(store):
    result = store.retrieve(known=[Q("邮件ID", "internet_message_id")], wanted=[Q("文件夹路径")])
    assert all(c["capability"] == "partial" for c in result["candidates"])
    assert "requires_join" not in json.dumps(result)
    selected = executor_context(result, "EmailEvents")
    assert selected["capability"] == "partial"
    assert "wanted:Folder_Path" in selected["missing"]


def test_schema_scope_removes_nonexistent_fields_and_tables():
    store = SemanticMemoryV2.load(schema={"EmailEvents": {"NetworkMessageId": "varchar(100)"}})
    result = store.retrieve(known=[Q("邮件ID", "network_message_id")], wanted=[Q("发件人")])
    assert result["schema_scope"] == "target_snapshot"
    assert result["total"] == 1
    candidate = result["candidates"][0]
    assert candidate["capability"] == "partial"
    assert candidate["fields"][0]["type"] == "varchar(100)"
    assert candidate["time_field"] is None
    empty = SemanticMemoryV2.load(schema={}).retrieve(known=[Q("邮件ID")], wanted=[Q("发件人")])
    assert empty["total"] == 0


def test_pagination_is_visible_and_deterministic(store):
    args = dict(known=[Q("邮件ID", "network_message_id")], wanted=[Q("发件人", "header_from")])
    full = store.retrieve(**args, limit=100)
    pages = [store.retrieve(**args, limit=2, offset=i) for i in (0, 2, 4)]
    assert [c for p in pages for c in p["candidates"]] == full["candidates"]
    assert pages[0]["has_more"] and pages[0]["next_offset"] == 2
    assert not pages[-1]["has_more"]


def test_executor_pass_through_is_isolated_and_requires_retrieved_table(store):
    result = store.retrieve(known=[Q("邮件ID", "internet_message_id")], wanted=[Q("发件人", "header_from")])
    before = deepcopy(result)
    selected = executor_context(result, "EmailEvents")
    selected["fields"].clear()
    assert result == before
    with pytest.raises(ValueError):
        executor_context(result, "invented_table")


def test_dictionary_validation():
    concepts, tables = build()
    tables["catalog_id"] = "other-version"
    with pytest.raises(ValueError, match="different catalogs"):
        SemanticMemoryV2(concepts, tables)
    concepts, tables = build()
    concepts["concepts"]["Email_Sender"]["aliases"].append("邮件ID")
    with pytest.raises(ValueError, match="alias"):
        SemanticMemoryV2(concepts, tables)


def test_rebuild_and_every_mapping_matches_source_schema():
    from src.memory.longterm_memory.semantic_memory.v2.store import PACKAGE
    concepts, tables = build()
    assert concepts == json.loads((PACKAGE / "excytin/concepts.json").read_text())
    assert tables == json.loads((PACKAGE / "excytin/tables.json").read_text())
    schemas = {p.stem: {c["name"]: c["type"] for c in yaml.safe_load(p.read_text())["core_columns"]}
               for p in (SOURCE / "l2").glob("*.yaml")}
    for item in concepts["concepts"].values():
        for row in item["fields"]:
            assert schemas[row["table"]][row["field"]] == row["type"]


def test_runtime_reads_only_two_jsons(tmp_path, monkeypatch):
    concepts, tables = build()
    for name, doc in (("concepts", concepts), ("tables", tables)):
        (tmp_path / f"{name}.json").write_text(json.dumps(doc))
    from pathlib import Path
    original = Path.read_text
    reads = []
    def tracked(path, *args, **kwargs):
        reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", tracked)
    memory = SemanticMemoryV2.load(tmp_path)
    result = memory.retrieve(known=[Q("邮件ID", "internet_message_id")], wanted=[Q("发件人", "header_from")])
    executor_context(result, "EmailEvents")
    assert reads == ["concepts.json", "tables.json"]


@pytest.mark.parametrize("query", [Q("IP"), Q("Source_IP"), Q("Source_IP", "source_ip"), Q("来源IP")])
def test_authentication_intent_recovers_from_wrong_ip_role(store, query):
    result = store.retrieve(known=[query], wanted=[Q("Login_Time"), Q("Application")],
                            intent="When did suspicious SAP authentication from this IP occur?")
    first = result["candidates"][0]
    assert first["table"] == "SigninLogs"
    assert {f["field"] for f in first["fields"]} == {"IPAddress", "CreatedDateTime", "AppDisplayName"}
    assert first["time_field"]["field"] == "TimeGenerated"
    assert result["total"] > 5 and result["has_more"]
    assert executor_context(result, "SigninLogs")["capability"] == "direct"


@pytest.mark.parametrize("intent,expected", [
    ("查询防火墙流量的来源 IP", "AZFWApplicationRule"),
    ("Investigate endpoint network connection", "DeviceNetworkEvents"),
    ("查询该 IP 对应的点击用户", "UrlClickEvents"),
])
def test_intent_changes_ranking_without_dropping_ip_roles(store, intent, expected):
    result = store.retrieve(known=[Q("IP")], wanted=[], intent=intent, limit=100)
    assert result["candidates"][0]["table"] == expected
    names = {c["table"] for c in result["candidates"]}
    assert {"SigninLogs", "DeviceNetworkEvents", "AZFWNetworkRule", "UrlClickEvents"} <= names
    network = next(c for c in result["candidates"] if c["table"] == "DeviceNetworkEvents")
    assert {f["kind"] for f in network["fields"]} == {"local_ip", "remote_ip"}


def test_partial_url_table_remains_valid_intermediate_step(store):
    result = store.retrieve(known=[Q("URL", "clicked_url")], wanted=[Q("Email_ID", "network_message_id"), Q("Email_Sender")], intent="查点击 URL 的邮件发件人")
    context = executor_context(result, "UrlClickEvents")
    assert context["capability"] == "partial"
    assert "wanted:Email_Sender" in context["missing"]
    assert {f["field"] for f in context["fields"]} == {"Url", "NetworkMessageId"}


def test_intent_ranking_cannot_restore_nonexistent_columns():
    m = SemanticMemoryV2.load(schema={"SigninLogs": {"IPAddress": "varchar(80)"}})
    result = m.retrieve(known=[Q("Source_IP")], wanted=[Q("Login_Time")], intent="身份验证时间")
    context = executor_context(result, "SigninLogs")
    assert context["capability"] == "partial"
    assert context["time_field"] is None
    assert [f["field"] for f in context["fields"]] == ["IPAddress"]


def test_full_source_and_procedural_schema_coverage(store):
    from scripts.audit_semantic_coverage import audit, ROOT
    schema = {p.stem: {c['name']: c['type'] for c in yaml.safe_load(p.read_text())['core_columns']}
              for p in (SOURCE / 'l2').glob('*.yaml')}
    assert audit(store, schema)['passed']
    catalog = json.loads((ROOT / 'src/memory/longterm_memory/procedural_memory/trajectory_snippets/excytin/catalog.json').read_text())
    for template in catalog['templates'].values():
        assert audit(store, template['schema_fields'])['passed']
    # Every field must be retrievable, not merely present in a table description.
    for table, fields in schema.items():
        for field in fields:
            result = store.retrieve(known=[Q('Field:' + field)], wanted=[], limit=100)
            match = next(c for c in result['candidates'] if c['table'] == table)
            assert field in {r['field'] for r in match['fields']}


def test_physical_index_is_not_injected_into_planner_vocabulary(store):
    from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession
    vocabulary = SemanticPlanningSession(store).vocabulary()
    assert not any(name.startswith('Field:') for name in vocabulary)
    assert 'Alert_ID' in vocabulary and 'Alert_Title' in vocabulary
    result = store.retrieve(known=[Q('Field:Entities')], wanted=[], limit=100)
    assert 'SecurityAlert' in {c['table'] for c in result['candidates']}
    assert not store.retrieve(known=[Q('Field:invented_column')], wanted=[])['candidates']


def test_alert_evidence_retrieval_and_sql_binding(store):
    from src.memory.longterm_memory.semantic_memory.v2.session import SemanticPlanningSession, validate_sql_binding
    session = SemanticPlanningSession(store)
    session.retrieve({'known': ['Alert_ID'], 'wanted': ['Device_Name', 'Email_ID', 'Alert_Title']})
    payload = {'operations': [{'op': 'add_task', 'entity_id': 'E1',
        'objective': '查询告警 a-1 的 DeviceName、NetworkMessageId、EntityType 和 EvidenceRole',
        'table': 'AlertEvidence'}]}
    _, contexts = session.prepare(payload)
    assert {'EntityType', 'EvidenceRole'} <= {f['field'] for f in contexts[0]['fields']}
    validate_sql_binding("SELECT DeviceName, NetworkMessageId, EntityType, EvidenceRole FROM AlertEvidence WHERE AlertId = 'a-1'", contexts[0])
    assert 'Login_Time' not in {f['concept'] for f in contexts[0]['fields']}
