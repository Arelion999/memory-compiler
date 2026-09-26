"""memory-compiler MCP server — entry point."""
import os

# Офлайн-режим Hugging Face выбирается ДО импорта ML-библиотек: huggingface_hub читает
# HF_HUB_OFFLINE один раз, при импорте. Модели в кеше — процесс офлайн; нет — этот запуск
# скачает их один раз (memory_compiler/hf_offline.py). Порядок держит
# tests/test_first_start_guards.py: импорт tools/api выше этой строки вернул бы тихую
# деградацию поиска на свежей установке.
from memory_compiler import hf_offline

hf_offline.apply()

import uvicorn  # noqa: E402
from memory_compiler.tools import app  # noqa: E402
from memory_compiler.api import create_starlette_app  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8765"))
    starlette_app = create_starlette_app(app)
    uvicorn.run(starlette_app, host=host, port=port)
