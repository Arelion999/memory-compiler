FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .

# torch — ТОЛЬКО CPU-колесо. NAS на Celeron J4125, GPU нет вовсе, а дефолтные колёса
# с PyPI тянут nvidia-* + triton: замер 20.08.2026 — 3.4 ГБ из 8.67 ГБ образа (67%),
# кода для железа, которого здесь не существует. Ставится ОТДЕЛЬНЫМ шагом и ДО
# requirements.txt: тогда sentence-transformers видит torch уже установленным и не
# подтягивает GPU-вариант с PyPI. Через --index-url (не --extra-index-url): второй
# оставляет PyPI равноправным источником, и выбор колеса становится делом случая.
RUN pip install --no-cache-dir --timeout=120 --index-url https://download.pytorch.org/whl/cpu torch
RUN pip install --no-cache-dir --timeout=120 -r requirements.txt

COPY memory_compiler/ memory_compiler/
COPY server.py VERSION ./

ENV KNOWLEDGE_DIR=/knowledge
ENV MCP_TRANSPORT=sse
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/health')"

CMD ["python", "server.py"]
