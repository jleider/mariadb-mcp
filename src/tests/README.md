# MariaDB MCP Server Tests

## Running the tests

From the repository root:

```bash
# Unit tests only (no database needed) — integration tests skip automatically
python -m unittest discover -s src/tests -t .

# Everything, including integration tests
docker compose -f docker-compose.test.yml up -d --wait
python -m unittest discover -s src/tests -t .
docker compose -f docker-compose.test.yml down
```

Run the suite from the repository root, not from `src/`. `src/tests/__init__.py`
puts `src/` on `sys.path` so that `server.py`'s flat imports (`from config
import ...`) resolve while the tests import `src.server`.

## Layout

| File | Needs a database | What it covers |
| --- | --- | --- |
| `test_table_schema_comments.py` | No (mocked) | Schema-tool logic in isolation: comment handling, validation, error paths, field mapping |
| `test_list_databases_unittest.py` | No (mocked) | `list_databases` |
| `test_integration_schema_comments.py` | Yes | Table/column comments against a real schema (DL-5840) |
| `test_mcp_server.py` | Yes | All six tools through a fastmcp client, plus read-only enforcement |
| `test_mariadb_mcp_tools.py` | Yes | Tool behaviour called directly: SQL execution, joins, aggregation, parameter edge cases |
| `smoke_test.py` | Yes | Standalone sanity check; run directly, not collected by `unittest` |
| `support.py` | — | Shared integration fixtures and the skip-if-unavailable probe |
| `fixtures/01-test-schema.sql` | — | Schema loaded into the test container |

## The test database

`docker-compose.test.yml` runs MariaDB 11 on host port **3307**, so it never
collides with a local server on 3306. Storage is `tmpfs`, so every `up` starts
from an empty data directory and re-applies `fixtures/01-test-schema.sql`. The
healthcheck waits for the fixture tables, so `--wait` does not return early.

Connection details default to that container and can be overridden with
`TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_USER`, `TEST_DB_PASSWORD`,
`TEST_DB_NAME` and `TEST_DB_OTHER_NAME`. Note that the integration tests do
**not** read `.env`; they patch the settings that `server.py` binds at import
time, so they never touch a real database by accident.

When the container is not reachable, integration tests skip with a message
telling you how to start it, rather than failing.

## What the fixture schema is for

Each object exists to pin down a specific behaviour:

-   `documented` — every column commented, plus one deliberately uncommented column, a `DEFAULT`, and a foreign key
-   `undocumented` — no comments anywhere; everything must come back as `''`
-   `column_defaults` — every shape of column default, because `INFORMATION_SCHEMA.COLUMN_DEFAULT` returns SQL literals (`'active'`) and the string `NULL` for "no default", all of which get decoded back to plain values
-   `with_comment_column` — has a column literally named `comment`, which is why `get_table_schema` nests columns under `columns` instead of returning them flat
-   `documented_view` — MariaDB reports `TABLE_COMMENT` as the literal string `'VIEW'` for views, which must be suppressed rather than surfaced as documentation
-   `parents` — foreign-key target, and a table with no FKs of its own
-   `mcp_test_other.documented` — same table and column names as `mcp_test.documented` but different comments, so a lookup that forgot `TABLE_SCHEMA` returns visibly wrong text

## Known behaviour pinned by tests

`execute_sql` passes `params or ()` to the driver. An empty tuple is not
`None`, so the driver applies `%`-formatting even when no parameters were
supplied, and a literal `%` must be doubled: `SHOW VARIABLES LIKE 'version%%'`
works, `'version%'` raises. `test_step_12*` pins both halves of this. Changing
it would silently alter what `%%` means for callers already escaping it.
