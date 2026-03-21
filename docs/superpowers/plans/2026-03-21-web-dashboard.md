# Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only Web Dashboard to Lynxclaw for visualizing group status, message history, audit logs, scheduled tasks, and token usage metrics.

**Architecture:** Extend the existing `WebhookServer` in `src/server.py` to also serve Dashboard API routes (`/api/*`) and static files, sharing the same uvicorn instance and SQLite connection. The dashboard is a zero-build-step SPA using Alpine.js + Tailwind CSS CDN + Chart.js.

**Tech Stack:** Python FastAPI (existing), Alpine.js 3.x (CDN), Tailwind CSS 3.x (CDN play), Chart.js 4.x (CDN), pytest + httpx for API tests.

---

## File Map

| File | Action | Responsibility |
| ---- | ---- | ---- |
| `src/config.py` | Modify (line 102–116) | Add `DashboardConfig` dataclass + wire into `Config` and `load_config` |
| `src/db.py` | Modify (line 425–468) | Add `offset` to `get_messages()` + `get_audit_log()`; add `get_messages_count_since()` |
| `src/dashboard/__init__.py` | Create | Empty package marker |
| `src/dashboard/auth.py` | Create | `verify_token` FastAPI dependency (Bearer Token) |
| `src/dashboard/api.py` | Create | All 7 `/api/*` route handlers |
| `src/server.py` | Modify (line 44–53) | `set_db()` / `set_config()` methods; startup event; dashboard router + StaticFiles |
| `src/main.py` | Modify (line 653–669) | Conditional dashboard startup; call `set_db()` + `set_config()` |
| `src/dashboard/static/index.html` | Create | SPA shell: layout, nav, login modal |
| `src/dashboard/static/app.js` | Create | Alpine.js store + 4 page components |
| `src/dashboard/static/style.css` | Create | Custom accent colors and layout overrides |
| `tests/test_dashboard_auth.py` | Create | Auth: 503/401/200 scenarios |
| `tests/test_dashboard_api.py` | Create | All 7 API endpoints: structure, pagination, filters |
| `tests/test_db.py` | Modify (append) | New DB methods: `get_messages_count_since`, offset params |

---

## Task 1: DashboardConfig — config.py

**Files:**
- Modify: `src/config.py:102-116` (after `SecurityConfig`, before `Config`)
- Modify: `src/config.py:173-182` (`section_map` in `load_config`)
- Test: `tests/test_config.py` (append)

- [ ] **Step 1: Write failing test**

```python
# append to tests/test_config.py

def test_dashboard_defaults():
    from src.config import DashboardConfig
    cfg = DashboardConfig()
    assert cfg.enabled is False

def test_dashboard_config_loaded(tmp_path):
    from src.config import load_config
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("dashboard:\n  enabled: true\n")
    import os; os.environ["ANTHROPIC_API_KEY"] = "test"
    cfg = load_config(cfg_file, dotenv_path=None)
    assert cfg.dashboard.enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/test_config.py::test_dashboard_defaults -v
```

Expected: `ImportError` or `AttributeError` — `DashboardConfig` not yet defined.

- [ ] **Step 3: Add `DashboardConfig` and wire it into `Config` + `load_config`**

In `src/config.py`, after the `SecurityConfig` dataclass (line ~100), add:

```python
@dataclass
class DashboardConfig:
    enabled: bool = False
```

In the `Config` dataclass (line ~103), add one field after `security`:

```python
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
```

In `load_config`, add `"dashboard": cfg.dashboard` to `section_map` (line ~173):

```python
    section_map = {
        "host": cfg.host,
        "container": cfg.container,
        "telegram": cfg.telegram,
        "feishu": cfg.feishu,
        "router": cfg.router,
        "proxy": cfg.proxy,
        "streaming": cfg.streaming,
        "security": cfg.security,
        "dashboard": cfg.dashboard,   # ← add this line
    }
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_config.py -v
```

Expected: all pass (including new tests).

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: add DashboardConfig to config system"
```

---

## Task 2: DB extensions — db.py

**Files:**
- Modify: `src/db.py:425-468`
- Test: `tests/test_db.py` (append)

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_db.py

import time

async def test_get_messages_offset(db: Database):
    """offset parameter skips rows correctly."""
    import time
    for i in range(5):
        await db.insert_message(
            channel="telegram", chat_id="c1", message_id=f"m{i}",
            sender_id="u1", content=f"msg{i}", direction="inbound",
        )
    all_msgs = await db.get_messages(limit=10, offset=0)
    offset_msgs = await db.get_messages(limit=10, offset=2)
    assert len(offset_msgs) == len(all_msgs) - 2

async def test_get_audit_log_offset(db: Database):
    """offset parameter skips audit rows correctly."""
    for i in range(4):
        await db._conn.execute(
            "INSERT INTO tool_audit_log (group_name, tool_name, created_at) VALUES (?,?,?)",
            ("g1", f"tool{i}", int(time.time()) + i),
        )
    await db._conn.commit()
    all_rows = await db.get_audit_log(limit=10, offset=0)
    offset_rows = await db.get_audit_log(limit=10, offset=2)
    assert len(offset_rows) == len(all_rows) - 2

async def test_get_messages_count_since(db: Database):
    """Count messages created after a timestamp."""
    now = int(time.time())
    await db.insert_message(
        channel="telegram", chat_id="c1", message_id="old",
        sender_id="u1", content="old", direction="inbound",
        created_at=now - 10000,
    )
    await db.insert_message(
        channel="telegram", chat_id="c1", message_id="new",
        sender_id="u1", content="new", direction="inbound",
        created_at=now,
    )
    count = await db.get_messages_count_since(now - 1)
    assert count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```
