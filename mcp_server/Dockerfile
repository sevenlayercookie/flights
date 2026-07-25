FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    HOME=/tmp

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home /app app

COPY pyproject.toml README.md LICENSE ./
COPY fast_flights ./fast_flights
COPY mcp_server/requirements.txt /tmp/mcp-requirements.txt
RUN pip install --no-cache-dir . \
    --requirement /tmp/mcp-requirements.txt

COPY --chown=app:app mcp_server/server.py ./server.py

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

CMD ["python", "server.py"]
