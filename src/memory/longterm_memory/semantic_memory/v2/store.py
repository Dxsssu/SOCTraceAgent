from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping

PACKAGE = Path(__file__).resolve().parent


def _normalize(value: str) -> str:
    if value.strip().casefold().startswith("field:"):
        return value.strip().casefold()
    return re.sub(r"[\s_\-]+", "", value).casefold()


def _matches_intent(keyword: str, text: str) -> bool:
    if re.fullmatch(r"[a-zA-Z -]+", keyword):
        return bool(re.search(r"(?<![a-zA-Z])" + re.escape(keyword) + r"(?![a-zA-Z])", text, re.I))
    return keyword.casefold() in text.casefold()


@dataclass(frozen=True)
class ConceptQuery:
    concept: str
    kind: str | None = None


class SemanticMemoryV2:
    """One local lookup, two JSON dictionaries, no LLM or legacy L1/L2 reads.

    ``schema`` is an optional authoritative table -> column -> SQL type snapshot
    of the target database. An empty snapshot means no available tables, not union.
    """

    def __init__(self, concepts: dict, tables: dict, *,
                 schema: Mapping[str, Mapping[str, str]] | None = None):
        if concepts.get("version") != "semantic-flat-v2" or tables.get("version") != concepts["version"]:
            raise ValueError("Unsupported semantic memory version")
        if concepts.get("catalog_id") != tables.get("catalog_id"):
            raise ValueError("Concept and table dictionaries belong to different catalogs")
        if concepts.get("environment_id") != tables.get("environment_id"):
            raise ValueError("Concept and table dictionaries belong to different environments")
        self.concepts = deepcopy(concepts["concepts"])
        self.tables = deepcopy(tables["tables"])
        self.catalog_id = concepts["catalog_id"]
        self.environment_id = concepts["environment_id"]
        self.schema = deepcopy(schema)
        self.aliases: dict[str, str] = {}
        for name, item in self.concepts.items():
            for alias in [name, *item["aliases"]]:
                key = _normalize(alias)
                if key in self.aliases and self.aliases[key] != name:
                    raise ValueError(f"Ambiguous concept alias: {alias}")
                self.aliases[key] = name
            seen = set()
            for row in item["fields"]:
                identity = (row["table"], row["field"], row["kind"])
                if identity in seen or row["table"] not in self.tables:
                    raise ValueError(f"Duplicate mapping or unknown table: {identity}")
                if not all(isinstance(row[k], str) and row[k] for k in ("table", "field", "type", "kind", "meaning")):
                    raise ValueError(f"Invalid field mapping: {identity}")
                seen.add(identity)
        for table, brief in self.tables.items():
            if not isinstance(brief.get("description"), str) or not brief["description"]:
                raise ValueError(f"Missing table description: {table}")
            time_field = brief.get("time_field")
            if time_field is not None and not all(time_field.get(k) for k in ("field", "type", "meaning")):
                raise ValueError(f"Invalid time field: {table}")

    @classmethod
    def load(cls, directory: Path = PACKAGE / "excytin", *, schema=None):
        return cls(
            json.loads((directory / "concepts.json").read_text(encoding="utf-8")),
            json.loads((directory / "tables.json").read_text(encoding="utf-8")),
            schema=schema,
        )

    def retrieve(self, *, known: list[ConceptQuery], wanted: list[ConceptQuery],
                 limit: int = 5, offset: int = 0, intent: str = "") -> dict:
        if not known:
            raise ValueError("known must be nonempty")
        if not isinstance(intent, str):
            raise ValueError("intent must be a string")
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset nonnegative")
        resolved = {"known": [], "wanted": []}
        unresolved, ambiguities = [], []
        for role, queries in (("known", known), ("wanted", wanted)):
            seen = set()
            for query in queries:
                name = self.aliases.get(_normalize(query.concept))
                if name is None:
                    name = self.aliases.get(_normalize("Field:" + query.concept))
                if name is None:
                    unresolved.append({"role": role, "concept": query.concept, "reason": "unknown_concept"})
                    continue
                key = (name, query.kind)
                if key in seen:
                    continue
                seen.add(key)
                item = self.concepts[name]
                kinds = sorted({r["kind"] for r in item["fields"]})
                if query.kind is not None and query.kind not in kinds and item.get("kind_policy") != "hint":
                    unresolved.append({"role": role, "concept": name, "reason": "unknown_kind", "kinds": kinds})
                    continue
                if query.kind is None and len(kinds) > 1:
                    ambiguities.append({"role": role, "concept": name, "kinds": kinds})
                hint = query.kind
                if name == "IP" and hint is None:
                    hint = {"sourceip": "source_ip", "loginip": "login_ip", "remoteip": "remote_ip", "localip": "local_ip", "destinationip": "destination_ip"}.get(_normalize(query.concept))
                resolved[role].append((name, hint))

        matches: dict[str, dict] = {}
        for role, requests in resolved.items():
            for index, (name, kind) in enumerate(requests):
                for original in self.concepts[name]["fields"]:
                    if kind is not None and original["kind"] != kind and self.concepts[name].get("kind_policy") != "hint":
                        continue
                    row = deepcopy(original)
                    table, column = row.pop("table"), row["field"]
                    if self.schema is not None:
                        if column not in self.schema.get(table, {}):
                            continue
                        row["type"] = self.schema[table][column]
                    group = matches.setdefault(table, {"fields": [], "known": set(), "wanted": set(), "hint_matches": 0})
                    group[role].add(index)
                    group["hint_matches"] += int(kind is not None and original["kind"] == kind)
                    row.update(concept=name, use=role)
                    group["fields"].append(row)

        candidates = []
        for table, match in matches.items():
            # Do not introduce tables matching only the desired output concept.
            if not match["known"]:
                continue
            complete_known = len(match["known"]) == len(resolved["known"])
            complete_wanted = len(match["wanted"]) == len(resolved["wanted"])
            complete = complete_known and complete_wanted and not unresolved
            brief = self.tables[table]
            time_field = deepcopy(brief["time_field"])
            if self.schema is not None and time_field:
                if time_field["field"] not in self.schema.get(table, {}):
                    time_field = None
                else:
                    time_field["type"] = self.schema[table][time_field["field"]]
            missing = [f"{role}:{name}" for role in ("known", "wanted")
                       for i, (name, _) in enumerate(resolved[role]) if i not in match[role]]
            missing.extend(f"{u['role']}:{u['concept']}" for u in unresolved)
            keywords = [word for word in brief.get("intent_keywords", []) if _matches_intent(word, intent)]
            # Intent leads ranking; a mistaken role hint cannot exclude another table family.
            score = min(len(keywords), 3) * 10 + (brief.get("intent_priority", 0) if keywords else 0)
            candidates.append({
                "table": table, "description": brief["description"],
                "capability": "direct" if complete else "partial",
                "fields": match["fields"], "time_field": time_field,
                "missing": missing, "intent_score": score, "matched_intent": keywords,
                "role_hint_matches": match["hint_matches"],
            })
        # Ranking is explainable, not a claim that the table contains relevant evidence.
        candidates.sort(key=lambda c: (-c["intent_score"], c["capability"] != "direct", len(c["missing"]), -c["role_hint_matches"], c["table"]))
        return {
            "catalog_id": self.catalog_id, "environment_id": self.environment_id,
            "schema_scope": "target_snapshot" if self.schema is not None else "catalog_union",
            "intent": intent, "unresolved": unresolved, "ambiguities": ambiguities,
            "total": len(candidates), "has_more": offset + limit < len(candidates),
            "next_offset": offset + limit if offset + limit < len(candidates) else None,
            "candidates": candidates[offset:offset + limit],
        }


