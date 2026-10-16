"""Derive compatibility retrieval metadata from the L1/L2 read model.

This in-memory view supports offline PM/EM builders and legacy lexical
retrieval. The online SM selector reads L1 and one exact L2 card directly.
"""

from typing import Any

from .build_knowledge import classify_table, infer_semantic_type


def retrieval_view(knowledge: dict[str, Any]) -> dict[str, Any]:
    membership = {
        field_id: key["name"]
        for key in knowledge.get("join_keys", [])
        for field_id in key.get("field_ids", [])
    }
    tables = []
    for table in knowledge["tables"]:
        fields = []
        for field in table.get("field", table.get("fields", [])):
            semantic_type = infer_semantic_type(field["name"], field.get("data_type", "string"))
            description = field.get("description", field.get("description_en", ""))
            fields.append({
                **field,
                "description": description,
                "semantic_type": semantic_type,
                "join_key": membership.get(field["field_id"]),
                "is_identifier": field["field_id"] in membership,
                "is_time_field": semantic_type == "TIMESTAMP",
                "semantic_text": " ".join([field["name"], description, field.get("description_zh", "")]),
            })
        description = table.get("description", table.get("description_en", ""))
        tables.append({
            **table,
            "description": description,
            "log_type": classify_table(table["name"]),
            "fields": fields,
            "semantic_text": " ".join([table["name"], description, table.get("description_zh", "")]),
        })
    return {**knowledge, "tables": tables}
