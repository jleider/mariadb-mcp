"""
Unit tests for the schema tools (DL-5840).

Covers the table/column COMMENT metadata returned by `get_table_schema` and
`get_table_schema_with_relations`, the INFORMATION_SCHEMA default normalization,
and previously-untested behaviour of the other MCP tools (validation, error
paths, field mapping, parameter handling).

All tests mock `_execute_query`, so no database is required. The behaviour these
mocks stand in for is verified against a real MariaDB in
test_integration_schema_comments.py.

Run from the repo root:
    python -m unittest src.tests.test_table_schema_comments -v
"""
import unittest
from unittest.mock import AsyncMock, patch

from src.server import MariaDBServer


def _meta_row(column_name='id', column_type='int(11)', is_nullable='NO',
              column_key='', column_default=None, extra='', column_comment='',
              table_type='BASE TABLE', table_comment=''):
    """
    Builds a row shaped like the TABLES/COLUMNS join in _TABLE_METADATA_SQL.

    Note `column_default=None` models SQL NULL. Real INFORMATION_SCHEMA reports
    a column with no default as the *string* 'NULL'; both are normalized to None.
    """
    return {
        'column_name': column_name,
        'column_type': column_type,
        'is_nullable': is_nullable,
        'column_key': column_key,
        'column_default': column_default,
        'extra': extra,
        'column_comment': column_comment,
        'table_type': table_type,
        'table_comment': table_comment,
    }


def _meta_rows(columns, table_comment='', table_type='BASE TABLE'):
    """Stamps a shared table comment/type across per-column row overrides."""
    return [
        _meta_row(table_comment=table_comment, table_type=table_type, **col)
        for col in columns
    ]


class _QueryDispatcher:
    """
    Stands in for `_execute_query`, routing calls by SQL. get_table_schema now
    issues a single metadata query; with_relations adds the foreign-key query.
    """

    def __init__(self, metadata=None, fk_rows=None):
        self.metadata = metadata if metadata is not None else []
        self.fk_rows = fk_rows if fk_rows is not None else []
        self.calls = []

    # Deliberately synchronous: AsyncMock does not treat a class with an async
    # __call__ as a coroutine function, so it would return the coroutine without
    # awaiting it. A sync side_effect's return value is used as the await result.
    def __call__(self, sql, params=None, database=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params, database))

        if 'information_schema.TABLES' in normalized and 'LEFT JOIN' in normalized:
            return self.metadata
        if 'KEY_COLUMN_USAGE' in normalized:
            return self.fk_rows
        raise AssertionError(f"Unexpected query issued by tool: {normalized}")

    @property
    def metadata_calls(self):
        return [c for c in self.calls if 'LEFT JOIN' in c[0]]


