# Web Dashboard 设计规格

**日期**: 2026-03-21
**状态**: 已批准（v2，修复 spec review 问题）
**关联任务**: `docs/TASKS.md` — 后续开发方向 > Web Dashboard

---

## 1. 背景与目标

Lynxclaw 是一个轻量级 AI Agent 运行平台，目前缺乏可视化管理界面。运维人员只能通过日志和 SQLite 直查来了解系统状态。

**目标**：提供一个只读的 Web Dashboard，用于：

- 可视化 Group 状态、消息流量、Token 用量
- 查看消息历史与工具审计日志
- 管理定时任务（查看，不含创建/修改）
- 展示指标图表与当前配置

**非目标（本期不做）**：

- 在线编辑配置
- 创建/修改/删除 Group 或任务
- 实时 WebSocket 推送
- 多用户权限管理

---

## 2. 架构

### 2.1 整体方案

**方案 A：扩展现有 `server.py`**（已选定）

在现有 `WebhookServer` 中增加 Dashboard 子模块，共享同一 uvicorn 实例与数据库连接。

```text
[Browser]
    │  HTTP GET /           → 静态文件 (index.html / app.js / style.css)
    │  HTTP GET /api/*      → Dashboard API (FastAPI router)
    │  HTTP POST /webhook/* → 现有 Webhook 端点（不变）
    ▼
[WebhookServer: FastAPI + uvicorn]
    ├── src/dashboard/api.py    (新增 router)
    ├── src/dashboard/auth.py   (Bearer Token 中间件)
    └── src/dashboard/static/   (静态文件)
         ├── index.html
         ├── app.js
         └── style.css
```

### 2.2 目录结构

```text
src/
  dashboard/
    __init__.py
    api.py          # FastAPI APIRouter，所有 /api/* 端点
    auth.py         # HTTPBearer 依赖，Token 校验
    static/
      index.html    # SPA 入口
      app.js        # Alpine.js 组件（所有页面逻辑）
      style.css     # 少量自定义样式
```

### 2.3 集成到 `server.py`

`WebhookServer` 新增 `set_db(db)` 和 `set_config(config)` 方法，由 `main.py` 在 `start()` 前调用：

```python
# main.py 中
server = WebhookServer()
server.set_db(db)
server.set_config(config)
await server.start(host, port)
```

`WebhookServer.__init__()` 中追加 router 和静态文件挂载（**顺序固定：router 必须在 StaticFiles mount 之前注册**，否则 StaticFiles 的 `/` catch-all 会遮蔽 `/api/*`）：

```python
from src.dashboard.api import router as dashboard_router
from fastapi.staticfiles import StaticFiles

# 1. 先注册 API router
self.app.include_router(dashboard_router)
# 2. 再挂载静态文件（catch-all，必须最后）
self.app.mount("/", StaticFiles(directory="src/dashboard/static", html=True), name="static")
```

`startup` 事件中注入 DB 和 Config：

```python
@self.app.on_event("startup")
async def _startup():
    self.app.state.db = self._db          # set_db() 设置
    self.app.state.config = self._config  # set_config() 设置
```

FastAPI 文档保持禁用（与现有 `docs_url=None, redoc_url=None` 一致），不暴露 API schema。

---

## 3. 认证

### 3.1 Static Bearer Token

- 从环境变量 `LYNXCLAW_DASHBOARD_TOKEN` 读取预设 Token（**模块加载时读取一次**，非每请求读取）
- 未配置时所有 `/api/*` 端点返回 `503`，body 不描述具体原因（避免泄露配置状态）
- 前端首次访问弹出 Token 输入框，验证成功后存入 `sessionStorage`

### 3.2 实现（`auth.py`）

```python
import os
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

security = HTTPBearer(auto_error=False)

# 模块加载时读取一次
_DASHBOARD_TOKEN: str | None = os.environ.get("LYNXCLAW_DASHBOARD_TOKEN") or None

def verify_token(credentials: HTTPAuthorizationCredentials | None = Security(security)):
    if _DASHBOARD_TOKEN is None:
        raise HTTPException(503, detail="Service unavailable")
    if credentials is None or credentials.credentials != _DASHBOARD_TOKEN:
        raise HTTPException(401, detail="Unauthorized")
```

---

## 4. API 端点

所有端点前缀 `/api`，依赖 `verify_token`。

