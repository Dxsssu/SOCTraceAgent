# SOCAgent

SOCAgent is a three-role multi-agent prototype for security alert traceback. The current version uses local `SQLite` as a shared state hub. `Planner`, `Executor`, and `Reviewer` collaborate on the same event across multiple rounds and progressively produce a `TTT` (Traceback Task Tree), execution records, and round reviews.

The project is currently closer to a runnable architecture skeleton than a finished product:

- Clear multi-role state transitions
- A persistent shared blackboard plus structured message history
- A working Web home page and war room UI
- Real-time event updates through Socket.IO
- An OpenAI-compatible LLM wrapper
- A standalone multi-round smoke test
- Real external security tools are only partially integrated

## Goal

This repository validates a minimal closed loop:

1. `Planner` receives an alert and initializes a `TTT`
2. `Executor` claims an `L3` leaf node from the `TTT` and runs a tool
3. `Reviewer` summarizes the current round after an `Execution` completes and writes a `RoundReview`
4. `Planner` updates the next-round `TTT` from the `RoundReview`
5. If open leaf nodes remain, the next round starts; otherwise the event is completed

Core workflow:

```text
Event -> TTT -> Execution -> RoundReview -> TTT
```

Structured messages are used for auditing, debugging, and UI presentation. The source of truth is always the persistent SQLite state.

## Architecture

### Roles

- `Planner`
  - Reads new alerts
  - Performs initial analysis and matches procedural memory
  - Builds the first `TTT`
  - Updates the next-round `TTT` after each `RoundReview`

- `Executor`
  - Claims one executable `L3` leaf node from the current `TTT`
  - Selects a tool and creates an `Execution`
  - Advances the leaf node to `done` or `n/a`
  - Hands control to `Reviewer` after each execution

- `Reviewer`
  - Summarizes the round from current `Execution` records
  - Identifies validated conclusions, evidence gaps, and capability gaps
  - Writes a `RoundReview` and moves the event back to `replanning`

### Shared state objects

- `Event`
  - The main event object, including current round and overall status
- `TracebackTaskTree`
  - Versioned task tree snapshots
  - Strictly limited to `L1 -> L2 -> L3`
- `Execution`
  - One execution record for one `L3` leaf node
- `RoundReview`
  - The review summary after each round
- `MessageEnvelope`
  - Structured audit/debug messages

### Event status

```text
pending -> planned -> executing -> reviewing -> replanning -> planned/completed
```

- `pending`: a new event waiting for `Planner`
- `planned`: a `TTT` exists and has executable leaves
- `executing`: `Executor` is handling a leaf node
- `reviewing`: new execution results are waiting for `Reviewer`
- `replanning`: `Reviewer` has written a summary and `Planner` needs to update the next round
- `completed`: no open leaf nodes remain

## TTT constraints

Current `Planner` output must follow these rules:

- The `TTT` must be a full snapshot, not an incremental patch
- It must have exactly three levels
- `L1` represents an independent investigation direction
- `L2` represents a question that must be answered under one `L1`
- `L3` represents a concrete query or evidence-gathering action
- `node_id` uses numeric hierarchical IDs such as `1`, `1-2`, `1-2-3`
- `Executor` only consumes `L3` leaf nodes

Node statuses:

- `todo`
- `in_progress`
- `done`
- `n/a`

## Long-term memory

The long-term memory base lives under `src/memory/longterm_memory/`.

- `procedural_memory/`
  - Investigation workflow memory for different event types, usually organized directly as `L1/L2/L3`
- `factual_memory/`
  - Enterprise background, data-environment facts, and investigation constraints
- Both use `YAML front matter + Markdown body`
- Before initializing the first `TTT`, `Planner` reads all procedural-memory summaries
- The LLM then selects the best matching workflow memory as context
- All factual-memory documents and the registered MCP/tool capability list are also injected

Initial `TTT` context therefore includes the event, initial analysis, procedural memory, factual memory, and available tools.

## Repository layout

