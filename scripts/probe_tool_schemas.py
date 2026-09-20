# -*- coding: utf-8 -*-
"""Зонд к боевому MCP: что сервер РЕАЛЬНО отдаёт в схемах инструментов.

Клиент кэширует tools/list на всю жизнь приложения, поэтому правку схемы нельзя
проверить из текущей сессии — она увидит старую. Зонд открывает СВОЮ сессию и
печатает то, что отдаёт сервер: параметры с `default` (их не должно быть вовсе,
v1.90.0) и состав `required` у ключевых инструментов.

Запуск (ключ нужен, если у сервера задан MC_API_KEY):
    MC_API_KEY=... python scripts/probe_tool_schemas.py
    MC_URL=http://host:8765/sse python scripts/probe_tool_schemas.py
"""
import asyncio
import os
import sys

URL = os.environ.get("MC_URL", "http://localhost:8765/sse")
WATCH = ("session_note", "finish_task", "save_lesson", "save_tracking",
         "delete_article", "search")


async def main():
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    headers = {}
    key = os.environ.get("MC_API_KEY")
    if key:
        headers["Authorization"] = "Bearer %s" % key

    async with sse_client(URL, headers=headers or None) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print("инструментов:", len(tools))
            defaults = []
            for tool in tools:
                for pname, spec in ((tool.inputSchema or {}).get("properties") or {}).items():
                    if isinstance(spec, dict) and "default" in spec:
                        defaults.append("%s.%s" % (tool.name, pname))
            print("параметров с default (ожидается 0):", len(defaults), defaults[:6])
            by_name = {t.name: t for t in tools}
            for name in WATCH:
                tool = by_name.get(name)
                if not tool:
                    print("  нет инструмента", name)
                    continue
                schema = tool.inputSchema or {}
                req = schema.get("required") or []
                facts = (schema.get("properties") or {}).get("facts") or {}
                extra = (" facts.type=%s" % facts.get("type")) if facts else ""
                print("  %-16s required=%s%s" % (name, req, extra))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