| 方法 | 路径 | 描述 |
| ---- | ---- | ---- |
| GET | `/api/system/overview` | 系统概览统计 |
| GET | `/api/groups` | Groups 列表（含 session 状态） |
| GET | `/api/messages` | 消息历史，支持 `?group=&status=&limit=&offset=` |
| GET | `/api/audit` | 工具审计日志，支持 `?group=&limit=&offset=` |
| GET | `/api/tasks` | 定时任务列表，支持 `?group=` |
| GET | `/api/usage` | Token 用量，支持 `?group=&since=`（Unix timestamp 秒） |
| GET | `/api/config` | 只读配置（敏感字段脱敏为 `***`） |

### 4.1 `/api/system/overview` 响应示例

```json
{
  "groups_count": 3,
  "active_containers": 1,
  "messages_today": 47,
  "total_input_tokens": 128400,
  "total_output_tokens": 34200
}
```

`messages_today` 通过新增的 `db.get_messages_count_since(ts)` 查询（`COUNT(*)` + `WHERE created_at >= ?`）实现。`total_input_tokens` / `total_output_tokens` 由 `get_token_usage_all_groups()` 返回的行在 API 层 `sum()` 计算。

### 4.2 分页规则

- `limit` 默认 50，最大 200
- `offset` 默认 0（需在 `db.py` 的 `get_messages()` 和 `get_audit_log()` 中新增 `offset` 参数）
- 响应体格式：`{"total": N, "items": [...]}`
- `total` 通过 `COUNT(*)` + 相同过滤条件计算

### 4.3 `?since=` 参数规范

`/api/usage` 的 `since` 参数为 **Unix timestamp（整数，秒）**，与 `db.get_token_usage()` 的现有签名一致。前端传入时通过 `Date.getTime() / 1000 | 0` 转换。

### 4.4 脱敏规则（`/api/config`）

以下字段替换为 `"***"`：

- `anthropic_api_key`
- `telegram.bot_token`
- `feishu.app_secret`（密钥）
- `feishu.app_id`（与 secret 合用可访问 API，一并脱敏）

序列化方式：`dataclasses.asdict(config)`，然后对上述路径逐一覆写为 `"***"`。

---

## 5. 数据层

### 5.1 复用现有方法

| API 端点 | 调用的 DB 方法 |
| ---- | ---- |
| `/api/groups` | `get_all_groups()`, `get_all_sessions()` |
| `/api/messages` | `get_messages(group_name, status, limit, offset)` — 新增 `offset` 参数 |
| `/api/audit` | `get_audit_log(group_name, limit, offset)` — 新增 `offset` 参数 |
| `/api/tasks` | `get_all_tasks(group_name)` |
| `/api/usage` | `get_token_usage_all_groups()`, `get_token_usage(group_name, since)` |
| `/api/config` | `app.state.config`（内存中的 `Config` 对象） |

### 5.2 新增 DB 方法（最小化）

| 方法 | 位置 | SQL 描述 |
| ---- | ---- | ---- |
| `get_messages_count_since(ts: int)` | `src/db.py` | `SELECT COUNT(*) FROM messages WHERE created_at >= ?` |
| 为 `get_messages()` 新增 `offset` 参数 | `src/db.py` | `LIMIT ? OFFSET ?` |
| 为 `get_audit_log()` 新增 `offset` 参数 | `src/db.py` | `LIMIT ? OFFSET ?` |

---

## 6. 前端设计

### 6.1 技术选型

| 库 | 版本 | 引入方式 | 注意 |
| ---- | ---- | ---- | ---- |
| Alpine.js | 3.x | CDN (unpkg) | — |
| Tailwind CSS | 3.x | CDN play.cdn | **仅适合内部工具**；生产环境应换 PostCSS 构建版 |
| Chart.js | 4.x | CDN (unpkg) | — |

零构建步骤，静态文件直接由 FastAPI `StaticFiles` serve。内部运维工具，Tailwind Play CDN 可接受。

### 6.2 页面布局

```text
┌─────────────────────────────────────────────────┐
│  Lynxclaw Dashboard             [Token: ●●●●●]  │  ← Header
├──────────┬──────────────────────────────────────┤
│          │                                      │
│  系统概览 │        主内容区                       │
│  消息审计 │   (动态渲染，hash 路由切换)            │
│  任务管理 │                                      │
│  指标图表 │                                      │
│          │                                      │
└──────────┴──────────────────────────────────────┘
```

### 6.3 页面规格

#### 系统概览 (`#/overview`)

- 4 张 stat 卡片：Groups 数量、活跃容器、今日消息数、累计 Token 用量
- Groups 状态表格：name、channel、chat_id、trigger、session 状态、最后活跃时间

#### 消息与审计 (`#/messages`)

