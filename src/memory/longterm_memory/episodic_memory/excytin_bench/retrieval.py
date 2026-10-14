"""Indexed error signatures, SQL-scope gates and local TF-IDF tie breaking.

The v2 three-field memory is unchanged. No network/LLM is used for retrieval.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from copy import deepcopy

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .sql_parser import classify_error


def tokens(text):
    return re.findall(r"[a-z_][a-z_0-9]*|[\u4e00-\u9fff]", text.lower())


def sql_tables(text):
    try:
        tree = sqlglot.parse_one(re.sub(r"<NUMBER_\d+>", "0", text), read="mysql")
        # Scope sources exclude CTE names and string literals that look like SQL.
        return {source.name for scope in traverse_scope(tree)
                for source in scope.sources.values() if isinstance(source, exp.Table)} or {
                    t.name for t in tree.find_all(exp.Table)}
    except (sqlglot.errors.SqlglotError, ValueError):
        return set()


def failure_tables(sql, subject):
    tables = {t.lower() for t in sql_tables(sql)}
    if not subject or "." not in subject:
        return tables
    qualifier, column = subject.replace("`", "").rsplit(".", 1)
    try:
        tree = sqlglot.parse_one(re.sub(r"<NUMBER_\d+>", "0", sql), read="mysql")
        matched = set()
        for scope in traverse_scope(tree):
            if not any(c.name.lower() == column.lower() and c.table.lower() == qualifier.lower()
                       for c in scope.columns):
                continue
            for alias, source in scope.sources.items():
                if alias.lower() == qualifier.lower() and isinstance(source, exp.Table):
                    matched.add(source.name.lower())
        # Unresolved/ambiguous aliases must not borrow another table's repair.
        return matched if len(matched) == 1 else set()
    except (sqlglot.errors.SqlglotError, ValueError):
        return set()


def subject_key(subject):
    return (subject or "").replace("`", "").rsplit(".", 1)[-1].lower()


def error_clause(error):
    match = re.search(r"in ['\"]([^'\"]+)['\"]", error, re.I)
    return match.group(1).lower() if match else ""


class RecoveryIndex:
    def __init__(self, knowledge):
        if knowledge.get("format_version") != "2.0":
            raise ValueError("Rebuild failure-recovery Episodic Memory v2")
        self.entries = deepcopy(knowledge["entries"])
        self.signatures = defaultdict(list)
        self.background_tokens = []
        self.clauses = []
        for i, entry in enumerate(self.entries):
            kind, subject = classify_error(entry["error"])
            self.signatures[(kind, subject_key(subject))].append(i)
            # v2 backgrounds name the failing table before “表”; do not use
            # repair text, which can name a different destination table.
            tables = re.findall(r"([a-zA-Z_][\w]*)\s*表", entry["background"])
            self.background_tokens.append({t.lower() for t in tables} or set(tokens(entry["background"])))
            self.clauses.append(error_clause(entry["error"]))
        docs = [Counter(tokens(e["error"])) for e in self.entries]
        df = Counter(t for d in docs for t in d)
        self.idf = {t: math.log((1 + len(docs)) / (1 + n)) + 1 for t, n in df.items()}
        self.vectors = [self.vector(d) for d in docs]

    def vector(self, counts):
        v = {
            t: (1 + math.log(n)) * self.idf[t]
            for t, n in counts.items()
            if t in self.idf
        }
        norm = math.sqrt(sum(x * x for x in v.values()))
        return {t: x / norm for t, x in v.items()} if norm else {}

    def retrieve(
        self, error, *, background="", table_hints=(), limit=3, min_similarity=0.35
    ):
        if limit <= 0 or not error.strip():
            return []
        kind, subject = classify_error(error)
        if not kind:
            return []
        # The executed SQL is authoritative; hints are only for callers with no SQL.
        tables = failure_tables(background, subject if kind == "unknown_column" else None) if background else {t.lower() for t in table_hints}
        if not tables:
            return []
        candidates = self.signatures.get((kind, subject_key(subject)), ())
        candidates = [i for i in candidates if tables & self.background_tokens[i]]
        # An unqualified column in a join can refer to either table. Abstain
        # if matching memories prescribe repairs for different source tables.
        scopes = {tuple(sorted(tables & self.background_tokens[i])) for i in candidates}
        if len(scopes) > 1:
            return []
        query = self.vector(Counter(tokens(error)))
        clause = error_clause(error)
        ranked = []
        for i in candidates:
            similarity = sum(v * self.vectors[i].get(t, 0) for t, v in query.items())
            # Exact typed subjects survive irrelevant driver/alias wording.
            # Generic errors still require lexical evidence, never nearest-only.
            if subject or similarity >= min_similarity:
                ranked.append((int(bool(clause) and clause == self.clauses[i]), similarity, i))
        ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
        seen, result = set(), []
        for _, _, i in ranked:
            repair = self.entries[i]["repair"]
            if repair in seen:
                continue
            seen.add(repair)
            result.append(deepcopy(self.entries[i]))
            if len(result) >= limit:
                break
        return result
