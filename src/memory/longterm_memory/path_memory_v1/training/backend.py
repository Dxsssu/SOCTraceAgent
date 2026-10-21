"""Independent raw-log adapter and session-local MySQL evidence projection.

Never reads question records, graph nodes or reference answers. Original tables stay
unchanged. Source membership is read from SecurityIncident.AlertIds, not graph paths.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from .core import digest, extract

SOURCE_ROOT=Path('data/excytin-bench/mysql/data_anonymized/alphineskihouse')
TABLE='LTMEntityEvidence'


class RawLogSource:
    def __init__(self, root=SOURCE_ROOT):
        self.root=root
        membership_file=root/'SecurityIncident.csv'
        alert_files=sorted((root/'SecurityAlert').glob('*.csv'))
        if not alert_files:raise ValueError('Missing original SecurityAlert CSV files')
        self.files=[membership_file,*alert_files]
        self.hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.files}
        self.members={};self.snapshots={};self.row_count=0;self.invalid_rows={}
        with membership_file.open(encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f,delimiter='❖'):
                ids=json.loads(row['AlertIds'])
                if not isinstance(ids,list):raise ValueError('Invalid incident AlertIds')
                self.members.setdefault(str(row['IncidentNumber']),set()).update(ids)
        for file in alert_files:
            with file.open(encoding='utf-8-sig',newline='') as f:
                for line,row in enumerate(csv.DictReader(f,delimiter='❖'),2):
                    self.row_count+=1
                    # Parse CSV quoting first, JSON second. No heuristic JSON repair.
                    try:
                        entities=json.loads(row['Entities'])
                        if not isinstance(entities,list):raise ValueError('Non-array Entities')
                    except ValueError as exc:
                        self.invalid_rows.setdefault(row['SystemAlertId'],[]).append({'file':str(file),'record':line,'error':str(exc)})
                        continue
                    key=digest([row['SystemAlertId'],row['AlertName'],entities])
                    if key not in self.snapshots:
                        self.snapshots[key]={'SystemAlertId':row['SystemAlertId'],'AlertName':row['AlertName'],
                            'TimeGenerated':row['TimeGenerated'],'Entities':entities,
                            'SourceRowHash':key,'source_file':str(file),'source_record':line,'duplicate_rows':1}
                    else:
                        self.snapshots[key]['duplicate_rows']+=1
        self.by_alert={}
        for row in self.snapshots.values():self.by_alert.setdefault(row['SystemAlertId'],[]).append(row)

    def evidence(self,incident):
        number=incident.removeprefix('incident_')
        if number not in self.members:raise ValueError('Unknown source incident')
        ids=self.members[number]
        invalid={a:self.invalid_rows[a] for a in ids if a in self.invalid_rows}
        if invalid:raise ValueError(f'Invalid original source rows for incident {number}: {invalid}')
        missing=sorted(ids-self.by_alert.keys())
        # Missing source logs are not fabricated; surface in readiness diagnostics.
        rows=[]
        for alert_id in sorted(ids):
            for row in self.by_alert.get(alert_id,[]):
                for e in extract(row['Entities']):
                    rows.append((row['SourceRowHash'],row['SystemAlertId'],row['AlertName'],row['TimeGenerated'],
                         e['entity_index'],e['concept'],e['graph_value'],
                         json.dumps(e['raw_components'],ensure_ascii=False),json.dumps(e['roles'],ensure_ascii=False)))
        return rows,{'incident':incident,'source_alert_ids':len(ids),'missing_source_alert_ids':missing,
          'snapshots':sum(len(self.by_alert.get(a,[])) for a in ids),'entity_rows':len(rows),
          'source_files':self.hashes,'source_policy':'all incident membership snapshots; retain distinct alert payload snapshots; no graph access'}


class ReplayBackend:
    def __init__(self, incident, source):
        load_dotenv('docker/excytin-mysql/.env')
        self.db=MySQLQueryExecutor(incident,password=os.environ['MYSQL_ROOT_PASSWORD'])
        self.query_count=0
        try:
            rows,self.manifest=source.evidence(incident)
            self.db.cursor.execute(f'''CREATE TEMPORARY TABLE {TABLE} (
                SourceRowHash CHAR(64) CHARACTER SET ascii NOT NULL,
                AlertId VARCHAR(255) NOT NULL, AlertTitle TEXT NOT NULL, ObservedAt TEXT,
                EntityIndex INT NOT NULL, Concept VARCHAR(64) CHARACTER SET ascii NOT NULL,
                GraphValue LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
                RawComponents JSON NOT NULL, Roles JSON NOT NULL,
                KEY idx_snapshot (SourceRowHash), KEY idx_concept (Concept)
            )''')
            if rows:
                self.db.cursor.executemany(f'INSERT INTO {TABLE} VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',rows)
            # MySQL cannot reference a TEMPORARY table twice in one statement.
            # An identical input-side copy permits the scoped self-association join.
            self.db.cursor.execute(f'CREATE TEMPORARY TABLE LTMInputEvidence LIKE {TABLE}')
            self.db.cursor.execute(f'INSERT INTO LTMInputEvidence SELECT * FROM {TABLE}')
        except Exception:
            self.db.close();raise

    def execute(self,episode,values,title):
        if not values:return {'status':'empty','outputs':[],'query_count':0}
        values=sorted(set(values))
        if not all(isinstance(v,str) and v for v in values):raise ValueError('Invalid input values')
        # Trusted template; all values remain bound parameters.
        sql=episode['sql'].replace(':event_title','%s').replace(':input_values',','.join(['%s']*len(values)))
        params=(title,*values)
        calls=0
        data=[]
        try:
            while True:
                self.query_count+=1;calls+=1
                self.db.cursor.execute(sql+f' OFFSET {len(data)}',params)
                columns=[c[0] for c in self.db.cursor.description]
                page=[dict(zip(columns,row)) for row in self.db.cursor.fetchall()]
                data.extend(page[:episode['row_limit']])
                if len(page)<=episode['row_limit']:break
        except Exception as exc:
            return {'status':'sql_error','outputs':[],'error':str(exc),'query_count':calls}
        outputs=[]
        for row in data:
            outputs.append({'concept':episode['signature']['wanted'],'graph_value':row['value'],
                'raw_components':json.loads(row['RawComponents']),'roles':json.loads(row['Roles']),
                'entity_index':row['EntityIndex'],'alert_id':row['AlertId'],'time':row['ObservedAt'],
                'source_row_hash':row['SourceRowHash'],'matched_inputs':[row['matched_input']]})
        return {'status':'ok' if outputs else 'empty','outputs':outputs,'query_count':calls,
                'sql':sql,'parameters':params}

    def close(self):
        self.db.close()  # MySQL automatically drops session-local temporary tables.
