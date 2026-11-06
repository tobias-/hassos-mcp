FROM ghcr.io/openai/tunnel-client:v0.0.19 AS tunnel
FROM python:3.12-slim AS runtime
COPY --from=tunnel /usr/bin/tunnel-client /usr/local/bin/tunnel-client
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home mcp && mkdir /data && chown mcp:mcp /data
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data/state
ENTRYPOINT ["hassio-mcp-start"]

# Compose uses an unprivileged user with a dedicated data volume.
FROM runtime AS compose
USER mcp

# HAOS provides root-owned app options and config mounts. No host privileges.
# Keep this the final stage so Supervisor's default build selects it.
FROM runtime AS haos
USER root
