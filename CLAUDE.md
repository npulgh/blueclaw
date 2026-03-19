# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Status

**Lynxclaw MVP is complete.** All 4 phases implemented and tested (2026-03-19). 381 unit/integration tests pass, 2 skipped (Windows symlink). E2E test suite exists (`tests/test_e2e_local.py`) with 6/8 passing (2 depend on live API availability).

## What This Project Is

**Lynxclaw** is a lightweight AI agent runtime platform. It runs Anthropic Claude Agents inside hardened Docker containers and connects them to IM platforms (Telegram + Feishu). The guiding principles are: container-as-security-boundary, defense in depth, streaming-first responses, and a codebase small enough to audit.

## Development Commands

```bash
python -m src.main                                              # Start the host process
python -m pytest tests/                                         # Run all tests (381 pass, ~17s)
python -m pytest tests/ --ignore=tests/test_e2e_local.py        # Unit/integration only (no Docker needed)
python -m pytest tests/test_e2e_local.py                        # E2E (requires Docker + API key)
docker build -t lynxclaw-agent:latest -f container/agent-runner/Dockerfile .  # Build agent image
```

## Key Design Documents

- `docs/ARCHITECTURE.md` — component design, security model, data schema, directory structure
- `docs/TASKS.md` — phased task list with acceptance criteria (Phase 1-4 complete, backlog at bottom)
- `docs/adr/` — architecture decision records explaining *why* key decisions were made
- `docs/E2E-TESTING.md` — E2E testing strategy and runbook
- `docs/adr/ADR-005-container-hardening-lessons.md` — container hardening lessons learned

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

## Directory Structure

```
src/
  main.py                 # Host orchestrator (713 LOC, asyncio, graceful shutdown)
  config.py               # YAML + .env config loading
  router.py               # Message routing (idempotency + backpressure)
  container_manager.py    # Container lifecycle (hardened + concurrency)
  ipc.py                  # IPC Watcher (watchdog on_created + on_moved)
  db.py                   # SQLite + migrations, 6 tables, schema v2
  scheduler.py            # Cron task scheduler
  proxy.py                # Network proxy sidecar
  observability.py        # structlog + Prometheus metrics
  types.py                # Global dataclasses
  memory.py               # Agent memory management
  stream_debouncer.py     # Streaming debounce (500ms / 200 chars)
  swarm.py                # Multi-agent swarm coordination
  persistent_container.py # Long-running container mode
  channels/
    registry.py           # Adapter factory/registry + ChannelAdapter ABC
    telegram.py           # aiogram v3 adapter
    feishu.py             # lark-oapi WebSocket adapter
    example_adapter.py    # Mock adapter for testing
  server.py               # FastAPI (webhook + /metrics)
container/agent-runner/
  Dockerfile              # python:3.11-slim, non-root user 1000
  requirements.txt        # claude-agent-sdk + deps
  main.py                 # Agent entry point + hooks + streaming
  ipc_bridge.py           # JSON-RPC file writer (atomic write)
  api_proxy.py            # HTTP proxy for Anthropic-compatible endpoints
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
--tmpfs /tmp:rw,nosuid,size=256m
--tmpfs /home/agent:rw,nosuid,size=64m,uid=1000,gid=1000
--pids-limit 256 --user 1000:1000 --network none --memory 512m --cpus 1.0
```

Key hardening notes (see `docs/adr/ADR-005-container-hardening-lessons.md`):

- `/tmp` tmpfs omits `noexec` — Node.js JIT requires executable memory mappings
- `/home/agent` tmpfs required — Claude Code CLI writes `~/.claude.json` on startup
- All volume mount points must pre-exist in Dockerfile (`mkdir -p`)
- `ANTHROPIC_BASE_URL` must be explicitly forwarded to containers

Mounts: `data/ipc/` (rw), `groups/{group}/` (rw), `groups/` (ro, global), project dir (ro, main group only).

Credentials passed to containers: `ANTHROPIC_API_KEY` (+ `ANTHROPIC_BASE_URL` if set). IM tokens never enter containers.

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
- `send_message` — complete message (also used as fallback when streaming returns empty)
- `schedule_task` / `list_tasks` / `cancel_task` / `read_context`

**Streaming debounce**: flush every 500 ms or 200 characters, whichever comes first.
