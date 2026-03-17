# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Blueclaw is in the design/planning phase.** No source code exists yet. The repository contains only design documents and research notes. Implementation starts from T1.1 in `docs/TASKS.md`.

## What This Project Is

**Blueclaw** is a lightweight AI agent runtime platform. It runs Anthropic Claude Agents inside hardened Docker containers and connects them to IM platforms (Telegram + Feishu). The guiding principles are: container-as-security-boundary, defense in depth, streaming-first responses, and a codebase small enough to audit (≤ 5,000 lines).

## Key Design Documents

Read these before implementing anything:

- `docs/ARCHITECTURE.md` — component design, security model, data schema, directory structure, config schema. **Start here.**
- `docs/TASKS.md` — phased task list with acceptance criteria and inter-task dependencies. Each task is the unit of work.
- `docs/adr/` — architecture decision records explaining *why* key decisions were made.
- `notes/nanoclaw-architecture.md` — analysis of NanoClaw (TypeScript), the reference implementation Blueclaw improves upon.

## Development Workflow

Tasks are defined in `docs/TASKS.md` and must be done sequentially within each phase (dependencies are marked `depends:`). Do one task at a time, run its acceptance tests, then move to the next.

**Once the project scaffold exists**, the expected commands will be:
```bash
python -m src.main           # Start the host process
python -m pytest tests/      # Run all tests
python -m pytest tests/test_db.py   # Run a single test file
docker build -t blueclaw-agent:latest container/agent-runner/   # Build agent image
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.11+ / asyncio |
| AI SDK | `claude-agent-sdk` (hooks, resume, MCP) |
| Containers | Docker Engine |
| Database | SQLite + `aiosqlite` |
| Telegram | `aiogram` v3 (Long Polling) |
| Feishu | `lark-oapi` (WebSocket) |
| File watch | `watchdog` |
| Logging | `structlog` (JSON) |
| Config | `pyyaml` + `python-dotenv` |
| HTTP (optional) | `fastapi` + `uvicorn` |
| Scheduling | `croniter` + asyncio |

## Planned Directory Structure

```
src/
  main.py                 # Host orchestrator (asyncio, graceful shutdown)
  config.py               # YAML + .env config loading
  router.py               # Message routing (idempotency + backpressure)
  container_manager.py    # Container lifecycle (hardened + concurrency)
  ipc.py                  # IPC Watcher (watchdog-based, streaming dispatch)
  db.py                   # SQLite + migrations
  scheduler.py            # Cron task scheduler
  proxy.py                # Network proxy sidecar
  observability.py        # structlog + Prometheus metrics
  types.py                # Global dataclasses
  channels/
    registry.py           # Adapter factory/registry
    telegram.py           # aiogram v3 adapter
    feishu.py             # lark-oapi adapter
  server.py               # FastAPI (webhook + /metrics, optional)
container/agent-runner/
  Dockerfile              # python:3.11-slim, non-root user 1000
  requirements.txt        # claude-agent-sdk + deps
  main.py                 # Agent entry point + hooks
  ipc_bridge.py           # MCP stdio server → IPC files
groups/
  CLAUDE.md               # Global memory (all groups read-only; main group can write)
  {group-name}/CLAUDE.md  # Group-specific memory (agent read/write)
data/
  store/messages.db
  ipc/{group}/outbox/ inbox/ audit/
  audit_log.jsonl
```

## Architecture Constraints (DO NOT CHANGE without updating the corresponding ADR)

| Decision | ADR |
|----------|-----|
| IPC uses filesystem JSON-RPC (not gRPC/sockets) | `docs/adr/001-file-ipc.md` |
| Telegram uses `aiogram` v3 (not `python-telegram-bot`) | `docs/adr/002-aiogram.md` |
| Long-connection preferred over webhook | `docs/adr/003-long-connection.md` |
| Network access via Proxy Sidecar (not `--network bridge`) | `docs/adr/004-proxy-sidecar.md` |
| Default Ephemeral containers + optional Resumable (not Persistent) | `docs/adr/005-resumable-containers.md` |
| Database is SQLite (not PostgreSQL) | Design philosophy — zero deployment deps |
| Container hardening flags (`--cap-drop ALL`, etc.) must not be reduced | `docs/ARCHITECTURE.md §3.4.2` |

## Container Security Baseline

Every agent container must be launched with these flags (defined in `container_manager.py`):
```bash
--cap-drop ALL --security-opt no-new-privileges:true --read-only
--tmpfs /tmp:rw,noexec,nosuid,size=256m --pids-limit 256
--user 1000:1000 --network none --memory 512m --cpus 1.0
```

Mounts: `groups/{group}/` (rw), `groups/` (ro, global), project dir (ro, main group only), `data/ipc/{group}/` (rw).

Credentials passed to containers: `ANTHROPIC_API_KEY` only. IM tokens never enter containers.

## Key Interfaces

**ChannelAdapter ABC** (`src/channels/registry.py`):
```python
async def init(config) / start() / stop()
def on_message(handler)
async def send_message(chat_id, content) -> str   # returns platform msg_id
async def edit_message(chat_id, msg_id, content)  # for streaming updates
def capabilities() -> ChannelCapabilities
```

**IPC methods** (container → host via outbox files):
- `stream_chunk` — streaming text chunk (has `is_final` flag)
- `send_message` — complete message
- `schedule_task` / `list_tasks` / `cancel_task` / `read_context`

**Streaming debounce**: flush every 500 ms or 200 characters, whichever comes first.
