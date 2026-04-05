# Lynxclaw

Lightweight AI agent runtime platform. Run AI agents securely in Docker containers, interacting via Telegram / Feishu (Lark).

[![CI](https://github.com/lynxpurr/lynxclaw/actions/workflows/ci.yml/badge.svg)](https://github.com/lynxpurr/lynxclaw/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

## Design Philosophy

| Principle | Description |
|-----------|-------------|
| **Container as Security Boundary** | Agents run in minimal-privilege Docker containers with OS-level isolation |
| **Defense in Depth** | Container isolation + network proxy + tool hooks + mount allowlist, layered protection |
| **Streaming-First** | Agent responses stream to IM in real-time, no waiting for full response |
| **Long-Connection Preferred** | Feishu WebSocket / Telegram Long Polling as default, Webhook as optional |
| **Small & Auditable** | Core codebase ≤5,000 lines |
| **IM-Native** | Telegram + Feishu as first-class citizens, no generic gateway |
| **IPC Decoupling** | Host and containers communicate via filesystem JSON-RPC, no SDK version coupling |

## Architecture Overview

```
Telegram / Feishu
      ↓
  Channel Adapter
      ↓
  Message Router  ──→  SQLite (messages, groups, sessions, tasks)
      ↓
Container Manager  (asyncio.Semaphore, max 5 concurrent)
      ↓
Docker Container  (--cap-drop ALL / --read-only / --network none)
      │
  Agent Runner  (Claude Agent SDK + PreToolUse/PostToolUse hooks)
      │
  IPC Bridge  (MCP stdio → /workspace/ipc/outbox/ files)
      ↓
  IPC Watcher  (watchdog event-driven)
      ↓
  Channel send_message / edit_message (streaming updates)
```

## Quick Start

### Prerequisites

- Python 3.11+
- Docker Engine
- Anthropic API Key (or compatible endpoint like Kimi K2)
- (Optional) Telegram Bot Token / Feishu App Credentials

### Installation

```bash
git clone https://github.com/lynxpurr/lynxclaw.git
cd lynxclaw

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# Install dependencies
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env, fill in ANTHROPIC_API_KEY etc.
```

### Build Agent Image

All runtime paths require the Agent container image:

```bash
docker build -t lynxclaw-agent:latest -f container/agent-runner/Dockerfile .
```

---

### Path A: Telegram Bot (Recommended)

**Step 1** — Create Bot: Message `@BotFather` on Telegram, send `/newbot`, get the token.

**Step 2** — Configure `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=your_token
```

Also supports Anthropic-compatible endpoints (e.g., Kimi K2):

```
# Kimi K2
ANTHROPIC_API_KEY=sk-kimi-...
ANTHROPIC_BASE_URL=https://api.kimi.com/coding/v1
```

> **Note**: `ANTHROPIC_BASE_URL` should NOT end with `/v1` (CLI appends automatically).

**Step 3** — Get `chat_id`. Send any message to your bot, then open in browser:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Find `result[0].message.chat.id` in the JSON. Private chats are positive (e.g., `7994661284`), groups are negative (e.g., `-1001234567890`).

**Step 4** — Enable Telegram + configure group, edit `lynxclaw.config.yaml`:

```yaml
telegram:
  enabled: true
  mode: polling

groups:
  - name: main
    channel: telegram
    chat_id: "your_chat_id"   # From Step 3
    is_main: true
    trigger: ""               # Private chat: empty (all messages trigger); Group: use "@bot"
    token_budget: 0
```

> **trigger**: `trigger: "@bot"` requires messages to start with `@bot`. Private chat recommended ` ""` (empty) to trigger on all messages.

**Step 5** — Start:

```bash
python -m src.main
```

**Step 6** — Try it:

Send `@bot hello` in the Telegram group (or private chat), observe:

- Terminal logs: message routing → container start → Agent API call → IPC response
- Bot streaming reply: sends a message first, then continuously edits to update content
- Data persistence: check `data/store/messages.db` for message records and token usage

---

### Path B: Local E2E (No IM token needed)

Run the full pipeline with built-in `ExampleAdapter` + real Docker + real API:

```bash
# Ensure API key is valid in .env
python -m pytest tests/test_e2e_local.py -v -x
```

This runs the complete flow: inject message → Router → start container → Agent API call → IPC write file → Watcher callback → Adapter receives response.

---

### Path C: Docker Compose One-Command Deploy

```bash
docker compose up -d --build
```

Requires `.env` and `lynxclaw.config.yaml` configured. See [docker-compose.yml](docker-compose.yml).

---

## Running Tests

```bash
python -m pytest tests/                                         # All tests (417 pass, ~22s)
python -m pytest tests/ --ignore=tests/test_e2e_local.py        # Unit/integration only (no Docker)
python -m pytest tests/test_e2e_local.py                        # E2E (requires Docker + API key)
```

## Configuration

Main config file: `lynxclaw.config.yaml` (see [docs/ARCHITECTURE.md §8](docs/ARCHITECTURE.md))

```yaml
container:
  lifecycle: ephemeral    # ephemeral | resumable
  max_concurrent: 5

telegram:
  enabled: true
  mode: polling           # polling | webhook

feishu:
  enabled: false
  mode: websocket         # websocket | webhook
```

Sensitive credentials go in `.env` (not in config file):

```
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
FEISHU_APP_ID=...
FEISHU_APP_SECRET=...
```

## Documentation

| Document | Content |
|----------|---------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture, component design, security model, data schema |
| [docs/TASKS.md](docs/TASKS.md) | Phased development task list and acceptance criteria |
| [docs/adr/](docs/adr/) | Architecture Decision Records (why we chose this) |

## Security Model

Each Agent call runs in an isolated Docker container with hardening flags:

- `--cap-drop ALL` — Remove all Linux Capabilities
- `--read-only` — Read-only root filesystem
- `--network none` — No network by default (networking via Proxy Sidecar)
- `--user 1000:1000` — Non-root user
- `--pids-limit 256` — Process limit
- `--memory 512m --cpus 1.0` — Resource limits

**IM credentials never enter containers**, only `ANTHROPIC_API_KEY` is injected via environment variable.

## Development Status

**MVP Complete** (2026-03-19). All 4 development phases implemented and tested, 417 unit/integration tests pass. See [docs/TASKS.md](docs/TASKS.md).

## License

GNU Affero General Public License v3.0 (AGPL-3.0)

See [LICENSE](./LICENSE) file for details.