pytest tests/test_db.py::test_get_messages_offset tests/test_db.py::test_get_audit_log_offset tests/test_db.py::test_get_messages_count_since -v
```

Expected: `TypeError` (unexpected keyword argument `offset`) + `AttributeError` (no method).

- [ ] **Step 3: Update `get_messages()` to add `offset`**

Replace the `get_messages` method in `src/db.py` (lines 425–448):

```python
async def get_messages(
    self,
    *,
    group_name: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return messages with optional group/status filters, newest first."""
    conditions: list[str] = []
    params: list[Any] = []
    if group_name is not None:
        conditions.append("group_name=?")
        params.append(group_name)
    if status is not None:
        conditions.append("status=?")
        params.append(status)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])
    async with self._conn.execute(
        f"SELECT * FROM messages {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Update `get_audit_log()` to add `offset`**

Replace the `get_audit_log` method in `src/db.py` (lines 450–468):

```python
async def get_audit_log(
    self,
    *,
    group_name: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return tool audit log entries, newest first."""
    if group_name is not None:
        sql = (
            "SELECT * FROM tool_audit_log WHERE group_name=? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params: tuple[Any, ...] = (group_name, limit, offset)
    else:
        sql = "SELECT * FROM tool_audit_log ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params = (limit, offset)
    async with self._conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Add `get_messages_count_since()`**

Append after `get_audit_log` in `src/db.py`:

```python
async def get_messages_count_since(self, ts: int) -> int:
    """Return the number of messages created at or after the given Unix timestamp."""
    async with self._conn.execute(
        "SELECT COUNT(*) FROM messages WHERE created_at >= ?",
        (ts,),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0
```

- [ ] **Step 6: Run all db tests**

```
pytest tests/test_db.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: add offset param to get_messages/get_audit_log; add get_messages_count_since"
```

---

## Task 3: Auth dependency — dashboard/auth.py

**Files:**
- Create: `src/dashboard/__init__.py`
- Create: `src/dashboard/auth.py`
- Create (partial): `tests/test_dashboard_auth.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_dashboard_auth.py
"""Tests for Bearer Token authentication in the dashboard."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard.auth import verify_token


def test_missing_token_returns_503(monkeypatch):
    """No LYNXCLAW_DASHBOARD_TOKEN env var → 503."""
    monkeypatch.delenv("LYNXCLAW_DASHBOARD_TOKEN", raising=False)
    # Re-import to pick up env change
    import importlib, src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", None)

    app = FastAPI()

    from src.dashboard.auth import verify_token
    from fastapi import Depends

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t", headers={"Authorization": "Bearer sometoken"})
    assert resp.status_code == 503


def test_wrong_token_returns_401(monkeypatch):
    """Wrong token → 401."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "correct-token")

    app = FastAPI()
    from src.dashboard.auth import verify_token
    from fastapi import Depends

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_correct_token_returns_200(monkeypatch):
    """Correct token → 200."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "my-secret")

    app = FastAPI()
    from src.dashboard.auth import verify_token
    from fastapi import Depends

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t", headers={"Authorization": "Bearer my-secret"})
    assert resp.status_code == 200


def test_no_auth_header_returns_401(monkeypatch):
    """Missing Authorization header → 401."""
    import src.dashboard.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "my-secret")

    app = FastAPI()
    from src.dashboard.auth import verify_token
    from fastapi import Depends

    @app.get("/t")
    def t(v=Depends(verify_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/t")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_dashboard_auth.py -v
```

Expected: `ModuleNotFoundError` for `src.dashboard.auth`.

- [ ] **Step 3: Create `src/dashboard/__init__.py`**

```python
# src/dashboard/__init__.py
```

(Empty file — package marker only.)

- [ ] **Step 4: Create `src/dashboard/auth.py`**

```python
"""Bearer Token authentication dependency for the Lynxclaw Dashboard."""
from __future__ import annotations

import os

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(auto_error=False)

# Read once at module load — never per-request.
_DASHBOARD_TOKEN: str | None = os.environ.get("LYNXCLAW_DASHBOARD_TOKEN") or None


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> None:
    """Raise 503 if token not configured; 401 if token wrong or missing."""
    if _DASHBOARD_TOKEN is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    if credentials is None or credentials.credentials != _DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
```

- [ ] **Step 5: Run auth tests**

```
pytest tests/test_dashboard_auth.py -v
```

Expected: all 4 pass.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/__init__.py src/dashboard/auth.py tests/test_dashboard_auth.py
git commit -m "feat: add dashboard Bearer Token auth dependency"
```

---

## Task 4: API router — dashboard/api.py

**Files:**
- Create: `src/dashboard/api.py`
- Create: `tests/test_dashboard_api.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_dashboard_api.py
"""Tests for Dashboard API endpoints."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard.api import router
from src.dashboard import auth as auth_mod


@pytest.fixture
async def db(tmp_path):
    from src.db import Database
    database = Database()
    await database.init(str(tmp_path / "test.db"))
    yield database
    await database.close()


@pytest.fixture
async def client(db, monkeypatch):
    """TestClient with token patched and db/config injected into app.state.

    NOTE: must be async because it depends on the async `db` fixture.
    """
    monkeypatch.setattr(auth_mod, "_DASHBOARD_TOKEN", "test-token")

    from src.config import Config
    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.config = Config(anthropic_api_key="dummy")

    return TestClient(app, raise_server_exceptions=False)


AUTH = {"Authorization": "Bearer test-token"}


# --- /api/system/overview ---

def test_overview_shape(client):
    resp = client.get("/api/system/overview", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "groups_count" in data
    assert "messages_today" in data
    assert "total_input_tokens" in data
    assert "total_output_tokens" in data
    assert "active_containers" in data


# --- /api/groups ---

def test_groups_shape(client):
    resp = client.get("/api/groups", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data


# --- /api/messages ---

async def test_messages_pagination(client, db):
    for i in range(5):
        await db.insert_message(
            channel="telegram", chat_id="c1", message_id=f"m{i}",
            sender_id="u1", content=f"msg{i}", direction="inbound",
        )
    resp = client.get("/api/messages?limit=3&offset=0", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 3
    assert data["total"] >= 5

    resp2 = client.get("/api/messages?limit=3&offset=3", headers=AUTH)
    assert resp2.status_code == 200
    assert len(resp2.json()["items"]) >= 2


# --- /api/audit ---

def test_audit_shape(client):
    resp = client.get("/api/audit", headers=AUTH)
    assert resp.status_code == 200
    assert "items" in resp.json()
    assert "total" in resp.json()


# --- /api/tasks ---

def test_tasks_shape(client):
    resp = client.get("/api/tasks", headers=AUTH)
    assert resp.status_code == 200
    assert "items" in resp.json()


# --- /api/usage ---

def test_usage_shape(client):
    resp = client.get("/api/usage", headers=AUTH)
    assert resp.status_code == 200
    assert "items" in resp.json()


# --- /api/config ---

def test_config_redacts_secrets(client):
    resp = client.get("/api/config", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["anthropic_api_key"] == "***"
    assert data["telegram"]["bot_token"] == "***"
    assert data["feishu"]["app_secret"] == "***"
    assert data["feishu"]["app_id"] == "***"


# --- auth rejection ---

def test_missing_token_rejected(client):
    resp = client.get("/api/system/overview")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_dashboard_api.py -v
```

Expected: `ModuleNotFoundError` for `src.dashboard.api`.

- [ ] **Step 3: Create `src/dashboard/api.py`**

```python
"""Lynxclaw Dashboard API — read-only FastAPI router.

All routes require Bearer Token auth via verify_token dependency.
DB and Config are read from request.app.state (injected by WebhookServer startup).
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from src.dashboard.auth import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _day_start_ts() -> int:
    """Unix timestamp for 00:00:00 UTC today."""
    now = time.time()
    return int(now - (now % 86400))


def _redact_config(cfg_dict: dict[str, Any]) -> dict[str, Any]:
    """Replace sensitive fields in a dataclasses.asdict() dict with '***'."""
    cfg_dict["anthropic_api_key"] = "***"
    if "telegram" in cfg_dict:
        cfg_dict["telegram"]["bot_token"] = "***"
    if "feishu" in cfg_dict:
        cfg_dict["feishu"]["app_secret"] = "***"
        cfg_dict["feishu"]["app_id"] = "***"
    return cfg_dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/system/overview")
async def overview(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    groups = await db.get_all_groups()
    messages_today = await db.get_messages_count_since(_day_start_ts())
    usage_rows = await db.get_token_usage_all_groups()
    total_input = sum(r["input_tokens"] for r in usage_rows)
    total_output = sum(r["output_tokens"] for r in usage_rows)
    # Active containers: count from DB messages with status='pending' as proxy.
    # Real count unavailable without querying Docker; use 0 as safe default.
    return {
        "groups_count": len(groups),
        "active_containers": 0,
        "messages_today": messages_today,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
    }


@router.get("/groups")
async def groups(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    groups_list = await db.get_all_groups()
    sessions = await db.get_all_sessions()
    session_map = {s["group_name"]: s for s in sessions}
    items = []
    for g in groups_list:
        sess = session_map.get(g["name"])
        items.append({
            **g,
            "session_id": sess["session_id"] if sess else None,
            "last_active": sess["last_active"] if sess else None,
        })
    return {"items": items}


@router.get("/messages")
async def messages(
    request: Request,
    group: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = request.app.state.db
    items = await db.get_messages(
        group_name=group, status=status, limit=limit, offset=offset
    )
    # Count total matching rows (simplified; replace with COUNT(*) for large datasets)
    all_items = await db.get_messages(group_name=group, status=status, limit=100000, offset=0)
    return {"total": len(all_items), "items": items}


@router.get("/audit")
async def audit(
    request: Request,
    group: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    db = request.app.state.db
    items = await db.get_audit_log(group_name=group, limit=limit, offset=offset)
    all_items = await db.get_audit_log(group_name=group, limit=100000, offset=0)  # simplified total count
    return {"total": len(all_items), "items": items}


@router.get("/tasks")
async def tasks(
    request: Request,
    group: Optional[str] = Query(None),
) -> dict[str, Any]:
    db = request.app.state.db
    items = await db.get_all_tasks(group_name=group)
    return {"items": items}


@router.get("/usage")
async def usage(
    request: Request,
    group: Optional[str] = Query(None),
    since: Optional[int] = Query(None),
) -> dict[str, Any]:
    db = request.app.state.db
    if group:
        row = await db.get_token_usage(group_name=group, since=since)
        items = [{"group_name": group, **row}]
    else:
        items = await db.get_token_usage_all_groups()
        if since is not None:
            # Filter by since at application layer for per-group breakdown
            filtered = []
            for item in items:
                row = await db.get_token_usage(group_name=item["group_name"], since=since)
                filtered.append({"group_name": item["group_name"], **row})
            items = filtered
    return {"items": items}


@router.get("/config")
async def config(request: Request) -> dict[str, Any]:
    cfg = request.app.state.config
    cfg_dict = dataclasses.asdict(cfg)
    return _redact_config(cfg_dict)
```

- [ ] **Step 4: Run API tests**

```
pytest tests/test_dashboard_api.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/api.py tests/test_dashboard_api.py
git commit -m "feat: add dashboard API router with 7 read-only endpoints"
```

---

## Task 5: Integrate into server.py

**Files:**
- Modify: `src/server.py:44-53` (add `_db`, `_config`, `set_db`, `set_config`, startup event, dashboard router, StaticFiles)

This task has no new test file — the integration is tested implicitly by the API tests running against the real `WebhookServer` (not needed) and by manual smoke test.

- [ ] **Step 1: Update `WebhookServer.__init__` and add helpers**

In `src/server.py`, make the following changes:

**Add imports** at the top of existing imports block:

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles
```

**Modify `__init__`** — add `_db` / `_config` attributes and dashboard wiring after the existing route registrations (after line 53):

```python
    def __init__(self) -> None:
        self.app = FastAPI(title="Lynxclaw Webhook", docs_url=None, redoc_url=None)
        self._server: Optional[uvicorn.Server] = None
        self._serve_task: Optional[asyncio.Task] = None
        self._telegram_adapter: Optional["TelegramAdapter"] = None
        self._feishu_adapter: Optional["FeishuAdapter"] = None
        self._db: Any = None
        self._config: Any = None

        # Register built-in routes
        self.app.add_api_route("/health", self._health, methods=["GET"])
        self.app.add_api_route("/metrics", self._metrics_redirect, methods=["GET"])

        # Dashboard — API router first, StaticFiles catch-all last
        from src.dashboard.api import router as _dash_router
        self.app.include_router(_dash_router)
        _static_dir = Path(__file__).parent / "dashboard" / "static"
        if _static_dir.exists():
            self.app.mount(
                "/", StaticFiles(directory=str(_static_dir), html=True), name="static"
            )

        # Startup event: inject shared state
        @self.app.on_event("startup")
        async def _startup() -> None:
            self.app.state.db = self._db
            self.app.state.config = self._config
```

**Add `set_db` and `set_config`** methods after `__init__`:

```python
    def set_db(self, db: Any) -> None:
        """Inject the shared Database instance before start()."""
        self._db = db

    def set_config(self, config: Any) -> None:
        """Inject the loaded Config before start()."""
        self._config = config
```

- [ ] **Step 2: Run full test suite to check no regressions**

```
pytest tests/ --ignore=tests/test_e2e_local.py -v
```

Expected: same pass count as before (402+), no new failures.

- [ ] **Step 3: Commit**

```bash
git add src/server.py
git commit -m "feat: integrate dashboard router and StaticFiles into WebhookServer"
```

---

## Task 6: Conditional startup in main.py

**Files:**
- Modify: `src/main.py:653-669`

- [ ] **Step 1: Update the webhook/dashboard startup block**

Find the block starting at line ~653 in `src/main.py`:

```python
    # --- Webhook server (started if any adapter uses webhook mode) ---
    webhook_server: Optional[WebhookServer] = None
    needs_webhook = (
        (config.telegram.enabled and config.telegram.mode == "webhook") or
        (config.feishu.enabled and config.feishu.mode == "webhook")
    )
    if needs_webhook:
        webhook_server = WebhookServer()
        ...
        await webhook_server.start(host=webhook_host, port=webhook_port)
```

Replace with:

```python
    # --- Webhook / Dashboard server ---
    webhook_server: Optional[WebhookServer] = None
    needs_webhook = (
        (config.telegram.enabled and config.telegram.mode == "webhook") or
        (config.feishu.enabled and config.feishu.mode == "webhook")
    )
    needs_dashboard = config.dashboard.enabled
    if needs_webhook or needs_dashboard:
        webhook_server = WebhookServer()
        webhook_server.set_db(db)
        webhook_server.set_config(config)
        if needs_webhook:
            if config.telegram.enabled and config.telegram.mode == "webhook":
                tg_adapter = registry.get("telegram")
                webhook_server.setup_telegram(tg_adapter)
            if config.feishu.enabled and config.feishu.mode == "webhook":
                feishu_adapter = registry.get("feishu")
                webhook_server.setup_feishu(feishu_adapter)
        webhook_host = getattr(config.host, "webhook_host", "0.0.0.0")
        webhook_port = getattr(config.host, "webhook_port", 8080)
        await webhook_server.start(host=webhook_host, port=webhook_port)
```

- [ ] **Step 2: Run full test suite**

```
pytest tests/ --ignore=tests/test_e2e_local.py -v
```

Expected: all pass, no regressions.

- [ ] **Step 3: Commit**

```bash
git add src/main.py
git commit -m "feat: start WebhookServer when dashboard.enabled; inject db+config"
```

---

## Task 7: Static frontend — index.html

**Files:**
- Create: `src/dashboard/static/index.html`

(No automated test — manual browser verification after server start.)

- [ ] **Step 1: Create `src/dashboard/static/index.html`**

```html
<!DOCTYPE html>
<html lang="zh" x-data>
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Lynxclaw Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <script src="https://unpkg.com/chart.js@4/dist/chart.umd.min.js"></script>
  <link rel="stylesheet" href="/style.css" />
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen" x-data="appRoot()">

  <!-- Login modal -->
  <div x-show="!store.token" class="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
    <div class="bg-gray-900 border border-gray-700 rounded-xl p-8 w-full max-w-sm shadow-2xl">
      <h2 class="text-lg font-semibold mb-4 text-white">Lynxclaw Dashboard</h2>
      <p class="text-sm text-gray-400 mb-4">输入 Dashboard Token 登录</p>
      <input
        type="password"
        placeholder="Bearer token..."
        x-model="tokenInput"
        @keyup.enter="login()"
        class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500 mb-3"
      />
      <p x-show="loginError" x-text="loginError" class="text-red-400 text-xs mb-3"></p>
      <button
        @click="login()"
        class="w-full bg-blue-600 hover:bg-blue-500 text-white rounded-lg py-2 text-sm font-medium transition"
      >登录</button>
    </div>
  </div>

  <!-- Main layout -->
  <div x-show="store.token" class="flex flex-col h-screen">

    <!-- Header -->
    <header class="bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center justify-between shrink-0">
      <span class="font-bold text-blue-400 tracking-wide">Lynxclaw Dashboard</span>
      <button @click="logout()" class="text-xs text-gray-500 hover:text-red-400 transition">退出</button>
    </header>

    <div class="flex flex-1 overflow-hidden">

      <!-- Sidebar nav -->
      <nav class="w-44 bg-gray-900 border-r border-gray-800 flex flex-col pt-4 shrink-0">
        <template x-for="item in navItems" :key="item.page">
          <button
            @click="store.currentPage = item.page"
            :class="store.currentPage === item.page
              ? 'bg-blue-600/20 text-blue-400 border-r-2 border-blue-500'
              : 'text-gray-400 hover:text-white hover:bg-gray-800'"
            class="w-full text-left px-4 py-2.5 text-sm transition"
            x-text="item.label"
          ></button>
        </template>
      </nav>

      <!-- Content area -->
      <main class="flex-1 overflow-auto p-6" id="content">
        <div x-show="store.loading" class="text-gray-500 text-sm">加载中...</div>
        <div x-show="store.error" class="text-red-400 text-sm" x-text="store.error"></div>

        <div x-show="store.currentPage === 'overview'" x-data="overviewPage()">
          <div x-html="render()"></div>
        </div>
        <div x-show="store.currentPage === 'messages'" x-data="messagesPage()">
          <div x-html="render()"></div>
        </div>
        <div x-show="store.currentPage === 'tasks'" x-data="tasksPage()">
          <div x-html="render()"></div>
        </div>
        <div x-show="store.currentPage === 'metrics'" x-data="metricsPage()">
          <div x-html="render()"></div>
        </div>
      </main>
    </div>
  </div>

  <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add src/dashboard/static/index.html
git commit -m "feat: add dashboard SPA shell (index.html)"
```

---

## Task 8: Frontend logic — app.js

**Files:**
- Create: `src/dashboard/static/app.js`

- [ ] **Step 1: Create `src/dashboard/static/app.js`**

```js
// Lynxclaw Dashboard — Alpine.js components

// ---------------------------------------------------------------------------
// Global store
// ---------------------------------------------------------------------------
document.addEventListener('alpine:init', () => {
  Alpine.store('app', {
    token: sessionStorage.getItem('dashboard_token') || '',
    currentPage: 'overview',
    loading: false,
    error: null,

    async api(path, params = {}) {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([, v]) => v !== null && v !== undefined))
      ).toString()
      const url = qs ? `/api${path}?${qs}` : `/api${path}`
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${this.token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.json()
    },

    setToken(t) {
      this.token = t
      sessionStorage.setItem('dashboard_token', t)
    },

    clearToken() {
      this.token = ''
      sessionStorage.removeItem('dashboard_token')
    },
  })
})

// ---------------------------------------------------------------------------
// Root component
// ---------------------------------------------------------------------------
function appRoot() {
  return {
    store: null,
    tokenInput: '',
    loginError: '',
    navItems: [
      { page: 'overview', label: '系统概览' },
      { page: 'messages', label: '消息审计' },
      { page: 'tasks',    label: '任务管理' },
      { page: 'metrics',  label: '指标图表' },
    ],

    init() { this.store = Alpine.store('app') },

    async login() {
      this.loginError = ''
      Alpine.store('app').setToken(this.tokenInput)
      try {
        await Alpine.store('app').api('/system/overview')
      } catch {
        Alpine.store('app').clearToken()
        this.loginError = '认证失败，请检查 Token'
      }
    },

    logout() { Alpine.store('app').clearToken() },
  }
}

// ---------------------------------------------------------------------------
// Helper: format unix timestamp
// ---------------------------------------------------------------------------
function fmtTs(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

// ---------------------------------------------------------------------------
// Overview page
// ---------------------------------------------------------------------------
function overviewPage() {
  return {
    data: null,
    groups: [],

    async init() {
      const s = Alpine.store('app')
      try {
        this.data = await s.api('/system/overview')
        const g = await s.api('/groups')
        this.groups = g.items
      } catch (e) {
        Alpine.store('app').error = e.message
      }
    },

    render() {
      if (!this.data) return '<p class="text-gray-500 text-sm">加载中...</p>'
      const d = this.data
      const cards = [
        { label: 'Groups',    value: d.groups_count },
        { label: '活跃容器',  value: d.active_containers },
        { label: '今日消息',  value: d.messages_today },
        { label: '累计 Token', value: (d.total_input_tokens + d.total_output_tokens).toLocaleString() },
      ]
      const cardHtml = cards.map(c => `
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <p class="text-xs text-gray-500 mb-1">${c.label}</p>
          <p class="text-2xl font-bold text-white">${c.value}</p>
        </div>
      `).join('')

      const rows = this.groups.map(g => `
        <tr class="border-t border-gray-800 hover:bg-gray-800/50">
          <td class="px-4 py-2 text-sm font-medium text-blue-400">${g.name}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${g.channel}</td>
          <td class="px-4 py-2 text-sm text-gray-400 font-mono">${g.chat_id}</td>
          <td class="px-4 py-2 text-sm text-gray-400">${g.trigger || '—'}</td>
          <td class="px-4 py-2 text-xs font-mono text-gray-500">${g.session_id ? g.session_id.slice(0,12)+'…' : '无会话'}</td>
          <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(g.last_active)}</td>
        </tr>
      `).join('')

      return `
        <h1 class="text-xl font-bold mb-6 text-white">系统概览</h1>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">${cardHtml}</div>
        <h2 class="text-base font-semibold mb-3 text-gray-300">Groups 状态</h2>
        <div class="overflow-x-auto rounded-xl border border-gray-800">
          <table class="w-full text-left">
            <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
              <tr>
                <th class="px-4 py-3">名称</th><th class="px-4 py-3">渠道</th>
                <th class="px-4 py-3">Chat ID</th><th class="px-4 py-3">触发词</th>
                <th class="px-4 py-3">Session</th><th class="px-4 py-3">最后活跃</th>
              </tr>
            </thead>
            <tbody>${rows || '<tr><td colspan="6" class="px-4 py-4 text-gray-600 text-sm">暂无数据</td></tr>'}</tbody>
          </table>
        </div>
      `
    },
  }
}

// ---------------------------------------------------------------------------
// Messages / Audit page
// ---------------------------------------------------------------------------
function messagesPage() {
  return {
    tab: 'messages',
    messages: [], msgTotal: 0, msgPage: 0,
    audit: [],    audTotal: 0, audPage: 0,
    msgGroup: '', msgStatus: '',

    async init() { await this.loadMessages(); await this.loadAudit() },

    async loadMessages() {
      const s = Alpine.store('app')
      try {
        const r = await s.api('/messages', {
          limit: 50, offset: this.msgPage * 50,
          group: this.msgGroup || null,
          status: this.msgStatus || null,
        })
        this.messages = r.items; this.msgTotal = r.total
      } catch (e) { Alpine.store('app').error = e.message }
    },

    async loadAudit() {
      const s = Alpine.store('app')
      try {
        const r = await s.api('/audit', { limit: 50, offset: this.audPage * 50 })
        this.audit = r.items; this.audTotal = r.total
      } catch (e) { Alpine.store('app').error = e.message }
    },

    render() {
      const tabs = `
        <div class="flex gap-2 mb-6">
          <button onclick="document.querySelector('[x-data*=messagesPage]').__x.$data.tab='messages';document.querySelector('[x-data*=messagesPage]').__x.$data.loadMessages()"
            class="${this.tab==='messages' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'} px-4 py-1.5 rounded-lg text-sm transition">消息历史</button>
          <button onclick="document.querySelector('[x-data*=messagesPage]').__x.$data.tab='audit';document.querySelector('[x-data*=messagesPage]').__x.$data.loadAudit()"
            class="${this.tab==='audit' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'} px-4 py-1.5 rounded-lg text-sm transition">审计日志</button>
        </div>
      `
      if (this.tab === 'messages') {
        const rows = this.messages.map(m => `
          <tr class="border-t border-gray-800 hover:bg-gray-800/50">
            <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(m.created_at)}</td>
            <td class="px-4 py-2 text-sm text-blue-400">${m.group_name || '—'}</td>
            <td class="px-4 py-2 text-xs"><span class="px-2 py-0.5 rounded ${m.direction==='inbound' ? 'bg-green-900/50 text-green-400' : 'bg-purple-900/50 text-purple-400'}">${m.direction}</span></td>
            <td class="px-4 py-2 text-xs"><span class="px-2 py-0.5 rounded ${m.status==='completed' ? 'bg-blue-900/50 text-blue-400' : m.status==='failed' ? 'bg-red-900/50 text-red-400' : 'bg-yellow-900/50 text-yellow-400'}">${m.status}</span></td>
            <td class="px-4 py-2 text-xs text-gray-400 max-w-xs truncate">${(m.content||'').slice(0,80)}</td>
          </tr>
        `).join('')
        return `
          <h1 class="text-xl font-bold mb-6 text-white">消息与审计</h1>${tabs}
          <p class="text-xs text-gray-500 mb-3">共 ${this.msgTotal} 条，当前第 ${this.msgPage+1} 页</p>
          <div class="overflow-x-auto rounded-xl border border-gray-800">
            <table class="w-full text-left">
              <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
                <tr><th class="px-4 py-3">时间</th><th class="px-4 py-3">Group</th><th class="px-4 py-3">方向</th><th class="px-4 py-3">状态</th><th class="px-4 py-3">内容</th></tr>
              </thead>
              <tbody>${rows || '<tr><td colspan="5" class="px-4 py-4 text-gray-600 text-sm">暂无数据</td></tr>'}</tbody>
            </table>
          </div>
        `
      } else {
        const rows = this.audit.map(a => `
          <tr class="border-t border-gray-800 hover:bg-gray-800/50">
            <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(a.created_at)}</td>
            <td class="px-4 py-2 text-sm text-blue-400">${a.group_name}</td>
            <td class="px-4 py-2 text-sm text-gray-300 font-mono">${a.tool_name}</td>
            <td class="px-4 py-2"><span class="px-2 py-0.5 rounded text-xs ${a.blocked ? 'bg-red-900/50 text-red-400' : 'bg-gray-800 text-gray-500'}">${a.blocked ? '已拦截' : '通过'}</span></td>
            <td class="px-4 py-2 text-xs text-gray-400 max-w-xs truncate">${a.input_summary || '—'}</td>
          </tr>
        `).join('')
        return `
          <h1 class="text-xl font-bold mb-6 text-white">消息与审计</h1>${tabs}
          <p class="text-xs text-gray-500 mb-3">共 ${this.audTotal} 条</p>
          <div class="overflow-x-auto rounded-xl border border-gray-800">
            <table class="w-full text-left">
              <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
                <tr><th class="px-4 py-3">时间</th><th class="px-4 py-3">Group</th><th class="px-4 py-3">工具</th><th class="px-4 py-3">状态</th><th class="px-4 py-3">摘要</th></tr>
              </thead>
              <tbody>${rows || '<tr><td colspan="5" class="px-4 py-4 text-gray-600 text-sm">暂无数据</td></tr>'}</tbody>
            </table>
          </div>
        `
      }
    },
  }
}

// ---------------------------------------------------------------------------
// Tasks page
// ---------------------------------------------------------------------------
function tasksPage() {
  return {
    tasks: [],

    async init() {
      try {
        const r = await Alpine.store('app').api('/tasks')
        this.tasks = r.items
      } catch (e) { Alpine.store('app').error = e.message }
    },

    render() {
      const rows = this.tasks.map(t => `
        <tr class="border-t border-gray-800 hover:bg-gray-800/50">
          <td class="px-4 py-2 text-xs font-mono text-gray-500">${t.id.slice(0,8)}…</td>
          <td class="px-4 py-2 text-sm text-blue-400">${t.group_name}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${t.type}</td>
          <td class="px-4 py-2 text-xs font-mono text-gray-400">${t.schedule}</td>
          <td class="px-4 py-2"><span class="px-2 py-0.5 rounded text-xs ${t.status==='active' ? 'bg-green-900/50 text-green-400' : 'bg-gray-700 text-gray-500'}">${t.status}</span></td>
          <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(t.last_run)}</td>
          <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(t.next_run)}</td>
        </tr>
      `).join('')
      return `
        <h1 class="text-xl font-bold mb-6 text-white">任务管理</h1>
        <div class="overflow-x-auto rounded-xl border border-gray-800">
          <table class="w-full text-left">
            <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
              <tr>
                <th class="px-4 py-3">ID</th><th class="px-4 py-3">Group</th>
                <th class="px-4 py-3">类型</th><th class="px-4 py-3">Cron</th>
                <th class="px-4 py-3">状态</th><th class="px-4 py-3">上次运行</th><th class="px-4 py-3">下次运行</th>
              </tr>
            </thead>
            <tbody>${rows || '<tr><td colspan="7" class="px-4 py-4 text-gray-600 text-sm">暂无任务</td></tr>'}</tbody>
          </table>
        </div>
      `
    },
  }
}

// ---------------------------------------------------------------------------
// Metrics page
// ---------------------------------------------------------------------------
function metricsPage() {
  return {
    usage: [],
    chartRendered: false,

    async init() {
      try {
        const since7d = Math.floor(Date.now() / 1000) - 7 * 86400
        const r = await Alpine.store('app').api('/usage', { since: since7d })
        this.usage = r.items
      } catch (e) { Alpine.store('app').error = e.message }
    },

    render() {
      const rows = this.usage.map(u => `
        <tr class="border-t border-gray-800">
          <td class="px-4 py-2 text-sm text-blue-400">${u.group_name}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${(u.input_tokens||0).toLocaleString()}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${(u.output_tokens||0).toLocaleString()}</td>
          <td class="px-4 py-2 text-sm font-medium text-white">${((u.input_tokens||0)+(u.output_tokens||0)).toLocaleString()}</td>
        </tr>
      `).join('')
      return `
        <h1 class="text-xl font-bold mb-6 text-white">指标图表</h1>
        <h2 class="text-base font-semibold mb-3 text-gray-300">Token 用量（最近 7 天，按 Group 汇总）</h2>
        <div class="overflow-x-auto rounded-xl border border-gray-800 mb-8">
          <table class="w-full text-left">
            <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
              <tr>
                <th class="px-4 py-3">Group</th>
                <th class="px-4 py-3">Input Tokens</th>
                <th class="px-4 py-3">Output Tokens</th>
                <th class="px-4 py-3">合计</th>
              </tr>
            </thead>
            <tbody>${rows || '<tr><td colspan="4" class="px-4 py-4 text-gray-600 text-sm">暂无数据</td></tr>'}</tbody>
          </table>
        </div>
      `
    },
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add src/dashboard/static/app.js
git commit -m "feat: add dashboard Alpine.js frontend components"
```

---

## Task 9: Custom styles — style.css

**Files:**
- Create: `src/dashboard/static/style.css`

- [ ] **Step 1: Create `src/dashboard/static/style.css`**

```css
/* Lynxclaw Dashboard — custom overrides (Tailwind handles the rest) */

/* Smooth scrollbars */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #111827; }
::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }

/* Table cells don't wrap by default */
td, th { white-space: nowrap; }

/* Truncate long content cells */
td.max-w-xs { white-space: normal; word-break: break-word; }
```

- [ ] **Step 2: Commit**

```bash
git add src/dashboard/static/style.css
git commit -m "feat: add dashboard custom CSS"
```

---

## Task 10: Run full test suite + smoke test

- [ ] **Step 1: Run full test suite**

```
pytest tests/ --ignore=tests/test_e2e_local.py -v
```

Expected: all original tests pass + new dashboard tests pass. Check count is ≥ original 402.

- [ ] **Step 2: Manual smoke test**

Start the server locally with dashboard enabled:

```bash
# .env or shell
LYNXCLAW_DASHBOARD_TOKEN=mytoken
# lynxclaw.config.yaml: dashboard.enabled: true
python -m src.main
```

Open `http://localhost:8080` in a browser. Verify:

- [ ] Login modal appears; wrong token shows error
- [ ] Correct token shows main layout with 4 nav items
- [ ] 系统概览: stat cards load, groups table renders
- [ ] 消息审计: Messages and Audit tabs work
- [ ] 任务管理: task table renders (or shows "暂无任务")
- [ ] 指标图表: token table renders
- [ ] Logout button clears session

- [ ] **Step 3: Final commit**

```bash
git add .
git commit -m "feat: web dashboard complete — 4 pages, 7 API endpoints, Bearer Token auth"
```

---

## Task 11: Update TASKS.md

**Files:**
- Modify: `docs/TASKS.md`

- [ ] **Step 1: Mark Web Dashboard as complete in TASKS.md**

Add a new Phase 6 section to `docs/TASKS.md` documenting the completed dashboard feature.

- [ ] **Step 2: Commit**

```bash
git add docs/TASKS.md
git commit -m "docs: mark Web Dashboard as complete in TASKS.md"
```
