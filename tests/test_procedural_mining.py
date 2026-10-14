import pytest
import sqlglot
from src.workflow.context import ExcytinBenchSnapshotRepository
from src.memory.longterm_memory.procedural_memory.excytin_bench.build_knowledge import normalize_record


def sample(sql,status='rows_returned'):
    return {'valid':True,'status':status,'tree':sqlglot.parse_one(sql,read='mysql'),'table':'EmailEvents'}


def test_parameterization_generalizes_different_case_values_and_in_cardinality():
    schema={t['name']:t for t in ExcytinBenchSnapshotRepository().semantic['tables']}
    a=normalize_record(sample("SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId='case-a'"),schema)
    b=normalize_record(sample("SELECT RecipientEmailAddress FROM EmailEvents WHERE NetworkMessageId='case-b'"),schema)
    assert a[0]==b[0] and 'case-a' not in a[0]
    a=normalize_record(sample("SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId IN ('a','b')"),schema)
    b=normalize_record(sample("SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId IN ('c')"),schema)
    assert a[0]==b[0] and a[2]['p1']['type']=='string[]'


@pytest.mark.parametrize('sql,status',[
    ("SELECT * FROM EmailEvents",'rows_returned'),
    ("SELECT SenderFromAddress FROM EmailEvents WHERE NetworkMessageId='a'",'error'),
    ("SELECT BadField FROM EmailEvents WHERE NetworkMessageId='a'",'rows_returned'),
    ("SELECT COUNT(*) FROM EmailEvents WHERE NetworkMessageId='a'",'rows_returned'),
])
def test_unsafe_unscoped_or_failed_sources_are_excluded(sql,status):
    schema={t['name']:t for t in ExcytinBenchSnapshotRepository().semantic['tables']}
    with pytest.raises(ValueError):normalize_record(sample(sql,status),schema)
