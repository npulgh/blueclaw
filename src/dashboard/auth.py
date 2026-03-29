# LynxClaw - AI Coding Agent Framework
# Copyright (C) 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

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
