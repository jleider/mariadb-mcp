"""
Integration tests for table/column comments (DL-5840), run against a real
MariaDB with a known schema — no mocks.

Start the database first:
    docker compose -f docker-compose.test.yml up -d --wait

Then:
    python -m unittest src.tests.test_integration_schema_comments -v

These skip (rather than fail) when the container is not reachable. The fixture
schema lives in src/tests/fixtures/01-test-schema.sql.
"""
import unittest
from unittest.mock import patch

from src.tests.support import (
    MariaDBIntegrationTestCase,
    TEST_DB_NAME,
    TEST_DB_OTHER_NAME,
    tool_payload,
)

# Exact text from the fixture, so a silently-truncated or wrongly-scoped
# comment fails the assertion rather than passing a "looks non-empty" check.
DOCUMENTED_TABLE_COMMENT = 'Fully documented table used to verify comment passthrough.'
DOCUMENTED_COLUMN_COMMENTS = {
    'id': 'Primary key.',
    'parent_id': 'FK -> parents.id; the owning parent record.',
    'email': 'Login address; unique per parent.',
    'status': 'Lifecycle state: active, paused or closed.',
    'uncommented_col': '',
    'notes': 'Free-form notes; may contain "quotes", commas and ünicode ✓.',
}


class TestGetTableSchemaComments(MariaDBIntegrationTestCase):

    async def test_table_comment_is_returned_verbatim(self):
        result = await self.server.get_table_schema(TEST_DB_NAME, 'documented')

        self.assertEqual(result['table_name'], 'documented')
        self.assertEqual(result['comment'], DOCUMENTED_TABLE_COMMENT)

    async def test_every_column_comment_matches_the_schema(self):
        result = await self.server.get_table_schema(TEST_DB_NAME, 'documented')

        actual = {name: col['comment'] for name, col in result['columns'].items()}
        self.assertEqual(actual, DOCUMENTED_COLUMN_COMMENTS)

    async def test_uncommented_column_returns_empty_string(self):
        result = await self.server.get_table_schema(TEST_DB_NAME, 'documented')

        self.assertEqual(result['columns']['uncommented_col']['comment'], '')

    async def test_unicode_and_quotes_survive_intact(self):
        result = await self.server.get_table_schema(TEST_DB_NAME, 'documented')

        self.assertEqual(
            result['columns']['notes']['comment'],
            'Free-form notes; may contain "quotes", commas and ünicode ✓.',
        )

    async def test_fully_undocumented_table_returns_empty_strings(self):
        result = await self.server.get_table_schema(TEST_DB_NAME, 'undocumented')

        self.assertEqual(result['comment'], '')
        self.assertEqual(
            {name: col['comment'] for name, col in result['columns'].items()},
            {'id': '', 'value': ''},
        )

    async def test_column_named_comment_does_not_shadow_table_comment(self):
        """The reason columns are nested under 'columns' rather than flat."""
        result = await self.server.get_table_schema(TEST_DB_NAME, 'with_comment_column')

        self.assertEqual(
            result['comment'],
            'Table with a column named comment, to catch namespace collisions.',
        )
        self.assertEqual(
            result['columns']['comment']['comment'],
            'A column literally named comment.',
        )

    async def test_view_placeholder_comment_is_suppressed(self):
        """MariaDB reports TABLE_COMMENT='VIEW' for views; that is not a comment."""
        result = await self.server.get_table_schema(TEST_DB_NAME, 'documented_view')

        self.assertEqual(result['comment'], '')
        self.assertNotEqual(result['comment'], 'VIEW')
        # The view still describes its columns.
        self.assertEqual(set(result['columns']), {'id', 'email', 'status'})

    async def test_comments_are_scoped_to_the_requested_database(self):
        """
        Both databases contain a `documented` table with different comments, so
        a lookup missing TABLE_SCHEMA would return the other database's text.
        """
        primary = await self.server.get_table_schema(TEST_DB_NAME, 'documented')
        other = await self.server.get_table_schema(TEST_DB_OTHER_NAME, 'documented')

        self.assertEqual(primary['comment'], DOCUMENTED_TABLE_COMMENT)
        self.assertEqual(other['comment'], 'Decoy table sharing a name with mcp_test.documented.')
        self.assertEqual(primary['columns']['id']['comment'], 'Primary key.')
        self.assertEqual(
            other['columns']['id']['comment'],
            'Second-database id, NOT the mcp_test one.',
        )

    async def test_second_database_table_comments(self):
        result = await self.server.get_table_schema(TEST_DB_OTHER_NAME, 'other_documented')

        self.assertEqual(
            result['comment'],
            'Table in a second database, to verify cross-database comment lookups.',
        )
        self.assertEqual(
            result['columns']['label']['comment'],
            'Label column in the second database.',
        )

    async def test_adding_comments_did_not_change_existing_fields(self):
        """
        Regression guard for the choice to keep DESCRIBE as the source of
        types/keys/defaults. INFORMATION_SCHEMA.COLUMN_DEFAULT reports this
        default as "'active'" (quoted); DESCRIBE reports it as "active".
        """
        cols = (await self.server.get_table_schema(TEST_DB_NAME, 'documented'))['columns']

        self.assertEqual(cols['status']['default'], 'active')
        self.assertEqual(cols['id']['key'], 'PRI')
        self.assertEqual(cols['email']['key'], 'UNI')
        self.assertEqual(cols['parent_id']['key'], 'MUL')
        self.assertEqual(cols['id']['extra'], 'auto_increment')
        self.assertEqual(cols['email']['type'], 'varchar(255)')
        self.assertFalse(cols['email']['nullable'])
        self.assertTrue(cols['parent_id']['nullable'])

    async def test_unknown_table_still_raises(self):
        with self.assertRaises(Exception):
            await self.server.get_table_schema(TEST_DB_NAME, 'no_such_table')

    async def test_schema_is_fetched_in_a_single_query(self):
        """
        Sourcing everything from INFORMATION_SCHEMA is what allows one round-trip
        instead of DESCRIBE plus two comment lookups.
        """
        with patch.object(self.server, '_execute_query',
                          wraps=self.server._execute_query) as spy:
            await self.server.get_table_schema(TEST_DB_NAME, 'documented')

        self.assertEqual(spy.call_count, 1)

    async def test_with_relations_uses_two_queries(self):
        with patch.object(self.server, '_execute_query',
                          wraps=self.server._execute_query) as spy:
            await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'documented')

        self.assertEqual(spy.call_count, 2)


