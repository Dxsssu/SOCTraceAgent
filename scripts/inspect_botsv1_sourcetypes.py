from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.splunk import BOTSV1_SOURCETYPES, SplunkSearchTool


def main() -> int:
    tool = SplunkSearchTool()
    dataset = "botsv1"
    report: list[dict[str, object]] = []

    print(f"# BOTSv1 Sourcetype Field Inspection\n")
    print(f"dataset={dataset}\n")

    for entry in BOTSV1_SOURCETYPES:
        print(f"## {entry.sourcetype}")
        print(f"- description: {entry.description}")
        print(f"- representative_fields(current): {list(entry.representative_fields)}")
        try:
            discovered = list(tool._get_discovered_fields(dataset, entry.sourcetype))
        except Exception as exc:
            discovered = []
            print(f"- discovered_fields(error): {exc}")
        else:
            print(f"- discovered_fields: {discovered}")
        print("")

        report.append(
            {
                "sourcetype": entry.sourcetype,
                "description": entry.description,
                "representative_fields": list(entry.representative_fields),
                "discovered_fields": discovered,
            }
        )

    output_path = ROOT_DIR / "tests" / "runtime" / "botsv1_sourcetype_fields.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved_report={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