def render_view(result: dict) -> str:
    """Compact prompt projection, with no complete schemas or example log values."""
    lines = [f"SMv2 scope={result['schema_scope']} candidates={result['total']} has_more={str(result['has_more']).lower()}"]
    for item in result["candidates"]:
        lines.append(f"{item['table']} [{item['capability']}; intent={item.get('intent_score', 0)}] {item['description']}")
        lines.append("; ".join(f"{r['use']}:{r['concept']}/{r['kind']}={r['field']}:{r['type']}({r['meaning']})"
                               for r in item["fields"]))
        if item["time_field"]:
            t = item["time_field"]
            lines.append(f"time={t['field']}:{t['type']}({t['meaning']}); 仅在有依据的时间范围内过滤")
        if item["missing"]:
            lines.append("missing=" + ",".join(item["missing"]))
    for category in ("unresolved", "ambiguities"):
        if result[category]:
            lines.append(category + "=" + json.dumps(result[category], ensure_ascii=False, separators=(",", ":")))
    if result["has_more"]:
        lines.append(f"next_offset={result['next_offset']}")
    return "\n".join(lines)


def executor_context(result: dict, table: str) -> dict:
    """Select an already retrieved candidate; no second lookup or disk read.

    Values and justified time ranges stay in the task. No inferred joins/SQL.
    A broad input identifier must be disambiguated before passing to Executor.
    """
    # Ambiguous identifier encodings must be clarified, unlike IP observation roles.
    if any(a["role"] == "known" and a["concept"] in {"Email_ID", "File_Hash"}
           for a in result["ambiguities"]):
        raise ValueError("Resolve input identifier kinds before execution (Email_ID/File_Hash)")
    item = next((c for c in result["candidates"] if c["table"] == table), None)
    if item is None:
        raise ValueError("Select a table from retrieved candidates; retrieve again if needed")
    return deepcopy({"catalog_id": result["catalog_id"], "schema_scope": result["schema_scope"],
                     "unresolved": result["unresolved"], "ambiguities": result["ambiguities"], **item})
