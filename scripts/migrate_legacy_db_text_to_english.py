from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "socagent.db"
BACKUP_PATH = ROOT / "data" / "socagent.pre_english_migration.db"

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


REPLACEMENTS: list[tuple[str, str]] = [
    ("2016-08-19 05:10:00，外部 IP ", "2016-08-19 05:10:00, external IP "),
    (" 持续访问 Wayne Enterprises 对外站点 ", " continuously accessed the Wayne Enterprises public-facing site "),
    ("随后出现针对 Joomla 后台的异常 POST 请求、可执行文件上传以及页面被篡改迹象", "followed by abnormal POST requests to the Joomla admin interface, executable file uploads, and signs of page defacement"),
    ("2.2.2.2收到Ddos攻击", "2.2.2.2 received a DDoS attack alert"),
    ("2.2.2.2受到ddos攻击", "2.2.2.2 was hit by a DDoS attack"),
    ("系统创建了安全事件: ", "The system created a security event: "),
    ("执行意图：", "Execution intent: "),
    ("外部 IP ", "External IP "),
    ("目标IP", "target IP"),
    ("源IP", "source IP"),
    ("对外站点", "public-facing site"),
    ("异常 POST 请求", "abnormal POST requests"),
    ("可执行文件上传", "executable file uploads"),
    ("页面被篡改迹象", "signs of page defacement"),
    ("页面被篡改", "page defacement"),
    ("威胁情报", "threat intelligence"),
    ("基础情报", "basic intelligence"),
    ("后续活动", "follow-on activity"),
    ("相关日志", "related logs"),
    ("文件上传", "file upload"),
    ("目标服务器", "target server"),
    ("时间窗", "time window"),
    ("轮次", "round"),
    ("本轮", "this round"),
    ("执行完成", "completed"),
    ("执行成功", "completed successfully"),
    ("执行失败", "failed"),
    ("误报", "false positive"),
    ("需进一步调查", "requires further investigation"),
    ("未找到任何匹配日志", "no matching logs were found"),
    ("未找到匹配日志", "no matching logs were found"),
    ("未命中相关日志", "did not match related logs"),
    ("未见明确恶意命中", "no clear malicious hit was found"),
    ("未完成", "not completed"),
    ("继续执行", "continue executing"),
    ("继续推进", "continue advancing"),
    ("总结", "summary"),
    ("结论", "conclusion"),
    ("建议", "recommendation"),
    ("问题", "question"),
    ("方向", "direction"),
    ("查询", "query"),
    ("整合", "consolidate"),
    ("确认", "confirm"),
    ("判断", "determine"),
    ("分析", "analyze"),
    ("检查", "check"),
    ("统计", "count"),
    ("攻击", "attack"),
    ("告警", "alert"),
    ("日志", "logs"),
    ("上传", "upload"),
    ("活动", "activity"),
    ("服务", "service"),
    ("类型", "type"),
    ("流量", "traffic"),
    ("验证", "validate"),
    ("返回了", "returned"),
    ("返回结果", "returned result"),
]


