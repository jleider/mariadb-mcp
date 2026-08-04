"""
Integration tests for the MariaDB MCP tools, calling the server methods
directly against a real MariaDB — no mocks.

Start the database first:
    docker compose -f docker-compose.test.yml up -d --wait

These skip when the container is unreachable. See src/tests/support.py.

History: this file previously documented a manual test plan executed by hand
through an AI assistant, with the note "cannot be executed directly". Each of
those twelve steps is now a real assertion against the fixture schema. The old
class also defined `async def setUp`, which IsolatedAsyncioTestCase never
awaits, so `self.server` was never assigned and every run errored in tearDown.
"""
import unittest

from src.tests.support import MariaDBIntegrationTestCase, TEST_DB_NAME


class TestMariaDBMCPTools(MariaDBIntegrationTestCase):

    # --- Step 1-5: basic tool behaviour ---

    async def test_step_1_list_databases(self):
        """Returns a list of database-name strings, including the system schemas."""
        result = await self.server.list_databases()

        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(db, str) for db in result))
        for expected in ('information_schema', 'mysql', 'sys', TEST_DB_NAME):
            self.assertIn(expected, result)

    async def test_step_2_list_tables_valid_db(self):
        """Lists tables for a known database."""
        result = await self.server.list_tables('information_schema')

        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(table, str) for table in result))
        self.assertIn('ALL_PLUGINS', result)
        self.assertIn('APPLICABLE_ROLES', result)

    async def test_step_3_get_schema_valid_table(self):
        """Retrieves the schema for a known table."""
        result = await self.server.get_table_schema('information_schema', 'TABLES')

        self.assertEqual(result['table_name'], 'TABLES')
        self.assertIn('TABLE_NAME', result['columns'])
        self.assertIn('TABLE_COMMENT', result['columns'])
        for column in result['columns'].values():
            self.assertIn('type', column)
            self.assertIn('comment', column)

    async def test_step_4_execute_simple_select(self):
        """Basic SQL execution."""
        rows = await self.server.execute_sql(
            'SELECT id, email, status FROM documented ORDER BY id', TEST_DB_NAME)

        self.assertIsInstance(rows, list)
        # The fixture inserts no rows; the point is that the query succeeds.
        self.assertEqual(rows, [])

    async def test_step_5_execute_parameterized_select(self):
        """Parameterized query execution."""
        rows = await self.server.execute_sql(
            'SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
            TEST_DB_NAME,
            [TEST_DB_NAME],
        )

        names = {row['TABLE_NAME'] for row in rows}
        self.assertIn('documented', names)
        self.assertIn('parents', names)

    # --- Step 6-7: error handling ---

    async def test_step_6_list_tables_nonexistent_db(self):
        """Unknown database surfaces an error."""
        with self.assertRaises(Exception):
            await self.server.list_tables('db_that_does_not_exist')

    async def test_step_7_get_schema_nonexistent_table(self):
        """Unknown table surfaces an error."""
        with self.assertRaises(Exception):
            await self.server.get_table_schema(TEST_DB_NAME, 'table_that_does_not_exist')

    # --- Step 8-9: more complex SQL ---

    async def test_step_8_execute_complex_join(self):
        """A JOIN across information_schema tables."""
        rows = await self.server.execute_sql(
            'SELECT t.TABLE_NAME, COUNT(c.COLUMN_NAME) AS column_count '
            'FROM information_schema.TABLES t '
            'JOIN information_schema.COLUMNS c '
            '  ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME '
            'WHERE t.TABLE_SCHEMA = %s '
            'GROUP BY t.TABLE_NAME ORDER BY t.TABLE_NAME',
            TEST_DB_NAME,
            [TEST_DB_NAME],
        )

        counts = {row['TABLE_NAME']: row['column_count'] for row in rows}
        self.assertEqual(counts['documented'], 6)
        self.assertEqual(counts['undocumented'], 2)

    async def test_step_9_execute_aggregation(self):
        """COUNT/GROUP BY aggregation."""
        rows = await self.server.execute_sql(
            'SELECT TABLE_SCHEMA, COUNT(*) AS n FROM information_schema.tables '
            'WHERE TABLE_SCHEMA = %s GROUP BY TABLE_SCHEMA',
            TEST_DB_NAME,
            [TEST_DB_NAME],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['TABLE_SCHEMA'], TEST_DB_NAME)
        # Cross-check against list_tables rather than hardcoding a count, so
        # adding a fixture table does not break this test.
        expected = len(await self.server.list_tables(TEST_DB_NAME))
        self.assertEqual(rows[0]['n'], expected)

    # --- Step 10-12: parameter and escaping edge cases ---

    async def test_step_10_execute_param_empty_string(self):
        """An empty-string parameter binds fine and matches nothing."""
        rows = await self.server.execute_sql(
            'SELECT COUNT(*) AS n FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
            TEST_DB_NAME,
            [''],
        )

        self.assertEqual(rows[0]['n'], 0)

    async def test_step_11_execute_param_mismatch(self):
        """Too few parameters for the placeholders is an error."""
        with self.assertRaises(Exception):
            await self.server.execute_sql(
                'SELECT * FROM information_schema.tables '
                'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s',
                TEST_DB_NAME,
                ['information_schema'],
            )

    async def test_step_11b_execute_empty_parameter_list(self):
        """An empty list is not the same as no parameters; %s stays unbound."""
        with self.assertRaises(Exception):
            await self.server.execute_sql(
                'SELECT * FROM information_schema.tables WHERE TABLE_SCHEMA = %s',
                TEST_DB_NAME,
                [],
            )

    async def test_step_12_execute_show_command(self):
        """SHOW works, and a literal '%' needs no escaping without parameters."""
        rows = await self.server.execute_sql(
            "SHOW VARIABLES LIKE 'version%'", TEST_DB_NAME)

        variables = {row['Variable_name'] for row in rows}
        self.assertIn('version', variables)

    async def test_step_12b_doubled_percent_still_works_in_like(self):
        """
        `%%` in a LIKE pattern keeps working, because SQL treats it as two
        consecutive zero-or-more wildcards — the same match as a single `%`.
        Callers who doubled `%` to work around the old formatting bug are
        unaffected.
        """
        rows = await self.server.execute_sql(
            "SHOW VARIABLES LIKE 'version%%'", TEST_DB_NAME)

        variables = {row['Variable_name'] for row in rows}
        self.assertIn('version', variables)