class _ServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = MariaDBServer()

    def install(self, dispatcher):
        """Patches `_execute_query` with the given dispatcher for this test."""
        patcher = patch.object(
            self.server, '_execute_query',
            new_callable=AsyncMock, side_effect=dispatcher,
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        return dispatcher


# --------------------------------------------------------------------------
# COLUMN_DEFAULT normalization
# --------------------------------------------------------------------------

class TestNormalizeColumnDefault(unittest.TestCase):
    """
    INFORMATION_SCHEMA.COLUMN_DEFAULT holds a SQL literal, so it must be decoded
    back to the plain value DESCRIBE used to report. The expectations below were
    taken from a real MariaDB 11 (see the probe table in the PR discussion).
    """

    def test_quoted_string_is_unquoted(self):
        self.assertEqual(MariaDBServer._normalize_column_default("'active'"), 'active')

    def test_empty_string_default(self):
        self.assertEqual(MariaDBServer._normalize_column_default("''"), '')

    def test_doubled_single_quote_is_unescaped(self):
        self.assertEqual(MariaDBServer._normalize_column_default("'it''s'"), "it's")

    def test_escaped_backslash_is_unescaped(self):
        self.assertEqual(MariaDBServer._normalize_column_default("'a\\\\b'"), 'a\\b')

    def test_escape_sequences_are_decoded(self):
        self.assertEqual(MariaDBServer._normalize_column_default("'a\\nb'"), 'a\nb')
        self.assertEqual(MariaDBServer._normalize_column_default("'a\\tb'"), 'a\tb')

    def test_numeric_defaults_pass_through(self):
        self.assertEqual(MariaDBServer._normalize_column_default('5'), '5')
        self.assertEqual(MariaDBServer._normalize_column_default('1.50'), '1.50')

    def test_expression_defaults_pass_through(self):
        self.assertEqual(
            MariaDBServer._normalize_column_default('current_timestamp()'),
            'current_timestamp()',
        )

    def test_unquoted_null_means_no_default(self):
        """This is the string 'NULL', which is how "no default" arrives."""
        self.assertIsNone(MariaDBServer._normalize_column_default('NULL'))

    def test_sql_null_means_no_default(self):
        self.assertIsNone(MariaDBServer._normalize_column_default(None))

    def test_quoted_null_is_a_real_string_default(self):
        """Quoting is what distinguishes DEFAULT 'NULL' from no default."""
        self.assertEqual(MariaDBServer._normalize_column_default("'NULL'"), 'NULL')


# --------------------------------------------------------------------------
# Table and column comments (DL-5840)
# --------------------------------------------------------------------------

class TestGetTableSchemaComments(_ServerTestCase):

    async def test_returns_nested_shape_with_table_and_column_comments(self):
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [
                {'column_name': 'id', 'column_key': 'PRI', 'column_comment': 'Surrogate PK'},
                {'column_name': 'email', 'column_type': 'varchar(255)',
                 'column_comment': 'Login address; unique per client'},
            ],
            table_comment='Registered users',
        )))

        result = await self.server.get_table_schema('BondLink', 'Users')

        self.assertEqual(result['table_name'], 'Users')
        self.assertEqual(result['comment'], 'Registered users')
        self.assertEqual(set(result['columns']), {'id', 'email'})
        self.assertEqual(result['columns']['id']['comment'], 'Surrogate PK')
        self.assertEqual(result['columns']['email']['comment'],
                         'Login address; unique per client')

    async def test_uses_a_single_query(self):
        """The whole point of sourcing from INFORMATION_SCHEMA rather than DESCRIBE."""
        dispatcher = self.install(_QueryDispatcher(
            metadata=_meta_rows([{'column_name': 'id'}], table_comment='x')))

        await self.server.get_table_schema('BondLink', 'Users')

        self.assertEqual(len(dispatcher.calls), 1)

    async def test_null_column_comment_becomes_empty_string(self):
        self.install(_QueryDispatcher(metadata=[
            _meta_row(column_name='id', column_comment=None)]))

        result = await self.server.get_table_schema('BondLink', 'Users')

        self.assertEqual(result['columns']['id']['comment'], '')

    async def test_every_column_always_carries_a_comment_key(self):
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [{'column_name': 'a'}, {'column_name': 'b'}, {'column_name': 'c'}])))

        result = await self.server.get_table_schema('BondLink', 'Users')

        for col_name, col in result['columns'].items():
            self.assertIn('comment', col, f"column {col_name} missing 'comment'")
            self.assertEqual(col['comment'], '')

    async def test_null_table_comment_becomes_empty_string(self):
        self.install(_QueryDispatcher(metadata=[
            _meta_row(column_name='id', table_comment=None)]))

        result = await self.server.get_table_schema('BondLink', 'Users')

        self.assertEqual(result['comment'], '')

    async def test_view_placeholder_comment_is_suppressed(self):
        """
        MariaDB reports TABLE_COMMENT as the literal string 'VIEW' for views and
        has no COMMENT clause for CREATE VIEW, so this is a placeholder rather
        than documentation.
        """
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [{'column_name': 'id'}], table_comment='VIEW', table_type='VIEW')))

        result = await self.server.get_table_schema('BondLinkReporting', 'IssuerPortalUsage')

        self.assertEqual(result['comment'], '')

    async def test_view_with_a_real_comment_is_preserved(self):
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [{'column_name': 'id'}],
            table_comment='Rollup of portal logins', table_type='VIEW')))

        result = await self.server.get_table_schema('BondLinkReporting', 'IssuerPortalUsage')

        self.assertEqual(result['comment'], 'Rollup of portal logins')

    async def test_base_table_commented_literally_view_is_preserved(self):
        """The placeholder guard must key on TABLE_TYPE, not the string alone."""
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [{'column_name': 'id'}], table_comment='VIEW', table_type='BASE TABLE')))

        result = await self.server.get_table_schema('BondLink', 'Users')

        self.assertEqual(result['comment'], 'VIEW')

    async def test_column_named_comment_does_not_collide_with_table_comment(self):
        """
        Regression test for the nested return shape: real tables have a column
        named `comment` (e.g. information_schema.STATISTICS), so the table-level
        comment cannot share a namespace with column names.
        """
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [
                {'column_name': 'comment', 'column_type': 'varchar(255)',
                 'column_comment': 'Free-text note from the index'},
                {'column_name': 'id'},
            ],
            table_comment='Index statistics',
        )))

        result = await self.server.get_table_schema('information_schema', 'STATISTICS')

        self.assertEqual(result['comment'], 'Index statistics')
        self.assertEqual(result['columns']['comment']['comment'],
                         'Free-text note from the index')

    async def test_metadata_query_is_scoped_to_the_requested_database(self):
        """Comments must resolve per-database, not from the pool's default DB."""
        dispatcher = self.install(_QueryDispatcher(
            metadata=_meta_rows([{'column_name': 'id'}], table_comment='x')))

        await self.server.get_table_schema('IpreoHoldings', 'Holdings')

        self.assertEqual(len(dispatcher.metadata_calls), 1)
        self.assertEqual(dispatcher.metadata_calls[0][1], ('IpreoHoldings', 'Holdings'))


