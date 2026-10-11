"""Paper-compatible observation formatting and alert-layer preflight."""
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor


class PaperQueryExecutor(MySQLQueryExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.cursor.execute('SHOW TABLES;')
            tables = {row[0] for row in self.cursor.fetchall()}
            missing = {'AlertInfo', 'AlertEvidence', 'SecurityAlert'} - tables
            if missing:
                raise ValueError(f'ExCyTIn alert-layer tables missing: {sorted(missing)}')
        except Exception:
            self.close()
            raise

    def execute(self, sql):
        try:
            self.cursor.execute(sql)
            rows = self.cursor.fetchall()
            count = len(rows)
            observation = str(rows)
            if len(observation) > self.max_str_len and count > self.max_entry_return:
                observation = (f'Retrieved {count} entries. Displaying first '
                               f'{self.max_entry_return} entries.\n{rows[:self.max_entry_return]}')
            return observation, True, count
        except Exception as exc:
            # ExcytinEnv uses __context__, including its literal None value.
            return f'{type(exc).__name__}: {exc.__context__}', False, 0
