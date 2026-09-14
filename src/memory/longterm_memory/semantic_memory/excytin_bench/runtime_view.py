"""Derive internal retrieval metadata without expanding the stored v2 format.

The public table/field objects each have exactly five keys. Legacy episodic
and procedural algorithms still need semantic categories and join membership;
these are reconstructed in memory, never written back to the snapshot.
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