# --------------------------------------------------------------------------
# Column definition mapping and error paths
# --------------------------------------------------------------------------

class TestGetTableSchemaColumnMapping(_ServerTestCase):

    async def test_maps_information_schema_fields(self):
        self.install(_QueryDispatcher(metadata=[_meta_row(
            column_name='id', column_type='bigint(20)', is_nullable='NO',
            column_key='PRI', column_default=None, extra='auto_increment',
        )]))

        col = (await self.server.get_table_schema('BondLink', 'Users'))['columns']['id']

        self.assertEqual(col['type'], 'bigint(20)')
        self.assertEqual(col['key'], 'PRI')
        self.assertEqual(col['extra'], 'auto_increment')
        self.assertIsNone(col['default'])
        self.assertFalse(col['nullable'])

    async def test_nullable_reflects_is_nullable(self):
        self.install(_QueryDispatcher(metadata=_meta_rows([
            {'column_name': 'a', 'is_nullable': 'YES'},
            {'column_name': 'b', 'is_nullable': 'NO'},
        ])))

        cols = (await self.server.get_table_schema('BondLink', 'Users'))['columns']

        self.assertTrue(cols['a']['nullable'])
        self.assertFalse(cols['b']['nullable'])

    async def test_nullable_handles_missing_value(self):
        self.install(_QueryDispatcher(metadata=[
            _meta_row(column_name='a', is_nullable=None)]))

        cols = (await self.server.get_table_schema('BondLink', 'Users'))['columns']

        self.assertFalse(cols['a']['nullable'])

    async def test_string_default_is_unquoted(self):
        self.install(_QueryDispatcher(metadata=[_meta_row(
            column_name='status', column_type='varchar(20)', column_default="'active'")]))

        cols = (await self.server.get_table_schema('BondLink', 'Users'))['columns']

        self.assertEqual(cols['status']['default'], 'active')

    async def test_string_null_default_becomes_none(self):
        self.install(_QueryDispatcher(metadata=[
            _meta_row(column_name='nickname', column_default='NULL')]))

        cols = (await self.server.get_table_schema('BondLink', 'Users'))['columns']

        self.assertIsNone(cols['nickname']['default'])

    async def test_column_order_is_preserved(self):
        """The query orders by ORDINAL_POSITION; the dict must keep that order."""
        self.install(_QueryDispatcher(metadata=_meta_rows(
            [{'column_name': n} for n in ('id', 'alpha', 'beta', 'zulu')])))

        cols = (await self.server.get_table_schema('BondLink', 'Users'))['columns']

        self.assertEqual(list(cols), ['id', 'alpha', 'beta', 'zulu'])

    async def test_unknown_table_raises_file_not_found(self):
        """No TABLES row at all means the table does not exist."""
        self.install(_QueryDispatcher(metadata=[]))

        with self.assertRaises(FileNotFoundError):
            await self.server.get_table_schema('BondLink', 'NoSuchTable')

    async def test_table_with_no_readable_columns_returns_empty_columns(self):
        """
        The LEFT JOIN yields one row with a NULL column_name when the table
        exists but exposes no columns, so the table comment is still reported.
        """
        self.install(_QueryDispatcher(metadata=[_meta_row(
            column_name=None, table_comment='restricted')]))

        result = await self.server.get_table_schema('BondLink', 'Restricted')

        self.assertEqual(result['columns'], {})
        self.assertEqual(result['comment'], 'restricted')

    async def test_invalid_database_name_raises_value_error(self):
        for bad in ['', 'has space', 'bad-name', 'a;DROP', '1db']:
            with self.subTest(database_name=bad):
                with self.assertRaises(ValueError):
                    await self.server.get_table_schema(bad, 'Users')

    async def test_invalid_table_name_raises_value_error(self):
        for bad in ['', 'has space', 'bad-name', 'a;DROP']:
            with self.subTest(table_name=bad):
                with self.assertRaises(ValueError):
                    await self.server.get_table_schema('BondLink', bad)

    async def test_query_failure_is_wrapped_in_runtime_error(self):
        patcher = patch.object(
            self.server, '_execute_query',
            new_callable=AsyncMock, side_effect=Exception("connection reset"),
        )
        self.addCleanup(patcher.stop)
        patcher.start()

        with self.assertRaises(RuntimeError):
            await self.server.get_table_schema('BondLink', 'Users')