class TestColumnDefaultNormalization(MariaDBIntegrationTestCase):
    """
    INFORMATION_SCHEMA.COLUMN_DEFAULT returns SQL literals, so the server decodes
    them. These assert the decoded values against a real server rather than
    against the mocked expectations in test_table_schema_comments.py.
    """

    async def test_defaults_are_decoded_to_plain_values(self):
        cols = (await self.server.get_table_schema(
            TEST_DB_NAME, 'column_defaults'))['columns']

        expected = {
            's_str': 'active',
            's_empty': '',
            's_quote': "it's",
            's_backslash': 'a\\b',
            's_null_word': 'NULL',
            'n_int': '5',
            'n_dec': '1.50',
            'e_enum': 'b',
            'd_none': None,
            'd_expnull': None,
            'ts_now': 'current_timestamp()',
            't_json': '[]',
        }
        actual = {name: col['default'] for name, col in cols.items()}
        self.assertEqual(actual, expected)

    async def test_text_column_default_is_not_double_quoted(self):
        """
        DESCRIBE reports a TEXT default as the quoted expression "'[]'", which
        misstates the value. Reading COLUMN_DEFAULT and decoding it yields the
        actual two characters. Two columns in the real BondLink schema
        (IceCusips.call_schedule and .sink_schedule) are affected.
        """
        cols = (await self.server.get_table_schema(
            TEST_DB_NAME, 'column_defaults'))['columns']

        self.assertEqual(cols['t_json']['default'], '[]')
        self.assertNotEqual(cols['t_json']['default'], "'[]'")

    async def test_absent_default_is_none_not_the_string_null(self):
        """COLUMN_DEFAULT reports no-default as the string 'NULL'."""
        cols = (await self.server.get_table_schema(
            TEST_DB_NAME, 'column_defaults'))['columns']

        self.assertIsNone(cols['d_none']['default'])
        self.assertNotEqual(cols['d_none']['default'], 'NULL')
        # ...while a genuine string default of "NULL" is preserved.
        self.assertEqual(cols['s_null_word']['default'], 'NULL')

    async def test_comments_still_present_on_defaults_table(self):
        cols = (await self.server.get_table_schema(
            TEST_DB_NAME, 'column_defaults'))['columns']

        self.assertEqual(cols['s_str']['comment'], 'Quoted string default.')
        self.assertEqual(cols['ts_now']['comment'], 'Expression default.')


