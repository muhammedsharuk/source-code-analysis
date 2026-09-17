FROM python:3.12-slim

# System dependencies
# curl + ca-certificates are required to download the pinned
# codebase-memory-mcp release. git is required by utils/git_clone.py to
# clone a caller-supplied repo URL (the zip-upload path doesn't need it).
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  curl \
  ca-certificates \
  git \
  && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Install codebase-memory-mcp v0.8.1
# ---------------------------------------------------------------------------
#
# We intentionally download the exact v0.8.1 release asset instead of using
# the installer from the main branch. This keeps the Docker image pinned to
# the version used by the application.
#
# The application depends on the v0.8.1 search_graph response format:
#
# {
#   "results": [
#       {
#           "file_path": "...",
#           ...
#       }
#   ]
# }
#
# Newer versions changed this response format, so upgrading CBM can break
# the application's graph parsing logic.
#
# The Linux AMD64 portable release is used because this Docker image targets
# the standard Linux AMD64 environment.

ARG CBM_VERSION=0.8.1
ARG CBM_ARCHIVE=codebase-memory-mcp-linux-amd64-portable.tar.gz

RUN curl -fL \
  "https://github.com/DeusData/codebase-memory-mcp/releases/download/v${CBM_VERSION}/${CBM_ARCHIVE}" \
  -o /tmp/codebase-memory-mcp.tar.gz \
  && mkdir -p /tmp/codebase-memory-mcp \
  && tar -xzf /tmp/codebase-memory-mcp.tar.gz \
  -C /tmp/codebase-memory-mcp \
  && find /tmp/codebase-memory-mcp \
  -type f \
  -name "codebase-memory-mcp" \
  -exec cp {} /usr/local/bin/codebase-memory-mcp \; \
  && chmod +x /usr/local/bin/codebase-memory-mcp \
  && rm -rf /tmp/codebase-memory-mcp \
  /tmp/codebase-memory-mcp.tar.gz \
  && codebase-memory-mcp --version

WORKDIR /app

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

COPY . .

EXPOSE 8000

# codebase-memory-mcp stores knowledge-graph indexes under:
#
#     ~/.cache/codebase-memory-mcp
#
# Since the container runs as root by default, this is:
#
#     /root/.cache/codebase-memory-mcp
#
# Mount this directory as a Docker volume if the indexes need to survive
# container restarts.

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]