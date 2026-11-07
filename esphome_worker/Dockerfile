FROM ghcr.io/esphome/esphome:2026.10.2
USER root
COPY worker.py runner.py /opt/mcp-worker/
WORKDIR /opt/mcp-worker
RUN mkdir -p /data/worker && chown 10001:10001 /data/worker
ENV PYTHONUNBUFFERED=1 WORKER_DATA_DIR=/data/worker
ENTRYPOINT ["python3", "/opt/mcp-worker/worker.py"]
