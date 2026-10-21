"""Serial review session. build_next always commits one case before the next begins."""
import argparse
import json
import os
from dotenv import load_dotenv
from src.benchmarks.excytin_bench.runner import MySQLQueryExecutor
from .build_one import build_next


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--count',type=int,default=1,help='Number of sequential one-case transactions')
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    from .backend import RawLogSource,ReplayBackend
    connections={};source=None
    def query(incident,episode,values,title):
        nonlocal source
        if source is None:source=RawLogSource()
        if incident not in connections:connections[incident]=ReplayBackend(incident,source)
        return connections[incident].execute(episode,values,title)
    try:
        for _ in range(args.count):
            result=build_next(query if args.live else None)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            if result.get('status')=='complete':break
    finally:
        for db in connections.values():db.close()

if __name__=='__main__':main()
