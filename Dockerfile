FROM python:3.11-slim
# This is from oddjob/drone, put back after a new arm build server is available.

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    ca-certificates \
    git \
    && apt-get auto-remove \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

RUN git clone -b main --single-branch --branch updates/logging https://github.com/jeffdyke/mariadb-mcp.git /src/mariadb-mcp
WORKDIR /src/mariadb-mcp
RUN uv sync
COPY build/support_files/mariadb-mcp/.env .

EXPOSE 9001
VOLUME [ "/var/log/bondlink" ]
ENTRYPOINT ["uv", "run", "src/server.py", "--host", "0.0.0.0", "--port", "9001", "--transport", "http"]
