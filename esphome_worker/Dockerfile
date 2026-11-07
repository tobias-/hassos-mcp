FROM ghcr.io/esphome/esphome:2026.10.2
USER root
COPY worker.py runner.py /opt/mcp-worker/
WORKDIR /opt/mcp-worker
RUN mkdir -p /data/worker && chown 0:10001 /data/worker
ENV PYTHONUNBUFFERED=1 WORKER_DATA_DIR=/data/worker
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6053/health', timeout=3)"
ENTRYPOINT ["python3", "/opt/mcp-worker/worker.py"]
