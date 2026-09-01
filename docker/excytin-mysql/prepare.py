from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = (
    REPOSITORY_ROOT / "data/excytin-bench/mysql/data_anonymized/incidents"
)
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "generated"
INCIDENTS = ("incident_5", "incident_38", "incident_34", "incident_39", "incident_55", "incident_134", "incident_166", "incident_322")
SKIP_TABLES = {"AzureDiagnostics", "LAQueryLogs"}
SEPARATOR = "❖"
QUOTECHAR = '"'


def quote_table(table_name: str) -> str:
    return f"`{table_name}`" if table_name == "Usage" else table_name


def read_schema(meta_path: Path | None, csv_path: Path) -> dict[str, str]:
    if meta_path is not None and meta_path.exists():
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload:
            raise ValueError(f"Invalid schema metadata: {meta_path}")
        return {str(name): str(data_type) for name, data_type in payload.items()}

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, delimiter=SEPARATOR, quotechar=QUOTECHAR)
        columns = next(reader)
    if not columns:
        raise ValueError(f"Could not infer columns from {csv_path}")
    return {column: "string" for column in columns}


def table_sources(incident_dir: Path) -> Iterable[tuple[str, dict[str, str], list[Path]]]:
    for entry in sorted(incident_dir.iterdir(), key=lambda path: path.name):
        if entry.name.startswith(("._", ".DS_Store")):
            continue
        if entry.is_file() and entry.suffix == ".csv":
            table_name = entry.stem
            if table_name in SKIP_TABLES:
                continue
            yield table_name, read_schema(entry.with_suffix(".meta"), entry), [entry]
        elif entry.is_dir():
            table_name = entry.name
            if table_name in SKIP_TABLES:
                continue
            csv_files = sorted(
                path for path in entry.glob("*.csv") if not path.name.startswith("._")
            )
            if not csv_files:
                continue
            meta_candidates = sorted(
                path for path in entry.glob("*.meta") if not path.name.startswith("._")
            )
            meta_path = meta_candidates[0] if meta_candidates else None
            yield table_name, read_schema(meta_path, csv_files[0]), csv_files


def create_table_statement(table_name: str, schema: dict[str, str]) -> str:
    # This intentionally follows the upstream benchmark: all imported columns
    # are TEXT so that generated SQL sees the same permissive MySQL behavior.
    columns = ",\n    ".join(f"{name} TEXT" for name in schema)
    return f"CREATE TABLE {quote_table(table_name)} (\n    {columns}\n);"


def load_statement(incident_dir: Path, table_name: str, csv_path: Path) -> str:
    relative_path = csv_path.relative_to(incident_dir).as_posix()
    return f"""LOAD DATA INFILE '/var/lib/mysql-files/{relative_path}'
INTO TABLE {quote_table(table_name)}
FIELDS TERMINATED BY '{SEPARATOR}'
ENCLOSED BY '{QUOTECHAR}'
LINES TERMINATED BY '\\n'
IGNORE 1 ROWS;"""


def build_sql(incident_dir: Path, database_name: str) -> tuple[str, int]:
    statements = [
        "CREATE USER IF NOT EXISTS 'admin'@'%' IDENTIFIED BY 'admin';",
        "GRANT ALL PRIVILEGES ON *.* TO 'admin'@'%';",
        "FLUSH PRIVILEGES;",
        f"CREATE DATABASE IF NOT EXISTS {database_name};",
        f"USE {database_name};",
    ]
    table_count = 0
    for table_name, schema, csv_files in table_sources(incident_dir):
        statements.append(create_table_statement(table_name, schema))
        statements.extend(
            load_statement(incident_dir, table_name, csv_path) for csv_path in csv_files
        )
        table_count += 1
    statements.extend(
        [
            "CREATE DATABASE IF NOT EXISTS socagent_control;",
            "CREATE TABLE IF NOT EXISTS socagent_control.import_status (status VARCHAR(32) PRIMARY KEY);",
            "REPLACE INTO socagent_control.import_status (status) VALUES ('complete');",
        ]
    )
    return "\n\n".join(statements) + "\n", table_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ExCyTIn-Bench MySQL initialization SQL.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--database", default="env_monitor_db")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_root = args.data_root.resolve()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    for incident in INCIDENTS:
        incident_dir = data_root / incident
        if not incident_dir.is_dir():
            raise FileNotFoundError(f"Missing extracted incident directory: {incident_dir}")
        sql, table_count = build_sql(incident_dir, args.database)
        output = output_root / f"{incident}.sql"
        output.write_text(sql, encoding="utf-8")
        print(f"Wrote {output} with {table_count} tables")


if __name__ == "__main__":
    main()