# --------------------------------------------------------------------------
# get_table_schema_with_relations
# --------------------------------------------------------------------------

class TestGetTableSchemaWithRelations(_ServerTestCase):

    def _dispatcher(self):
        return _QueryDispatcher(
            metadata=_meta_rows(
                [
                    {'column_name': 'id', 'column_key': 'PRI',
                     'column_comment': 'Surrogate PK'},
                    {'column_name': 'clientId', 'column_key': 'MUL',
                     'column_comment': 'FK -> Clients.id'},
                ],
                table_comment='Issuer deals',
            ),
            fk_rows=[{
                'column_name': 'clientId',
                'constraint_name': 'fk_deals_client',
                'referenced_table': 'Clients',
                'referenced_column': 'id',
                'on_update': 'CASCADE',
                'on_delete': 'RESTRICT',
            }],
        )

    async def test_includes_table_and_column_comments(self):
        self.install(self._dispatcher())

        result = await self.server.get_table_schema_with_relations('BondLink', 'Deals')

        self.assertEqual(result['table_name'], 'Deals')
        self.assertEqual(result['comment'], 'Issuer deals')
        self.assertEqual(result['columns']['id']['comment'], 'Surrogate PK')
        self.assertEqual(result['columns']['clientId']['comment'], 'FK -> Clients.id')

    async def test_uses_two_queries(self):
        """One metadata query plus one foreign-key query."""
        dispatcher = self.install(self._dispatcher())

        await self.server.get_table_schema_with_relations('BondLink', 'Deals')

        self.assertEqual(len(dispatcher.calls), 2)

    async def test_foreign_key_metadata_is_attached(self):
        self.install(self._dispatcher())

        result = await self.server.get_table_schema_with_relations('BondLink', 'Deals')
        fk = result['columns']['clientId']['foreign_key']

        self.assertEqual(fk['constraint_name'], 'fk_deals_client')
        self.assertEqual(fk['referenced_table'], 'Clients')
        self.assertEqual(fk['referenced_column'], 'id')
        self.assertEqual(fk['on_update'], 'CASCADE')
        self.assertEqual(fk['on_delete'], 'RESTRICT')

    async def test_non_foreign_key_columns_have_null_foreign_key(self):
        self.install(self._dispatcher())

        result = await self.server.get_table_schema_with_relations('BondLink', 'Deals')

        self.assertIsNone(result['columns']['id']['foreign_key'])

    async def test_comments_survive_alongside_foreign_keys(self):
        """Guards against the FK merge step dropping the comment field."""
        self.install(self._dispatcher())

        result = await self.server.get_table_schema_with_relations('BondLink', 'Deals')

        for col_name, col in result['columns'].items():
            self.assertIn('comment', col, f"column {col_name} lost its comment")
            self.assertIn('foreign_key', col)
            self.assertIn('type', col)

    async def test_foreign_key_for_unknown_column_is_ignored(self):
        dispatcher = self._dispatcher()
        dispatcher.fk_rows = [{
            'column_name': 'notAColumn',
            'constraint_name': 'fk_x', 'referenced_table': 'T',
            'referenced_column': 'id', 'on_update': 'CASCADE', 'on_delete': 'CASCADE',
        }]
        self.install(dispatcher)

        result = await self.server.get_table_schema_with_relations('BondLink', 'Deals')

        self.assertNotIn('notAColumn', result['columns'])

    async def test_invalid_names_raise_value_error(self):
        with self.assertRaises(ValueError):
            await self.server.get_table_schema_with_relations('bad-db', 'Deals')
        with self.assertRaises(ValueError):
            await self.server.get_table_schema_with_relations('BondLink', 'bad-table')

    async def test_failure_is_wrapped_in_runtime_error(self):
        self.install(_QueryDispatcher(metadata=[]))

        with self.assertRaises(RuntimeError):
            await self.server.get_table_schema_with_relations('BondLink', 'NoSuchTable')


