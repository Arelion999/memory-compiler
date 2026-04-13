FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=120 -r requirements.txt

COPY memory_compiler/ memory_compiler/
COPY server.py .

ENV KNOWLEDGE_DIR=/knowledge
ENV MCP_TRANSPORT=sse
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')"

CMD ["python", "server.py"]
