"""Standalone in-memory smoke test for the MariaDB MCP server.

Run from the src/ directory:  uv run python tests/smoke_test.py

Exercises the core (always-registered) MCP tools against a live MariaDB via
FastMCP's in-memory client transport. Avoids the broken stdio-background-server
harness used by the unittest suite.
"""
import asyncio
import json
import sys

from fastmcp.client import Client
from server import MariaDBServer


def _text(result):
    # FastMCP CallToolResult: content blocks live on .content (newer) or are iterable (older)
    content = getattr(result, "content", result)
    return content[0].text


async def main():
    server = MariaDBServer(autocommit=True)
    await server.initialize_pool()
    server.register_tools()

    client = Client(server.mcp)
    failures = []

    async with client:
        # list_databases
        dbs = json.loads(_text(await client.call_tool("list_databases", {})))
        assert isinstance(dbs, list) and "information_schema" in dbs, dbs
        print(f"[ok] list_databases -> {len(dbs)} databases")

        # list_tables
        tables = json.loads(_text(await client.call_tool(
            "list_tables", {"database_name": "information_schema"})))
        assert isinstance(tables, list) and len(tables) > 0, tables
        print(f"[ok] list_tables(information_schema) -> {len(tables)} tables")

        # get_table_schema
        schema = json.loads(_text(await client.call_tool(
            "get_table_schema",
            {"database_name": "information_schema", "table_name": "TABLES"})))
        assert isinstance(schema, dict), schema
        print(f"[ok] get_table_schema(information_schema.TABLES)")

        # execute_sql
        rows = json.loads(_text(await client.call_tool(
            "execute_sql",
            {"sql_query": "SELECT 1 AS one", "database_name": "information_schema"})))
        assert rows and rows[0].get("one") == 1, rows
        print(f"[ok] execute_sql(SELECT 1) -> {rows}")

    await server.close_pool()

    if failures:
        print("FAILURES:", failures)
        sys.exit(1)
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