# --------------------------------------------------------------------------
# Remaining tools that previously had no unit coverage
# --------------------------------------------------------------------------

class TestOtherToolCoverage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = MariaDBServer()
        self.patcher = patch.object(self.server, '_execute_query', new_callable=AsyncMock)
        self.mock_query = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_list_tables_returns_names(self):
        self.mock_query.return_value = [
            {'Tables_in_BondLink': 'Users'},
            {'Tables_in_BondLink': 'Deals'},
        ]

        self.assertEqual(
            await self.server.list_tables('BondLink'),
            ['Users', 'Deals'],
        )

    async def test_list_tables_handles_empty_database(self):
        self.mock_query.return_value = []

        self.assertEqual(await self.server.list_tables('BondLink'), [])

    async def test_list_tables_rejects_invalid_database_name(self):
        for bad in ['', 'has space', 'bad-name', 'a;DROP TABLE x']:
            with self.subTest(database_name=bad):
                with self.assertRaises(ValueError):
                    await self.server.list_tables(bad)

    async def test_execute_sql_rejects_invalid_database_name(self):
        with self.assertRaises(ValueError):
            await self.server.execute_sql('SELECT 1', 'bad-name')

    async def test_execute_sql_converts_parameters_to_tuple(self):
        self.mock_query.return_value = []

        await self.server.execute_sql('SELECT %s', 'BondLink', ['a', 1])

        self.assertEqual(self.mock_query.call_args.kwargs['params'], ('a', 1))

    async def test_execute_sql_passes_none_when_no_parameters(self):
        self.mock_query.return_value = []

        await self.server.execute_sql('SELECT 1', 'BondLink')

        self.assertIsNone(self.mock_query.call_args.kwargs['params'])

    async def test_execute_sql_returns_rows(self):
        self.mock_query.return_value = [{'one': 1}]

        self.assertEqual(await self.server.execute_sql('SELECT 1', 'BondLink'), [{'one': 1}])

    async def test_create_database_rejects_invalid_name(self):
        for bad in ['', 'has space', 'bad-name', 'a;DROP DATABASE x']:
            with self.subTest(database_name=bad):
                with self.assertRaises(ValueError):
                    await self.server.create_database(bad)

    async def test_create_database_reports_existing_database(self):
        with patch.object(self.server, '_database_exists',
                          new_callable=AsyncMock, return_value=True):
            result = await self.server.create_database('BondLink')

        self.assertEqual(result['status'], 'exists')
        self.assertEqual(result['database_name'], 'BondLink')
        self.mock_query.assert_not_called()

    async def test_create_database_creates_missing_database(self):
        with patch.object(self.server, '_database_exists',
                          new_callable=AsyncMock, return_value=False):
            result = await self.server.create_database('NewDb')

        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['database_name'], 'NewDb')
        self.mock_query.assert_called_once()

    async def test_create_database_wraps_failure_in_runtime_error(self):
        with patch.object(self.server, '_database_exists',
                          new_callable=AsyncMock, return_value=False):
            self.mock_query.side_effect = Exception("access denied")
            with self.assertRaises(RuntimeError):
                await self.server.create_database('NewDb')

    async def test_database_exists_rejects_invalid_identifier_without_querying(self):
        self.assertFalse(await self.server._database_exists('bad-name'))
        self.mock_query.assert_not_called()

    async def test_database_exists_true_when_row_returned(self):
        self.mock_query.return_value = [{'SCHEMA_NAME': 'BondLink'}]

        self.assertTrue(await self.server._database_exists('BondLink'))

    async def test_database_exists_false_when_no_row(self):
        self.mock_query.return_value = []

        self.assertFalse(await self.server._database_exists('Nope'))

    async def test_database_exists_false_on_query_error(self):
        self.mock_query.side_effect = Exception("boom")

        self.assertFalse(await self.server._database_exists('BondLink'))


