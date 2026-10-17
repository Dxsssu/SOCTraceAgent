"""Replay one official TRAIN path against MySQL using observed pivot values.

This is a guided verification, not a blind agent evaluation: graph IDs select
which of the returned candidate alerts to follow. No expected final IP is
used as a query predicate. Does not modify the database or any memory library.
"""
from collections import deque
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from dotenv import dotenv_values
import mysql.connector

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
QUESTION = ROOT/'data/excytin-bench/huggingface/questions/train/incident_5_qa_incident_o1-ga_c42.json'
GRAPH = ROOT/'data/excytin-bench/github/secgym/qagen/graph_files/incident_5.graphml'


def main():
    q = json.loads(QUESTION.read_text())[6]
    root = ET.parse(GRAPH).getroot()
    ns = {'g': 'http://graphml.graphdrawing.org/xmlns'}
    keys = {k.get('id'): k.get('attr.name') for k in root.findall('g:key', ns)}
    nodes = {int(n.get('id')): {keys[d.get('key')]: d.text for d in n.findall('g:data', ns)}
             for n in root.findall('g:graph/g:node', ns)}
    edges = [(int(e.get('source')), int(e.get('target'))) for e in root.findall('g:graph/g:edge', ns)]
    adjacent = {n: set() for n in nodes}
    for a,b in edges:
        adjacent[a].add(b); adjacent[b].add(a)
    distance = {q['start_alert']: 0}; queue = deque(distance)
    while queue:
        node = queue.popleft()
        for neighbor in adjacent[node]:
            if neighbor not in distance:
                distance[neighbor] = distance[node]+1; queue.append(neighbor)
    path = q['shortest_alert_path']
    assert all(b in adjacent[a] for a,b in zip(path,path[1:]))
    assert len(path)-1 == distance[q['end_alert']]
    expected_ids = [json.loads(nodes[n]['entry'])['SystemAlertId'] for n in path[::2]]
    report = {'mode': 'official-path-guided read-only MySQL replay', 'incident': 'incident_5',
              'split': 'train', 'question_index_zero_based': 6, 'question': q,
              'shortest_graph_distance_edges': distance[q['end_alert']],
              'steps': [], 'pivots': [], 'diagnostics': []}
    cfg = dotenv_values(ROOT/'docker/excytin-mysql/.env')
    conn = mysql.connector.connect(host='127.0.0.1',port=3306,user='root',
            password=cfg['MYSQL_ROOT_PASSWORD'],database='env_monitor_db',use_pure=True,connection_timeout=10)
    conn.start_transaction(readonly=True,consistent_snapshot=True)
    cur = conn.cursor(dictionary=True)
    def query(sql, params, expected_id, graph_node):
        cur.execute(sql,params); rows=cur.fetchall()
        matches=[r for r in rows if r['SystemAlertId']==expected_id]
        report['steps'].append({'step':len(report['steps'])+1,'sql':sql,'params':params,
                'row_count':len(rows),'distinct_alert_ids':len({r['SystemAlertId'] for r in rows}),
                'guided_selection':{'graph_node':graph_node,'SystemAlertId':expected_id},'rows':rows})
        if len(matches)!=1:
            raise ValueError(f'Expected one returned alert snapshot for node {graph_node}, got {len(matches)}')
        return matches[0]
    def field_values(text, field):
        # Extract only flat scalar tokens; retain raw text and never claim full JSON parsing.
        return re.findall(r'"'+re.escape(field)+r'"\s*:\s*"([^"\\]*)"', text)
    try:
        sql='SELECT DISTINCT SystemAlertId, AlertName, Description, Entities FROM SecurityAlert WHERE Entities LIKE %s AND LOWER(AlertName) LIKE %s'
        first=query(sql,['%get-userprttoken.ps1%','%ransomware%'],expected_ids[0],path[0])
        # Choose the official pivot only among actual returned scalar values.
        sid=next(v for v in field_values(first['Entities'],'Sid') if v==nodes[path[1]]['value'])
        report['pivots'].append({'node':path[1],'field':'Sid','value':sid,'source_step':1})
        second=query('SELECT DISTINCT SystemAlertId, AlertName, Description, Entities FROM SecurityAlert WHERE Entities LIKE %s AND AlertName LIKE %s',
                ['%'+sid+'%','%Primary Refresh Token%'],expected_ids[1],path[2])
        assert sid in field_values(second['Entities'],'Sid')
        process=next(v for v in field_values(second['Entities'],'Name') if v.lower()==nodes[path[3]]['value'])
        report['pivots'].append({'node':path[3],'graph_identifier':'ExtractedFileName',
                'observed_field':'ImageFile.Name (raw entity text)','value':process,'source_step':2})
        third=query('SELECT DISTINCT SystemAlertId, AlertName, Description, Entities FROM SecurityAlert WHERE LOWER(Entities) LIKE %s AND AlertName LIKE %s',
                ['%'+process.lower()+'%','%Primary Refresh Token%'],expected_ids[2],path[4])
        assert process.lower() in {v.lower() for v in field_values(third['Entities'],'Name')}
        addresses=list(dict.fromkeys(field_values(third['Entities'],'Address')))
        report['answer_from_database']=addresses
        report['matches_official_answer']=addresses==[q['answer']]
        report['path_verified']=True
        for node,row in zip(path[::2],[first,second,third]):
            try:
                json.loads(row['Entities']);valid=True;error=None
            except json.JSONDecodeError as exc:
                valid=False;error=str(exc)
            report['diagnostics'].append({'graph_node':node,'entities_valid_json':valid,'parse_error':error})
        report['limitations']=[
            'Candidate alerts were selected with official graph guidance; this does not measure autonomous discovery.',
            'Graph shortest distance is four edges between alerts, not the minimum SQL query count.',
            'browsercore.exe is an extracted filename shared across alerts, not a globally unique process identifier.',
            'The result establishes IP membership in a PRT-labelled alert, not proof of successful token theft or network causality.',
            'Entities returned by MySQL may have invalid escaping; raw results retained and only simple observed scalars extracted.',
        ]
    finally:
        (OUT/'evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        cur.close();conn.rollback();conn.close()
    print(json.dumps({'path':path,'rows_per_step':[s['row_count'] for s in report['steps']],
                     'unique_alerts_per_step':[s['distinct_alert_ids'] for s in report['steps']],
                     'answer':report.get('answer_from_database'),'matches_official':report.get('matches_official_answer'),
                     'diagnostics':report['diagnostics']},ensure_ascii=False))


if __name__=='__main__':
    main()
