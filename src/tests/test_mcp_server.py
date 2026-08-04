"""
Integration tests for the MCP tools, driven through a fastmcp client against a
real MariaDB — no mocks.

Start the database first:
    docker compose -f docker-compose.test.yml up -d --wait

These skip when the container is unreachable. See src/tests/support.py.

Note: `Client(server.mcp)` is an in-memory client that talks to the server
object directly, so there is no need to run the stdio transport. An earlier
version of this file also started `run_async_server('stdio')` in a background
task; that is redundant and fails under the test runner, because the stdio
transport wraps sys.stdin, which the runner has already closed.
"""
import unittest

import fastmcp
from fastmcp.client import Client

from src.tests.support import (
    MariaDBIntegrationTestCase,
    TEST_DB_NAME,
    tool_payload,
)


class _ClientTestCase(MariaDBIntegrationTestCase):
    """Registers the MCP tools and exposes an in-memory client."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.server.register_tools()
        self.client = Client(self.server.mcp)


class TestMariaDBMCPTools(_ClientTestCase):

    async def test_list_databases(self):
        async with self.client:
            result = await self.client.call_tool('list_databases', {})

        databases = tool_payload(result)
        self.assertIsInstance(databases, list)
        self.assertTrue(all(isinstance(db, str) for db in databases))
        for expected in ('information_schema', 'mysql', 'performance_schema', 'sys'):
            self.assertIn(expected, databases)
        self.assertIn(TEST_DB_NAME, databases)

    async def test_list_tables_valid_db(self):
        async with self.client:
            result = await self.client.call_tool(
                'list_tables', {'database_name': 'information_schema'})

        tables = tool_payload(result)
        self.assertIsInstance(tables, list)
        self.assertTrue(all(isinstance(table, str) for table in tables))
        for sys_table in ('ALL_PLUGINS', 'APPLICABLE_ROLES'):
            self.assertIn(sys_table, tables)

    async def test_list_tables_returns_fixture_tables(self):
        async with self.client:
            result = await self.client.call_tool(
                'list_tables', {'database_name': TEST_DB_NAME})

        tables = tool_payload(result)
        for expected in ('documented', 'undocumented', 'parents', 'documented_view'):
            self.assertIn(expected, tables)

    async def test_list_tables_invalid_db(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'list_tables', {'database_name': 'no_such_database'})

    async def test_get_schema_valid_table(self):
        async with self.client:
            result = await self.client.call_tool(
                'get_table_schema',
                {'database_name': 'information_schema', 'table_name': 'ALL_PLUGINS'})

        payload = tool_payload(result)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload['table_name'], 'ALL_PLUGINS')
        self.assertIsInstance(payload['comment'], str)
        self.assertTrue(all(isinstance(value, dict) for value in payload['columns'].values()))
        # Every column carries a comment field, even when empty.
        self.assertTrue(all('comment' in value for value in payload['columns'].values()))

    async def test_get_schema_invalid_table(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'get_table_schema',
                    {'database_name': 'information_schema', 'table_name': 'INVALID_TABLE'})

    async def test_get_schema_with_relations_valid_table(self):
        async with self.client:
            result = await self.client.call_tool(
                'get_table_schema_with_relations',
                {'database_name': TEST_DB_NAME, 'table_name': 'documented'})

        payload = tool_payload(result)
        self.assertEqual(payload['table_name'], 'documented')
        self.assertEqual(
            payload['columns']['parent_id']['foreign_key']['referenced_table'], 'parents')

    async def test_execute_sql(self):
        async with self.client:
            result = await self.client.call_tool(
                'execute_sql',
                {'database_name': TEST_DB_NAME, 'sql_query': 'SELECT 1 AS one'})

        rows = tool_payload(result)
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]['one'], 1)

    async def test_execute_sql_invalid_query(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'execute_sql',
                    {'database_name': 'information_schema',
                     'sql_query': 'SELECT * FROM information_schema.INVALID_TABLE WHERE 1=1'})

    async def test_execute_sql_parameterized(self):
        async with self.client:
            result = await self.client.call_tool(
                'execute_sql',
                {'database_name': TEST_DB_NAME,
                 'sql_query': 'SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
                 'parameters': [TEST_DB_NAME]})

        rows = tool_payload(result)
        self.assertIsInstance(rows, list)
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertGreater(len(rows), 1)

    async def test_execute_sql_parameterized_binds_values(self):
        """A bound parameter must filter, not be interpolated blindly."""
        async with self.client:
            result = await self.client.call_tool(
                'execute_sql',
                {'database_name': TEST_DB_NAME,
                 'sql_query': 'SELECT COUNT(*) AS n FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
                 'parameters': ['no_such_database']})

        self.assertEqual(tool_payload(result)[0]['n'], 0)

    async def test_execute_sql_parameterized_invalid_database(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'execute_sql',
                    {'database_name': 'information_schema_INVALID',
                     'sql_query': 'SELECT * FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
                     'parameters': ['information_schema']})

    async def test_execute_sql_parameterized_empty(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'execute_sql',
                    {'database_name': 'information_schema',
                     'sql_query': 'SELECT * FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
                     'parameters': []})

    async def test_execute_sql_parameterized_mismatch(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'execute_sql',
                    {'database_name': 'information_schema',
                     'sql_query': 'SELECT * FROM information_schema.tables '
                                  'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s',
                     'parameters': ['information_schema']})

    async def test_create_database(self):
        db_name = 'mcp_create_target'
        # Start from a known state so both branches are asserted.
        await self.server._execute_query(f"DROP DATABASE IF EXISTS `{db_name}`")

        async with self.client:
            created = tool_payload(await self.client.call_tool(
                'create_database', {'database_name': db_name}))
            self.assertEqual(created['status'], 'success')
            self.assertEqual(created['database_name'], db_name)

            again = tool_payload(await self.client.call_tool(
                'create_database', {'database_name': db_name}))
            self.assertEqual(again['status'], 'exists')

        await self.server._execute_query(f"DROP DATABASE IF EXISTS `{db_name}`")

    async def test_create_database_invalid_name(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'create_database', {'database_name': 'bad-name'})

    async def test_tools_are_registered(self):
        async with self.client:
            names = {tool.name for tool in await self.client.list_tools()}

        self.assertEqual(names, {
            'list_databases', 'list_tables', 'get_table_schema',
            'get_table_schema_with_relations', 'execute_sql', 'create_database',
        })


class TestReadOnlyMode(_ClientTestCase):
    read_only = True

    async def test_create_database_blocked_in_read_only_mode(self):
        async with self.client:
            with self.assertRaises(fastmcp.exceptions.ToolError):
                await self.client.call_tool(
                    'create_database', {'database_name': 'should_not_be_created'})

    async def test_select_still_allowed_in_read_only_mode(self):
        async with self.client:
            result = await self.client.call_tool(
                'execute_sql',
                {'database_name': TEST_DB_NAME, 'sql_query': 'SELECT 1 AS one'})

        self.assertEqual(tool_payload(result)[0]['one'], 1)

    async def test_schema_tools_still_work_in_read_only_mode(self):
        async with self.client:
            result = await self.client.call_tool(
                'get_table_schema',
                {'database_name': TEST_DB_NAME, 'table_name': 'documented'})

        payload = tool_payload(result)
        self.assertEqual(
            payload['comment'],
            'Fully documented table used to verify comment passthrough.',
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
