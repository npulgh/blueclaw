# SPDX-FileCopyrightText: 2026 lynxpurr
# SPDX-License-Identifier: AGPL-3.0-or-later

FROM python:3.11-slim

RUN useradd -m -u 1000 proxy

WORKDIR /app

COPY container/proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY container/proxy/proxy_server.py .

USER proxy

ENTRYPOINT ["python", "proxy_server.py"]
