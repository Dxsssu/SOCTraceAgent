"""Credential-free stdio facade; database and model credentials stay in parent."""
import json
import os
from urllib.request import Request, urlopen
from fastmcp import FastMCP

mcp = FastMCP("excytin")


def invoke(path: str, payload: dict) -> dict:
    req = Request(os.environ["EXCYTIN_BRIDGE_URL"] + path,
                  data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer " + os.environ["EXCYTIN_BRIDGE_TOKEN"]})
    with urlopen(req, timeout=60) as response:
        return json.load(response)


@mcp.tool()
def execute_sql(sql: str) -> dict:
    """Execute one read-only MySQL query, including SHOW TABLES and DESCRIBE.
    Failed queries also consume the query budget. No queries after submission.
    """
    return invoke("/execute_sql", {"sql": sql})


@mcp.tool()
def submit_answer(answer: str) -> dict:
    """Submit the final answer and finish this investigation. Call exactly once."""
    return invoke("/submit_answer", {"answer": answer})


if __name__ == "__main__":
    mcp.run(show_banner=False)