def translate_text(text: str) -> str:
    if not text or not CJK_RE.search(text):
        return text

    translated = text

    for src, dst in sorted(REPLACEMENTS, key=lambda item: len(item[0]), reverse=True):
        translated = translated.replace(src, dst)

    translated = re.sub(r"## 第 ?(\d+) ?轮执行总结", r"## Round \1 Execution Summary", translated)
    translated = re.sub(r"### 第 ?(\d+) ?轮执行总结", r"### Round \1 Execution Summary", translated)
    translated = re.sub(r"### 轮次总结 ?\(Round ?(\d+)\)", r"### Round \1 Summary", translated)
    translated = re.sub(r"### 第(\d+)轮执行总结", r"### Round \1 Execution Summary", translated)
    translated = re.sub(r"## 第(\d+)轮执行总结", r"## Round \1 Execution Summary", translated)
    translated = re.sub(r"### 本轮工具执行结果", "### Tool Execution Results This Round", translated)
    translated = re.sub(r"### 本轮执行结果", "### Execution Results This Round", translated)
    translated = re.sub(r"## 本轮执行总结", "## Execution Summary This Round", translated)
    translated = re.sub(r"\*\*本轮执行概要\*\*", "**Execution Overview This Round**", translated)
    translated = re.sub(r"\*\*当前证据状态\*\*", "**Current Evidence Status**", translated)
    translated = re.sub(r"\*\*执行结果：\*\*", "**Execution Result:**", translated)
    translated = re.sub(r"### ✅ 已收集结论", "### Collected Conclusions", translated)
    translated = re.sub(r"### 当前发现", "### Current Findings", translated)
    translated = re.sub(r"### 本轮执行结果", "### Results This Round", translated)
    translated = re.sub(r"### 本轮执行结果", "### Results This Round", translated)
    translated = re.sub(r"### 本轮执行结果", "### Results This Round", translated)
    translated = re.sub(r"### 本轮执行结果", "### Results This Round", translated)
    translated = re.sub(r"问题(\d+\.\d+)：", r"Question \1: ", translated)
    translated = re.sub(r"方向(\d+)：", r"Direction \1: ", translated)
    translated = re.sub(r"节点 ?(\d+(?:-\d+)*)", r"Node \1", translated)
    translated = re.sub(r"第 ?(\d+) ?轮", r"Round \1", translated)

    translated = translated.replace("使用log_searchquery", "Use log_search to query")
    translated = translated.replace("使用log_search", "Use log_search")
    translated = translated.replace("通过 VirusTotal query", "Use VirusTotal to query")
    translated = translated.replace("通过 ipinfo query", "Use ipinfo to query")
    translated = translated.replace("querySplunk", "Query Splunk")
    translated = translated.replace("query Splunk", "Query Splunk")

    translated = re.sub(r"\s+", " ", translated).strip()
    translated = translated.replace(" .", ".").replace(" ,", ",").replace(" ：", ":")
    translated = translated.replace("（", "(").replace("）", ")").replace("，", ", ").replace("。", ". ")
    translated = re.sub(r"\s+\.", ".", translated)
    translated = re.sub(r"\s+,", ",", translated)
    translated = re.sub(r"\.\s+\.", ".", translated)
    translated = re.sub(r"\s{2,}", " ", translated).strip()

    if CJK_RE.search(translated):
        translated = re.sub(CJK_RE, "", translated)
        translated = translated.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
        translated = re.sub(r"\s{2,}", " ", translated).strip()
        if not translated:
            translated = "Legacy investigation content migrated to English."

    return translated


def translate_json_payload(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return translate_text(value)
    translated = translate_object(parsed)
    return json.dumps(translated, ensure_ascii=False, sort_keys=True)


def translate_object(obj: Any) -> Any:
    if isinstance(obj, str):
        return translate_text(obj)
    if isinstance(obj, list):
        return [translate_object(item) for item in obj]
    if isinstance(obj, dict):
        return {key: translate_object(value) for key, value in obj.items()}
    return obj


def migrate_database() -> None:
    if not BACKUP_PATH.exists():
        shutil.copy2(DB_PATH, BACKUP_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN")

        events = conn.execute("SELECT event_id, message, context_json FROM events").fetchall()
        for event_id, message, context_json in events:
            conn.execute(
                "UPDATE events SET message = ?, context_json = ? WHERE event_id = ?",
                (translate_text(message), translate_json_payload(context_json), event_id),
            )

        executions = conn.execute(
            "SELECT execution_id, node_title, tool_input_json, result_json, error_message FROM executions"
        ).fetchall()
        for execution_id, node_title, tool_input_json, result_json, error_message in executions:
            conn.execute(
                """
                UPDATE executions
                SET node_title = ?, tool_input_json = ?, result_json = ?, error_message = ?
                WHERE execution_id = ?
                """,
                (
                    translate_text(node_title),
                    translate_json_payload(tool_input_json),
                    translate_json_payload(result_json),
                    translate_text(error_message),
                    execution_id,
                ),
            )

        reviews = conn.execute(
            "SELECT review_id, findings_json, gaps_json, recommendations_json, summary_text FROM round_reviews"
        ).fetchall()
        for review_id, findings_json, gaps_json, recommendations_json, summary_text in reviews:
            conn.execute(
                """
                UPDATE round_reviews
                SET findings_json = ?, gaps_json = ?, recommendations_json = ?, summary_text = ?
                WHERE review_id = ?
                """,
                (
                    translate_json_payload(findings_json),
                    translate_json_payload(gaps_json),
                    translate_json_payload(recommendations_json),
                    translate_text(summary_text),
                    review_id,
                ),
            )

        snapshots = conn.execute("SELECT id, tree_json FROM ttt_snapshots").fetchall()
        for snapshot_id, tree_json in snapshots:
            conn.execute(
                "UPDATE ttt_snapshots SET tree_json = ? WHERE id = ?",
                (translate_json_payload(tree_json), snapshot_id),
            )

        messages = conn.execute("SELECT message_id, payload_json FROM messages").fetchall()
        for message_id, payload_json in messages:
            conn.execute(
                "UPDATE messages SET payload_json = ? WHERE message_id = ?",
                (translate_json_payload(payload_json), message_id),
            )

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate_database()
    print(DB_PATH)