class TestGetTableSchemaWithRelationsComments(MariaDBIntegrationTestCase):

    async def test_table_and_column_comments_are_present(self):
        result = await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'documented')

        self.assertEqual(result['comment'], DOCUMENTED_TABLE_COMMENT)
        self.assertEqual(
            {name: col['comment'] for name, col in result['columns'].items()},
            DOCUMENTED_COLUMN_COMMENTS,
        )

    async def test_foreign_key_metadata_is_correct(self):
        result = await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'documented')
        fk = result['columns']['parent_id']['foreign_key']

        self.assertEqual(fk['constraint_name'], 'fk_documented_parent')
        self.assertEqual(fk['referenced_table'], 'parents')
        self.assertEqual(fk['referenced_column'], 'id')
        self.assertEqual(fk['on_update'], 'CASCADE')
        self.assertEqual(fk['on_delete'], 'SET NULL')

    async def test_comments_and_foreign_keys_coexist(self):
        """The FK merge step must not drop the comment it copies over."""
        result = await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'documented')

        self.assertEqual(
            result['columns']['parent_id']['comment'],
            'FK -> parents.id; the owning parent record.',
        )
        self.assertIsNotNone(result['columns']['parent_id']['foreign_key'])
        for name, col in result['columns'].items():
            self.assertIn('comment', col, f"column {name} lost its comment")
            self.assertIn('foreign_key', col)

    async def test_non_foreign_key_columns_have_null_foreign_key(self):
        result = await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'documented')

        for name in ('id', 'email', 'status', 'notes'):
            self.assertIsNone(result['columns'][name]['foreign_key'], name)

    async def test_table_without_foreign_keys_still_returns_comments(self):
        result = await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'parents')

        self.assertEqual(result['comment'], 'Parent records referenced by documented.parent_id.')
        self.assertEqual(result['columns']['name']['comment'], 'Display name of the parent record.')
        self.assertIsNone(result['columns']['name']['foreign_key'])

    async def test_view_placeholder_suppressed_with_relations(self):
        result = await self.server.get_table_schema_with_relations(TEST_DB_NAME, 'documented_view')

        self.assertEqual(result['comment'], '')


class TestSchemaCommentsThroughMCPTools(MariaDBIntegrationTestCase):
    """
    Exercises the registered MCP tool wrappers via an in-memory fastmcp client,
    which is the path a real client takes. The wrapper docstrings are the tool
    descriptions, and the wrappers are what actually get called.
    """

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from fastmcp.client import Client

        self.server.register_tools()
        self.client = Client(self.server.mcp)

    async def test_get_table_schema_tool_returns_comments(self):
        async with self.client:
            result = await self.client.call_tool(
                'get_table_schema',
                {'database_name': TEST_DB_NAME, 'table_name': 'documented'},
            )

        payload = tool_payload(result)
        self.assertEqual(payload['comment'], DOCUMENTED_TABLE_COMMENT)
        self.assertEqual(payload['columns']['email']['comment'],
                         'Login address; unique per parent.')

    async def test_with_relations_tool_returns_comments_and_fks(self):
        async with self.client:
            result = await self.client.call_tool(
                'get_table_schema_with_relations',
                {'database_name': TEST_DB_NAME, 'table_name': 'documented'},
            )

        payload = tool_payload(result)
        self.assertEqual(payload['comment'], DOCUMENTED_TABLE_COMMENT)
        self.assertEqual(payload['columns']['parent_id']['foreign_key']['referenced_table'],
                         'parents')
        self.assertEqual(payload['columns']['parent_id']['comment'],
                         'FK -> parents.id; the owning parent record.')

    async def test_tool_descriptions_mention_comments(self):
        """The description is how a model learns comments are available."""
        async with self.client:
            tools = {t.name: t for t in await self.client.list_tools()}

        for name in ('get_table_schema', 'get_table_schema_with_relations'):
            self.assertIn('comment', tools[name].description.lower(), name)

    async def test_view_comment_suppressed_through_tool(self):
        async with self.client:
            result = await self.client.call_tool(
                'get_table_schema',
                {'database_name': TEST_DB_NAME, 'table_name': 'documented_view'},
            )

        self.assertEqual(tool_payload(result)['comment'], '')


if __name__ == "__main__":
    unittest.main(verbosity=2)