```text
SOCAgent/
├─ main.py
├─ pyproject.toml
├─ src/
│  ├─ agent/
│  ├─ memory/
│  ├─ messaging/
│  ├─ schema/
│  ├─ storage/
│  ├─ tools/
│  └─ webapp/
├─ tests/
└─ data/
```

## Environment

### Python

`pyproject.toml` currently requires:

- Python `>= 3.13`

### Dependencies

- `flask`
- `flask-socketio`
- `openai`
- `python-dotenv`
- `pyyaml`

Recommended install:

```bash
uv sync
```

Or:

```bash
pip install flask flask-socketio openai python-dotenv pyyaml
```

## Environment variables

The project reads configuration from `.env`:

```env
DEEPSEEK_API_KEY=your_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_REASONING_EFFORT=high
DEEPSEEK_THINKING_ENABLED=true
SOCAGENT_DB_PATH=data/socagent.db
SOCAGENT_POLL_INTERVAL=5
SOCAGENT_WEB_HOST=127.0.0.1
SOCAGENT_WEB_PORT=5008
SPLUNK_USERNAME=admin
SPLUNK_PASSWORD=changeme
SPLUNK_VERIFY_TLS=false
SPLUNK_DEFAULT_DATASET=botsv1
SPLUNK_BOTSV1_BASE_URL=http://127.0.0.1:8000
SPLUNK_BOTSV1_INDEX=botsv1
SPLUNK_BOTSV2_BASE_URL=http://127.0.0.1:8020
SPLUNK_BOTSV2_INDEX=botsv2
SPLUNK_BOTSV3_BASE_URL=http://127.0.0.1:8030
SPLUNK_BOTSV3_INDEX=botsv3
```

Notes:

- `DEEPSEEK_*`
  - Used by `src/agent/llm.py`
  - The app uses an OpenAI-compatible API surface
- `SOCAGENT_DB_PATH`
  - Shared SQLite file path for all roles
- `SOCAGENT_POLL_INTERVAL`
  - Poll interval for role runtimes in seconds
- `SOCAGENT_WEB_HOST` / `SOCAGENT_WEB_PORT`
  - Host and port for the Web UI
- `SPLUNK_*`
  - Used by `src/tools/splunk.py`
  - Configures Splunk Docker credentials, default dataset, and port mappings

Security notes:

- Do not keep real keys in the repository `.env`
- Rotate any real key that has already been committed
- `Planner` and `Reviewer` now require a working LLM; if `DEEPSEEK_API_KEY` is missing or the model returns invalid YAML, the event will be marked `failed`

## Startup

### 1. Initialize the database

```bash
python main.py -init-db
```

This creates:

- `events`
- `executions`
- `round_reviews`
- `ttt_snapshots`
- `messages`

### 2. Start the Web UI

```bash
python main.py -web
```

Default address:

```text
http://127.0.0.1:5008
```

The home page supports:

- Creating events
- Viewing the event list
- Opening a single-event war room

The war room shows:

- Event details
- Real-time message flow
- Current round
- Execution records
- RoundReview results
- TTT / Execution / Review hierarchy

### 3. Start the three roles

The current system is not a single-process scheduler. It uses three independent processes polling the same SQLite file.

Open three terminals:

```bash
python main.py -role planner
python main.py -role executor
python main.py -role reviewer
```

Supported role names also include:

- `_planner`
- `_executor`
- `_reviewer`

### 4. Create an event

You can create events in multiple ways:

- Submit the form on the home page
- Use the test script
- Call `SQLiteStorage.save_event(...)` in your own script

### 5. Recommended local setup

Recommended four-terminal workflow:

```bash
python main.py -web
python main.py -role planner
python main.py -role executor
python main.py -role reviewer
```

Then open the browser home page, create an event, and watch the loop progress in the war room.

## Validation

The easiest validation entry point is the smoke test:

```bash
python tests/test_multi_agent_loop.py
```

It will:

