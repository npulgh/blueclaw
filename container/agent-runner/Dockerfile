FROM python:3.11-slim

RUN useradd -m -u 1000 agent

# Runner code lives at /app/runner — outside all volume mount points
# so that group_dir/ipc_dir mounts under /workspace never shadow it.
WORKDIR /app

COPY container/agent-runner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY container/agent-runner/ ./runner/

# Pre-create all volume mount points so they exist under --read-only.
# Docker requires mount point directories to exist in the image when
# the root filesystem is read-only.
# Note: /workspace/global_memory/CLAUDE.md is created as a file (not directory)
# because the host bind-mounts a single file here.
RUN mkdir -p /workspace/group /workspace/ipc /workspace/global \
             /workspace/project /workspace/global_memory \
    && touch /workspace/global_memory/CLAUDE.md

# Working directory for the agent process is the group dir (mounted at runtime)
WORKDIR /workspace/group

USER agent

ENTRYPOINT ["python", "/app/runner/main.py"]