- 标签页切换：**Messages** / **Audit Log**
- Messages 表格：时间、Group、方向（inbound/outbound）、状态、内容摘要（前 80 字）
- Audit 表格：时间、Group、工具名、blocked 标记、input 摘要
- 过滤器：Group 下拉、Status 下拉；分页控件

#### 任务管理 (`#/tasks`)

- 任务列表：id、Group、类型、cron 表达式、状态（active/cancelled）、上次运行、下次运行

#### 指标图表 (`#/metrics`)

- Token 用量折线图：X 轴为时间（最近 7 天，前端计算 `since = now - 7days`），Y 轴为 Token 数，按 Group 分线
- 配置只读面板：展示 container、streaming、security 等关键配置项

### 6.4 Alpine.js 全局 Store

```js
Alpine.store('app', {
  token: sessionStorage.getItem('dashboard_token') || '',
  currentPage: 'overview',
  loading: false,
  error: null,

  async api(path, params = {}) {
    const qs = new URLSearchParams(params).toString()
    const url = qs ? `/api${path}?${qs}` : `/api${path}`
    const res = await fetch(url, {
      headers: { Authorization: `Bearer ${this.token}` }
    })
    if (!res.ok) throw new Error(`${res.status}`)
    return res.json()
  },

  setToken(t) { this.token = t; sessionStorage.setItem('dashboard_token', t); }
})
```

### 6.5 Token 登录流

1. 页面加载 → 检查 `sessionStorage.dashboard_token`
2. 无 Token → 显示模态框，要求输入
3. 输入后调用 `GET /api/system/overview` 验证
4. 401/503 → 显示通用错误（"认证失败，请检查 Token"）；200 → 存储并进入主界面

---

## 7. 配置

在 `src/config.py` 中新增 `DashboardConfig` dataclass，并在主 `Config` 中引用：

```python
@dataclass
class DashboardConfig:
    enabled: bool = False   # 默认关闭，需显式开启

@dataclass
class Config:
    ...
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
```

`lynxclaw.config.yaml` 示例：

```yaml
dashboard:
  enabled: true
```

`LYNXCLAW_DASHBOARD_TOKEN` 通过 `.env` 或环境变量设置，**不进 config.yaml**（避免明文提交）。

Dashboard 仅在 `config.dashboard.enabled = True` 时由 `main.py` 启动 WebhookServer（或在已有 WebhookServer 上追加）。

---

## 8. 安全考量

- **只读**：所有 API 端点为 GET，无写入操作
- **认证强制**：Token 未配置时返回 503（无描述性 body），不暴露任何数据
- **脱敏**：`anthropic_api_key`、`telegram.bot_token`、`feishu.app_secret`、`feishu.app_id` 一律返回 `***`
- **本地优先**：Dashboard 默认只在 localhost 可访问（WebhookServer 默认绑定 `127.0.0.1`）
- **无 CORS 风险**：前后端同源（同端口），不需要 CORS 配置
- **FastAPI 文档禁用**：`docs_url=None, redoc_url=None` 保持现有设置，不暴露 API schema

---

## 9. 测试策略

| 测试类型 | 文件 | 覆盖内容 |
| ---- | ---- | ---- |
| API 端点测试 | `tests/test_dashboard_api.py` | 每个端点响应结构、分页（limit/offset）、过滤参数 |
| 认证测试 | `tests/test_dashboard_auth.py` | 无 Token 503、错误 Token 401、正确 Token 200 |
| DB 方法测试 | 追加到 `tests/test_db.py` | `get_messages_count_since()`、`offset` 参数 |

前端（`index.html`、`app.js`）不做自动化测试，手动验证。

---

## 10. 实现顺序

1. `src/config.py` — 新增 `DashboardConfig` dataclass
2. `src/db.py` — 新增 `get_messages_count_since()`，`get_messages()` + `get_audit_log()` 加 `offset` 参数
3. `src/dashboard/auth.py` — Bearer Token 依赖
4. `src/dashboard/api.py` — 所有 API 端点
5. `src/server.py` — `set_db()` / `set_config()` 方法 + 集成 router + StaticFiles（顺序固定）
6. `src/main.py` — 条件启动 Dashboard，调用 `set_db()` / `set_config()`
7. `src/dashboard/static/index.html` — 页面骨架 + Alpine.js store
8. `src/dashboard/static/app.js` — 4 个页面组件
9. `src/dashboard/static/style.css` — 自定义样式
10. `tests/test_dashboard_auth.py` — 认证测试
11. `tests/test_dashboard_api.py` — API 端点测试
