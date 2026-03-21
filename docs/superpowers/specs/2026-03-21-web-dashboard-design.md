# Web Dashboard 设计规格

**日期**: 2026-03-21
**状态**: 已批准
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

```
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

```
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

`WebhookServer.__init__()` 中追加：

```python
from src.dashboard.api import router as dashboard_router
from fastapi.staticfiles import StaticFiles

self.app.include_router(dashboard_router)
self.app.mount("/", StaticFiles(directory="src/dashboard/static", html=True), name="static")
```

`startup` 事件中注入 DB 实例：

```python
@self.app.on_event("startup")
async def _startup():
    self.app.state.db = self._db  # 由 main.py 在 start() 前设置
```

---

## 3. 认证

### 3.1 Static Bearer Token

- 从环境变量 `LYNXCLAW_DASHBOARD_TOKEN` 读取预设 Token
- 未配置时所有 `/api/*` 端点返回 `503 Service Unavailable`（强制配置，防止意外暴露）
- 前端首次访问弹出 Token 输入框，验证成功后存入 `sessionStorage`

### 3.2 实现（`auth.py`）

```python
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)):
    token = os.environ.get("LYNXCLAW_DASHBOARD_TOKEN")
    if not token:
        raise HTTPException(503, "Dashboard token not configured")
    if credentials.credentials != token:
        raise HTTPException(401, "Invalid token")
```

---

## 4. API 端点

所有端点前缀 `/api`，依赖 `verify_token`。

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/system/overview` | 系统概览统计 |
| GET | `/api/groups` | Groups 列表（含 session 状态） |
| GET | `/api/messages` | 消息历史，支持 `?group=&status=&limit=&offset=` |
| GET | `/api/audit` | 工具审计日志，支持 `?group=&limit=&offset=` |
| GET | `/api/tasks` | 定时任务列表，支持 `?group=` |
| GET | `/api/usage` | Token 用量，支持 `?group=&since=` (ISO 8601) |
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

### 4.2 分页规则

- `limit` 默认 50，最大 200
- `offset` 默认 0
- 响应体包含 `total`、`items` 字段

### 4.3 脱敏规则（`/api/config`）

以下字段替换为 `"***"`：
- `anthropic_api_key`
- `telegram.bot_token`
- `feishu.app_secret`

---

## 5. 前端设计

### 5.1 技术选型

| 库 | 版本 | 引入方式 |
|----|------|---------|
| Alpine.js | 3.x | CDN (`esm.sh` 或 `unpkg`) |
| Tailwind CSS | 3.x | CDN play.cdn |
| Chart.js | 4.x | CDN |

零构建步骤，静态文件直接由 FastAPI `StaticFiles` serve。

### 5.2 页面布局

```
┌─────────────────────────────────────────────────┐
│  🐱 Lynxclaw Dashboard          [Token: ●●●●●]  │  ← Header
├──────────┬──────────────────────────────────────┤
│          │                                      │
│  系统概览 │        主内容区                       │
│  消息审计 │   (动态渲染，hash 路由切换)            │
│  任务管理 │                                      │
│  指标图表 │                                      │
│          │                                      │
└──────────┴──────────────────────────────────────┘
```

### 5.3 页面规格

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
- Token 用量折线图：X 轴为时间（最近 7 天），Y 轴为 Token 数，按 Group 分线
- 配置只读面板：展示 container、streaming、security 等关键配置项

### 5.4 Alpine.js 全局 Store

```js
Alpine.store('app', {
  token: sessionStorage.getItem('dashboard_token') || '',
  currentPage: 'overview',
  loading: false,
  error: null,

  async api(path, params = {}) { /* fetch wrapper，自动带 Authorization header */ },
  setToken(t) { this.token = t; sessionStorage.setItem('dashboard_token', t); }
})
```

### 5.5 Token 登录流

1. 页面加载 → 检查 `sessionStorage.dashboard_token`
2. 无 Token → 显示模态框，要求输入
3. 输入后调用 `GET /api/system/overview` 验证
4. 401/503 → 显示错误；200 → 存储并进入主界面

---

## 6. 数据层

Dashboard API 完全复用 `src/db.py` 现有方法，无需新增 SQL：

| API 端点 | 调用的 DB 方法 |
|---------|--------------|
| `/api/system/overview` | `get_all_groups()`, `get_messages(limit=)`, `get_token_usage_all_groups()` |
| `/api/groups` | `get_all_groups()`, `get_all_sessions()` |
| `/api/messages` | `get_messages(group_name, status, limit)` |
| `/api/audit` | `get_audit_log(group_name, limit)` |
| `/api/tasks` | `get_all_tasks(group_name)` |
| `/api/usage` | `get_token_usage_all_groups()`, `get_token_usage(group_name, since)` |
| `/api/config` | 直接读 `Config` 对象（已在 `app.state` 中） |

---

## 7. 安全考量

- **只读**：所有 API 端点为 GET，无写入操作
- **认证强制**：Token 未配置时返回 503，不暴露任何数据
- **脱敏**：API key、bot token 等凭证在 `/api/config` 中替换为 `***`
- **本地优先**：Dashboard 默认只在 localhost 可访问（WebhookServer 默认绑定 `127.0.0.1`）
- **无 CORS 风险**：前后端同源（同端口），不需要 CORS 配置

---

## 8. 配置

在 `lynxclaw.config.yaml` 中新增 `dashboard` 段：

```yaml
dashboard:
  enabled: true   # 是否启用 dashboard（默认 false，与 server.enabled 联动）
```

`LYNXCLAW_DASHBOARD_TOKEN` 通过 `.env` 或环境变量设置，不进 config.yaml（避免明文提交）。

---

## 9. 测试策略

| 测试类型 | 文件 | 覆盖内容 |
|---------|------|---------|
| 单元测试 | `tests/test_dashboard_api.py` | 每个 API 端点的响应结构、分页、认证拒绝 |
| 认证测试 | `tests/test_dashboard_auth.py` | 无 Token 503、错误 Token 401、正确 Token 200 |

前端（`index.html`、`app.js`）不做自动化测试，手动验证。

---

## 10. 实现顺序

1. `src/dashboard/auth.py` — Bearer Token 依赖
2. `src/dashboard/api.py` — 所有 API 端点
3. `src/server.py` — 集成 dashboard router + static files
4. `src/dashboard/static/index.html` — 页面骨架 + Alpine.js store
5. `src/dashboard/static/app.js` — 4 个页面组件
6. `src/dashboard/static/style.css` — 自定义样式
7. `tests/test_dashboard_api.py` — API 测试
8. `tests/test_dashboard_auth.py` — 认证测试
