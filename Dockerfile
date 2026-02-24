FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp \
    MOCK_DB_PATH=/app/data/mock_external_service.db

WORKDIR /app

COPY mcp_server.py index.html ./
RUN pip install --no-cache-dir "fastmcp>=3.0.2"

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "mcp_server.py"]
