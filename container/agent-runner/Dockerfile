FROM python:3.11-slim

RUN useradd -m -u 1000 agent

WORKDIR /workspace/group

COPY container/agent-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY container/agent-runner/ ./runner/

USER agent

ENTRYPOINT ["python", "runner/main.py"]