1. Create a test-specific SQLite file
2. Start `Planner / Executor / Reviewer`
3. Insert a test event directly
4. Poll `Event / TTT / Execution / RoundReview / Message`
5. Check whether the multi-round loop closes successfully

Common examples:

```bash
python tests/test_multi_agent_loop.py --timeout 240 --target-rounds 2
python tests/test_multi_agent_loop.py --show-process-logs
python tests/test_multi_agent_loop.py --db-path tests/runtime/test_multi_agent_loop.db
```

## Current implementation details

### Storage

All current state lives in one SQLite file:

- `events`
- `executions`
- `round_reviews`
- `ttt_snapshots`
- `messages`

Responsibilities:

- `src/storage/sqlite.py` stores `Event / Execution / RoundReview`
- `src/memory/working_memory/ttt_store.py` stores `TTT`
- `src/messaging/bus.py` stores structured message history

### Web layer

`src/webapp/server.py` provides a minimal Web layer inspired by `deepsoc`:

- The home page uses HTTP APIs to create events and fetch the event list
- The war room uses HTTP for initial data:
  - event details
  - message stream
  - execution records
  - round reviews
  - round hierarchy
- The page joins a Socket.IO room with `join(event_id)`
- The backend watches SQLite changes and pushes new messages and state changes to the war room in real time

The UI therefore consumes real project state rather than front-end mock data.

### LLM calls

`src/agent/llm.py` uses the `OpenAI` Python SDK against an OpenAI-compatible API while keeping `DEEPSEEK_*` environment-variable names. If you switch providers, you mainly need to change `base_url`, `model`, and `api_key`.

### Tool execution

`Executor` already integrates real Splunk MCP-style tooling:

- `log_search` lets the LLM choose a tool from a routing step based on the `TTT` leaf intent
- Once selected, `src/tools/splunk.py` translates the investigation intent into query specs and SPL
- The tool then runs a real query through the Splunk REST API and returns a summary plus sample events

`src/tools` currently registers multiple MCP-style tool providers, including `splunk`, `ipinfo`, and `virustotal`. `Executor` no longer relies on hard-coded title keywords alone.

### Minimal Splunk examples

Structured query:

```python
from src.tools import SplunkSearchTool

tool = SplunkSearchTool()
result = tool.search(
    spec={
        "dataset": "botsv1",
        "sourcetype": "WinEventLog:Security",
        "keywords": ["failed login"],
        "ip": "11.22.33.44",
        "limit": 5,
        "fields": ["_time", "host", "user", "src"],
    }
)
```

Natural-language investigation intent:

```python
from src.tools import SplunkSearchTool

tool = SplunkSearchTool()
spec = tool.interpret_intent(
    "Search failed login logs in botsv1 related to 11.22.33.44",
    dataset="botsv1",
)
query = tool.build_query(spec)
result = tool.search(spec=spec)
```

## Implemented capabilities

- Clear separation across three role runtimes
- Cross-process shared state through SQLite
- Web home page and war room UI
- Real-time message updates through Socket.IO
- Versioned TTT snapshots
- Leaf claiming and status advancement
- Persistent execution records
- Persistent round reviews
- Structured audit messages
- Standalone multi-round smoke testing
- Explicit LLM validation and failure persistence

## Current limitations

- External tool coverage is still limited
- No unified supervisor for the three role processes
- SQLite is suitable for a single-machine prototype, not distributed deployment
- Some tool-selection logic is still a placeholder
- The smoke test is a terminal script, not a full pytest suite
- The Web layer has no authentication, authorization, or session management
- Real-time updates still depend on a local SQLite watcher instead of a queue/event-stream architecture

## Next steps

- Add richer log search, asset lookup, and threat-intelligence tools for `Executor`
- Strengthen `Planner` / `Reviewer` output constraints and validation
- Add deeper TTT visualization and node drill-down in the war room
- Add database snapshot inspection and replay tools
- Expand standardized test coverage beyond smoke tests
- Replace or extend the current watcher with a queue/event-stream architecture if stronger real-time delivery is needed
