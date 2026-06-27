# MariaDB Ops Server MCP Tool Tests

This directory contains artifacts related to testing the MariaDB Ops Server MCP (Model Context Protocol) tools.

## Purpose

The primary goal of these tests was to verify the basic functionality and robustness of the read-only operations provided by the MariaDB MCP server tools:

-   `mcp0_list_databases`
-   `mcp0_list_tables`
-   `mcp0_get_table_schema`
-   `mcp0_execute_sql`

## Execution Method

Tests were performed **manually** via the AI Assistant interface. The interface invoked the MCP tools directly based on user requests, and the results (or errors) were observed in the assistant's responses.

From there, tests were converted into code in test_mariadb_mcp_tools.py`. We use the python `unittest` framework to structure the tests. Note that the environment variables are still used in the unit tests, and a live mariadb server is required to run the tests currently.

## Test Cases

The specific test cases executed are documented in the `test_mariadb_mcp_tools.py` script within this directory. This script serves as a record of the manual tests performed and includes:

1.  **Basic Functionality Tests:** Verifying core operations like listing databases/tables, getting schema, and executing simple SELECTs.
2.  **Complex/Edge Case Tests:** Checking behavior with:
    *   Non-existent databases/tables
    *   Complex SQL (JOINs, Aggregations)
    *   Parameterized queries with edge-case values (empty strings)
    *   Parameter count mismatches
    *   `SHOW` commands (including necessary wildcard escaping)

## Summary of Results

All tests executed as expected. The tools successfully performed the requested read-only operations and provided appropriate error messages for invalid inputs or non-existent objects. The `mcp0_execute_sql` tool required correct escaping (`%%`) for literal `%` signs in `LIKE` clauses when used with `SHOW` commands.

## `test_mariadb_mcp_tools.py`

This Python script outlines the tests performed. It is **not** an automated test suite but rather a structured documentation of the manual steps and observed outcomes. It cannot be run independently to interact with the MCP tools.

## `test_multi_statements.py`

This is an **automated test script** that verifies the MULTI_STATEMENTS security fix. It can be run independently and tests:

### Test Coverage

1. **Flag Tests** (always run, no database required):
   - Verifies regular `asyncmy.Connection` has `MULTI_STATEMENTS` enabled
   - Verifies `SafeConnection` has `MULTI_STATEMENTS` disabled

2. **Functionality Tests** (requires database):
   - SafeConnection establishes connections successfully
   - Single SQL statements execute correctly
   - Parameterized queries work properly
   - Sequential single statements work
   - Multi-statement queries are properly blocked
   - Async operations work correctly

3. **Regular Pool Tests** (requires database, for comparison):
   - Regular asyncmy.Pool has `MULTI_STATEMENTS` enabled
   - Multi-statement queries work correctly in regular pool
   - Demonstrates the security risk we're fixing

4. **Safe Pool Tests** (requires database):
   - SafePool creates connections with `MULTI_STATEMENTS` disabled
   - Pool connections execute queries correctly
   - Concurrent connections work properly
   - Multi-statement queries are blocked in SafePool

### Running the Test

The test automatically:
1. **Loads from `.env` file** if it exists (create from `.env.example`)
2. **Prompts for missing credentials** interactively if needed
3. **Falls back to defaults** if no input provided

**Option 1: Using .env file (recommended)**:
```bash
# 1. Copy the example file
cp .env.example .env

# 2. Edit .env and set your credentials
# 3. Run the test - it will automatically load from .env
uv run python src/tests/test_multi_statements.py
```

**Option 2: Interactive prompts**:
```bash
uv run python src/tests/test_multi_statements.py
# Will prompt for password if not in .env or environment
```

**Option 3: Inline environment variable**:
```bash
DB_PASSWORD=your_password uv run python src/tests/test_multi_statements.py
```

**Option 4: Export environment variables**:
```bash
export DB_HOST=localhost
export DB_PORT=3306
export DB_USER=root
export DB_PASSWORD=your_password
export DB_NAME=test

uv run python src/tests/test_multi_statements.py
```

### Priority Order

The test loads credentials in this order (later sources override earlier ones):
1. Default values (localhost, root, test)
2. `.env` file (if exists)
3. Environment variables
4. Interactive prompts (only if password still not set)

See `MULTI_STATEMENTS_FIX.md` in the root directory for more details.
