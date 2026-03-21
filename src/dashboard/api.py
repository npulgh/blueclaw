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
    # Simplified total count — replace with COUNT(*) query for large datasets
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