# --------------------------------------------------------------------------
# _execute_query parameter passing
# --------------------------------------------------------------------------

class _FakeCursor:
    """Records execute() calls; stands in for an asyncmy DictCursor."""

    def __init__(self):
        self.calls = []

    async def execute(self, sql, args=None):
        self.calls.append((sql, args))

    async def fetchone(self):
        return {'DATABASE()': 'testdb'}

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor=None):
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, connection):
        self._connection = connection

    def acquire(self):
        return self._connection


class TestExecuteQueryParameterPassing(unittest.IsolatedAsyncioTestCase):
    """
    The driver applies %-formatting only when args is not None, so "no
    parameters" must reach it as None. Passing an empty tuple instead formats a
    query with nothing to substitute, which breaks every literal '%' — including
    LIKE '%foo%'. Guards that regression without needing a database.
    """

    async def asyncSetUp(self):
        self.server = MariaDBServer()
        self.server.is_read_only = False
        self.cursor = _FakeCursor()
        self.server.pool = _FakePool(_FakeConnection(self.cursor))

    async def test_no_parameters_reaches_the_driver_as_none(self):
        await self.server._execute_query("SELECT '100%' AS pct")

        sql, args = self.cursor.calls[-1]
        self.assertEqual(sql, "SELECT '100%' AS pct")
        self.assertIsNone(args, "empty tuple would re-enable %-formatting")

    async def test_supplied_parameters_are_forwarded_unchanged(self):
        await self.server._execute_query('SELECT %s', params=('a',))

        self.assertEqual(self.cursor.calls[-1], ('SELECT %s', ('a',)))

    async def test_empty_tuple_is_forwarded_as_given(self):
        """An explicitly empty tuple is the caller's choice, not None."""
        await self.server._execute_query('SELECT 1', params=())

        self.assertEqual(self.cursor.calls[-1], ('SELECT 1', ()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
