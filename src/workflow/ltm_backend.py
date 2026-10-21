"""MySQL environment adapter for LTM-generated SQL actions."""
import json
from functools import lru_cache
from src.memory.longterm_memory.path_memory_v1.training.backend import ReplayBackend, RawLogSource


@lru_cache(maxsize=1)
def raw_source():
    return RawLogSource()


class LTMQueryExecutor:
    def __init__(self,incident):
        self.backend=ReplayBackend(incident,raw_source())

    def execute(self,sql):
        from src.benchmarks.excytin_bench.workflow import ReadOnlySQLValidator
        sql=ReadOnlySQLValidator.validate(sql)
        try:
            self.backend.query_count+=1
            cur=self.backend.db.cursor
            cur.execute(sql)
            names=[c[0] for c in cur.description]
            rows=[dict(zip(names,row)) for row in cur.fetchall()]
            return json.dumps(rows,ensure_ascii=False,default=str),True,len(rows)
        except Exception as exc:
            return f'SQL error: {exc}',False,0

    def close(self):
        self.backend.close()
