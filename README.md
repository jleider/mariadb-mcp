# MCP MariaDB Server

The MCP MariaDB Server provides a Model Context Protocol (MCP) interface for managing and querying MariaDB databases, supporting standard SQL operations. Designed for use with AI assistants, it enables seamless integration of AI-driven data workflows with relational databases.

---

## Table of Contents

- [Overview](#overview)
- [Core Components](#core-components)
- [Available Tools](#available-tools)
- [Configuration & Environment Variables](#configuration--environment-variables)
- [Installation & Setup](#installation--setup)
- [Usage Examples](#usage-examples)
- [Integration - Claude desktop/Cursor/Windsurf](#integration---claude-desktopcursorwindsurf)
- [Logging](#logging)
- [Testing](#testing)
---

## Overview

The MCP MariaDB Server exposes a set of tools for interacting with MariaDB databases via a standardized protocol. It supports:
- Listing databases and tables
- Retrieving table schemas
- Executing safe, read-only SQL queries

---

## Core Components

- **server.py**: Main MCP server logic and tool definitions.
- **config.py**: Loads configuration from environment and `.env` files.
- **tests/**: Manual and automated test documentation and scripts.

---

## Available Tools

### Standard Database Tools

- **list_databases**
  - Lists all accessible databases.
  - Parameters: _None_

- **list_tables**
  - Lists all tables in a specified database.
  - Parameters: `database_name` (string, required)

- **get_table_schema**
  - Retrieves schema for a table (columns, types, keys, etc.), including the table-level `COMMENT` and each column's `COMMENT`.
  - Parameters: `database_name` (string, required), `table_name` (string, required)
  - Returns: `{"table_name": ..., "comment": ..., "columns": {"<col>": {"type", "nullable", "key", "default", "extra", "comment"}}}`. Columns are nested under `columns` because a table may itself have a column named `comment`. Tables and columns without a comment return an empty string, and columns with no default return `null`.
  - Everything is read from `INFORMATION_SCHEMA` in a single query. Column order follows `ORDINAL_POSITION`.

- **get_table_schema_with_relations**
  - Retrieves schema with foreign key relations for a table, including the table-level `COMMENT` and each column's `COMMENT`.
  - Parameters: `database_name` (string, required), `table_name` (string, required)
  - Returns: the same shape as `get_table_schema`, with an added `foreign_key` field per column (`null` when the column is not a foreign key). Uses two queries.

- **execute_sql**
  - Executes a read-only SQL query (`SELECT`, `SHOW`, `DESCRIBE`).
  - Parameters: `sql_query` (string, required), `database_name` (string, optional), `parameters` (list, optional)
  - _Note: Enforces read-only mode if `MCP_READ_ONLY` is enabled._
  
- **create_database**
  - Creates a new database if it doesn't exist.
  - Parameters: `database_name` (string, required)  

---

## Configuration & Environment Variables

All configuration is via environment variables (typically set in a `.env` file):

| Variable               | Description                                            | Required | Default      |
|------------------------|--------------------------------------------------------|----------|--------------|
| `DB_HOST`              | MariaDB host address                                   | Yes      | `localhost`  |
| `DB_PORT`              | MariaDB port                                           | No       | `3306`       |
| `DB_USER`              | MariaDB username                                       | Yes      |              |
| `DB_PASSWORD`          | MariaDB password                                       | Yes      |              |
| `DB_NAME`              | Default database (optional; can be set per query)      | No       |              |
| `MCP_READ_ONLY`        | Enforce read-only SQL mode (`true`/`false`)            | No       | `true`       |
| `MCP_MAX_POOL_SIZE`    | Max DB connection pool size                            | No       | `10`         |

#### Example `.env` file

```dotenv
DB_HOST=localhost
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_PORT=3306
DB_NAME=your_default_database
MCP_READ_ONLY=true
MCP_MAX_POOL_SIZE=10
```

---

## Installation & Setup

### Requirements

- **Python 3.11** (see `.python-version`)
- **uv** (dependency manager; [install instructions](https://github.com/astral-sh/uv))
- MariaDB server (local or remote)

### Steps

1. **Clone the repository**
2. **Install `uv`** (if not already):
   ```bash
   pip install uv
   ```
3. **Install dependencies**
   ```bash
   uv pip compile pyproject.toml -o uv.lock
   ```
   ```bash
   uv pip sync uv.lock
   ```
4. **Create `.env`** in the project root (see [Configuration](#configuration--environment-variables))
5. **Run the server**
   
   **Standard Input/Output (default):**
   ```bash
   python server.py
   ```
   
   **SSE Transport:**
   ```bash
   python server.py --transport sse --host 127.0.0.1 --port 9001
   ```
   
   **HTTP Transport (streamable HTTP):**
   ```bash
   python server.py --transport http --host 127.0.0.1 --port 9001 --path /mcp
   ```

---

## Usage Examples

### Standard SQL Query

```python
{
  "tool": "execute_sql",
  "parameters": {
    "database_name": "test_db",
    "sql_query": "SELECT * FROM users WHERE id = %s",
    "parameters": [123]
  }
}
```

---

## Integration - Claude desktop/Cursor/Windsurf/VSCode

### Option 1: Direct Command (stdio)
```json
{
  "mcpServers": {
    "MariaDB_Server": {
      "command": "uv",
      "args": [
        "--directory",
        "path/to/mariadb-mcp-server/",
        "run",
        "server.py"
        ],
        "envFile": "path/to/mariadb-mcp-server/.env"      
    }
  }
}
```

### Option 2: SSE Transport
```json
{
  "servers": {
    "mariadb-mcp-server": {
      "url": "http://{host}:9001/sse",
      "type": "sse"
    }
  }
}
```

### Option 3: HTTP Transport
```json
{
  "servers": {
    "mariadb-mcp-server": {
      "url": "http://{host}:9001/mcp",
      "type": "streamable-http"
    }
  }
}
```

---

## Logging

- Logs are written to `logs/mcp_server.log` by default.
- Log messages include tool calls, configuration issues, and client requests.
- Log level and output can be adjusted in the code (see `config.py` and logger setup).

---

## Testing

Tests live in `src/tests/`. Run them from the repository root.

Unit tests are fully mocked and need no database. Integration tests run against a disposable MariaDB container and skip with an explanatory message when it is not running, so the default command stays green without Docker:

```bash
# Unit tests only; integration tests skip
python -m unittest discover -s src/tests -t .

# Everything, including integration tests
docker compose -f docker-compose.test.yml up -d --wait
python -m unittest discover -s src/tests -t .
docker compose -f docker-compose.test.yml down
```

The test container listens on host port **3307** so it does not collide with a local MariaDB on 3306, and loads the fixture schema in `src/tests/fixtures/01-test-schema.sql`. The integration tests do not read `.env`, so they cannot touch a real database by accident. See `src/tests/README.md` for the fixture layout and the connection overrides.
