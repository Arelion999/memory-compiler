"""memory-compiler MCP server — entry point."""
import os
import uvicorn
from memory_compiler.tools import app
from memory_compiler.api import create_starlette_app

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8765"))
    starlette_app = create_starlette_app(app)
    uvicorn.run(starlette_app, host=host, port=port)