class TestLiteralPercentHandling(MariaDBIntegrationTestCase):
    """
    A query with no parameters is sent to the server verbatim, so `%` is a
    plain character. Once parameters are supplied, `%s` binding applies and a
    literal `%` must be doubled — the standard DB-API `format` paramstyle.
    """

    async def test_like_wildcard_needs_no_escaping_without_parameters(self):
        """The idiom that the old `params or ()` behaviour broke outright."""
        rows = await self.server.execute_sql(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = '{TEST_DB_NAME}' AND TABLE_NAME LIKE '%documented%'",
            TEST_DB_NAME,
        )

        names = {row['TABLE_NAME'] for row in rows}
        self.assertEqual(names, {'documented', 'undocumented', 'documented_view'})

    async def test_literal_percent_in_string_is_returned_intact(self):
        rows = await self.server.execute_sql("SELECT '100%' AS pct", TEST_DB_NAME)

        self.assertEqual(rows[0]['pct'], '100%')

    async def test_percent_in_like_pattern_expression(self):
        rows = await self.server.execute_sql(
            "SELECT 'a%b' LIKE 'a%%b' AS matched", TEST_DB_NAME)

        self.assertEqual(rows[0]['matched'], 1)

    async def test_wildcard_passed_as_a_bound_parameter(self):
        """The preferred form: the wildcard travels in the value, not the SQL."""
        rows = await self.server.execute_sql(
            'SELECT TABLE_NAME FROM information_schema.TABLES '
            'WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE %s',
            TEST_DB_NAME,
            [TEST_DB_NAME, '%documented%'],
        )

        names = {row['TABLE_NAME'] for row in rows}
        self.assertEqual(names, {'documented', 'undocumented', 'documented_view'})

    async def test_literal_percent_must_be_doubled_when_parameters_are_used(self):
        """Standard `format` paramstyle behaviour, unchanged by the fix."""
        rows = await self.server.execute_sql(
            "SELECT '100%%' AS pct, %s AS bound", TEST_DB_NAME, ['x'])

        self.assertEqual(rows[0]['pct'], '100%')
        self.assertEqual(rows[0]['bound'], 'x')

    async def test_single_percent_with_parameters_still_raises(self):
        """An unescaped `%` alongside `%s` is a genuine caller error."""
        with self.assertRaises(Exception):
            await self.server.execute_sql(
                "SELECT '100%' AS pct, %s AS bound", TEST_DB_NAME, ['x'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
