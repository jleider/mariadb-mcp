# server.py

# Import configuration settings
from config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, DB_CHARSET,
    DB_SSL, DB_SSL_CA, DB_SSL_CERT, DB_SSL_KEY, DB_SSL_VERIFY_CERT, DB_SSL_VERIFY_IDENTITY,
    MCP_READ_ONLY, MCP_MAX_POOL_SIZE,
    ALLOWED_ORIGINS, ALLOWED_HOSTS,
    logger
)

import asyncio
import argparse
import re
from typing import List, Dict, Any, Optional
from functools import partial
import os
import ssl

import asyncmy
import anyio 
from fastmcp import FastMCP, Context

# Import custom connection pool that disables MULTI_STATEMENTS
from custom_connection import create_safe_pool

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from asyncmy.errors import Error as AsyncMyError

# --- MariaDB MCP Server Class ---
class MariaDBServer:
    """
    MCP Server exposing tools to interact with a MariaDB database.
    Manages the database connection pool.
    """
    def __init__(self, server_name="MariaDB_Server", autocommit=True):
        self.mcp = FastMCP(server_name)
        self.pool: Optional[asyncmy.Pool] = None
        self.autocommit = not MCP_READ_ONLY
        self.is_read_only = MCP_READ_ONLY
        logger.info(f"Initializing {server_name}...")
        if self.is_read_only:
            logger.warning("Server running in READ-ONLY mode. Write operations are disabled.")

    async def _warn_if_file_privilege_enabled(self) -> None:
        if self.pool is None:
            return

        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT CURRENT_USER()")
                    current_user_row = await cursor.fetchone()
                    if not current_user_row:
                        return

                    if isinstance(current_user_row, dict):
                        current_user = next(iter(current_user_row.values()))
                    else:
                        current_user = current_user_row[0]

                    if not current_user:
                        return

                    await cursor.execute(f"SHOW GRANTS FOR {current_user}")
                    grant_rows = await cursor.fetchall()

                    grants: List[str] = []
                    for row in grant_rows or []:
                        if isinstance(row, dict):
                            grants.append(str(next(iter(row.values()))))
                        else:
                            grants.append(str(row[0]))

                    has_file_priv = any(
                        re.search(r"\bFILE\b", grant, flags=re.IGNORECASE) and "ON *.*" in grant.upper()
                        for grant in grants
                    )

                    if has_file_priv:
                        logger.error(
                            "Connected database user has the global FILE privilege. "
                            "This means the server is NOT running in a fully read-only posture, because MariaDB/MySQL allow "
                            "filesystem read/write via SQL (e.g. SELECT ... INTO OUTFILE, LOAD DATA INFILE, LOAD_FILE()). "
                            "This cannot be fixed client-side; revoke FILE for the database user you are connecting as."
                        )
        except Exception as e:
            logger.debug(f"Unable to determine whether FILE privilege is enabled: {e}")

    async def initialize_pool(self):
        """Initializes the asyncmy connection pool within the running event loop."""
        if not DB_USER:
            logger.error("Cannot initialize pool: DB_USER is empty or missing")
            raise ConnectionError("Missing DB_USER for pool initialization.")
        if DB_PASSWORD is None:
            logger.error("Cannot initialize pool: DB_PASSWORD is missing")
            raise ConnectionError("Missing DB_PASSWORD for pool initialization.")

        if self.pool is not None:
            logger.info("Connection pool already initialized.")
            return

        try:
            ssl_context = None
            if DB_SSL:
                ssl_context = ssl.create_default_context()
                if DB_SSL_CA:
                    ca_path = os.path.expanduser(DB_SSL_CA)
                    if os.path.exists(ca_path):
                        ssl_context.load_verify_locations(cafile=ca_path)
                        logger.info(f"Loaded SSL CA certificate: {ca_path}")
                    else:
                        logger.warning(f"SSL CA certificate file not found: {ca_path}")

                if DB_SSL_CERT and DB_SSL_KEY:
                    cert_path = os.path.expanduser(DB_SSL_CERT)
                    key_path = os.path.expanduser(DB_SSL_KEY)
                    if os.path.exists(cert_path) and os.path.exists(key_path):
                        ssl_context.load_cert_chain(cert_path, key_path)
                        logger.info(f"Loaded SSL client certificate: {cert_path}")
                    else:
                        logger.warning(f"SSL client certificate files not found: cert={cert_path}, key={key_path}")

                if not DB_SSL_VERIFY_CERT:
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                    logger.info("SSL certificate verification disabled")
                elif not DB_SSL_VERIFY_IDENTITY:
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_REQUIRED
                    logger.info("SSL hostname verification disabled, certificate verification enabled")
                else:
                    logger.info("Full SSL verification enabled")

                logger.info("SSL enabled for database connection")
            else:
                logger.info("SSL disabled for database connection")

            pool_params = {
                "host": DB_HOST,
                "port": DB_PORT,
                "user": DB_USER,
                "password": DB_PASSWORD,
                "db": DB_NAME,
                "minsize": 1,
                "maxsize": MCP_MAX_POOL_SIZE,
                "autocommit": self.autocommit,
                "pool_recycle": 3600
            }
            if DB_SSL and ssl_context is not None:
                pool_params["ssl"] = ssl_context

            if DB_CHARSET:
                pool_params["charset"] = DB_CHARSET
                logger.info(f"Creating connection pool for {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} (max size: {MCP_MAX_POOL_SIZE}, charset: {DB_CHARSET})")
            else:
                logger.info(f"Creating connection pool for {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME} (max size: {MCP_MAX_POOL_SIZE})")
            
            self.pool = await create_safe_pool(**pool_params)
            logger.info("Connection pool initialized successfully.")
            if self.is_read_only:
                await self._warn_if_file_privilege_enabled()
        except AsyncMyError as e:
            logger.error(f"Failed to initialize database connection pool: {e}", exc_info=True)
            self.pool = None
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred during pool initialization: {e}", exc_info=True)
            self.pool = None
            raise

    async def close_pool(self):
        """Closes the connection pool gracefully."""
        if self.pool:
            logger.info("Closing database connection pool...")
            try:
                self.pool.close()
                await self.pool.wait_closed()
                logger.info("Database connection pool closed.")
            except Exception as e:
                logger.error(f"Error closing connection pool: {e}", exc_info=True)
            finally:
                self.pool = None

    async def _execute_query(self, sql: str, params: Optional[tuple] = None, database: Optional[str] = None) -> List[Dict[str, Any]]:
        """Helper function to execute SELECT queries using the pool."""
        if self.pool is None:
            logger.error("Connection pool is not initialized.")
            raise RuntimeError("Database connection pool not available.")

        allowed_prefixes = ('SELECT', 'SHOW', 'DESC', 'DESCRIBE', 'USE')
        
        # Strip SQL comments from query
        # Remove single-line comments (-- comment)
        sql_no_comments = re.sub(r'--.*?$', '', sql, flags=re.MULTILINE)
        # Remove multi-line comments (/* comment */)
        sql_no_comments = re.sub(r'/\*.*?\*/', '', sql_no_comments, flags=re.DOTALL)
        sql_no_comments = sql_no_comments.strip()
        
        query_upper = sql_no_comments.upper()
        is_allowed_read_query = any(query_upper.startswith(prefix) for prefix in allowed_prefixes)

        if self.is_read_only and not is_allowed_read_query:
             logger.warning(f"Blocked potentially non-read-only query in read-only mode: {sql[:100]}...")
             raise PermissionError("Operation forbidden: Server is in read-only mode.")
        if self.is_read_only:
            # Remove string literals to avoid matching patterns inside strings
            # Handle both single and double quoted strings
            sql_no_strings = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql_no_comments)
            sql_no_strings = re.sub(r'"(?:[^"\\]|\\.)*"', '""', sql_no_strings)
            sql_no_strings_upper = sql_no_strings.upper()
            
            # Check for LOAD_FILE() function (case-insensitive, outside strings)
            if re.search(r'\bLOAD_FILE\s*\(', sql_no_strings_upper):
                logger.warning(f"Blocked query containing LOAD_FILE(): {sql[:100]}...")
                raise PermissionError("Operation forbidden: LOAD_FILE() is not allowed for security reasons.")
            
            # Check for SELECT ... INTO OUTFILE/DUMPFILE (case-insensitive, outside strings)
            if re.search(r'\bINTO\s+(OUTFILE|DUMPFILE)\b', sql_no_strings_upper):
                logger.warning(f"Blocked query containing SELECT INTO OUTFILE or DUMPFILE: {sql[:100]}...")
                raise PermissionError("Operation forbidden: SELECT INTO OUTFILE and SELECT INTO DUMPFILE are not allowed for security reasons.")

        logger.info(f"Executing query (DB: {database or DB_NAME}): {sql[:100]}...")
        if params:
            logger.debug(f"Parameters: {params}")

        conn = None
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor(cursor=asyncmy.cursors.DictCursor) as cursor:
                    current_db_query = "SELECT DATABASE()"
                    await cursor.execute(current_db_query)
                    current_db_result = await cursor.fetchone()
                    current_db_name = current_db_result.get('DATABASE()') if current_db_result else None
                    pool_db_name = DB_NAME
                    actual_current_db = current_db_name or pool_db_name

                    if database and database != actual_current_db:
                        logger.info(f"Switching database context from '{actual_current_db}' to '{database}'")
                        await cursor.execute(f"USE `{database}`")

                    if params is None:
                        await cursor.execute(sql)
                    else:
                        await cursor.execute(sql, params)
                    results = await cursor.fetchall()
                    logger.info(f"Query executed successfully, {len(results)} rows returned.")
                    return results if results else []
        except AsyncMyError as e:
            conn_state = f"Connection: {'acquired' if conn else 'not acquired'}"
            logger.error(f"Database error executing query ({conn_state}): {e}", exc_info=True)
            # Check for specific connection-related errors if possible
            raise RuntimeError(f"Database error: {e}") from e
        except PermissionError as e:
             logger.warning(f"Permission denied: {e}")
             raise e
        except Exception as e:
            # Catch potential loop closed errors here too, although ideally fixed by structure change
            if isinstance(e, RuntimeError) and 'Event loop is closed' in str(e):
                 logger.critical("Detected closed event loop during query execution!", exc_info=True)
                 # This indicates a fundamental problem with loop management still exists
                 raise RuntimeError("Event loop closed unexpectedly during query.") from e
            conn_state = f"Connection: {'acquired' if conn else 'not acquired'}"
            logger.error(f"Unexpected error during query execution ({conn_state}): {e}", exc_info=True)
            raise RuntimeError(f"An unexpected error occurred: {e}") from e
            
    async def _database_exists(self, database_name: str) -> bool:
        """Checks if a database exists."""
        if not database_name or not database_name.isidentifier():
            logger.warning(f"_database_exists called with invalid database_name: {database_name}")
            return False 

        sql = "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s"
        try:
            results = await self._execute_query(sql, params=(database_name,), database='information_schema')
            return len(results) > 0
        except Exception as e:
            logger.error(f"Error checking if database '{database_name}' exists: {e}", exc_info=True)
            return False
        
    # --- MCP Tool Definitions ---

    async def list_databases(self) -> List[str]:
        """Lists all accessible databases on the connected MariaDB server."""
        logger.info("TOOL START: list_databases called.")
        sql = "SHOW DATABASES"
        try:
            results = await self._execute_query(sql)
            db_list = [row['Database'] for row in results if 'Database' in row]
            logger.info(f"TOOL END: list_databases completed. Databases found: {len(db_list)}.")
            return db_list
        except Exception as e:
            logger.error(f"TOOL ERROR: list_databases failed: {e}", exc_info=True)
            raise

    async def list_tables(self, database_name: str) -> List[str]:
        """Lists all tables within the specified database."""
        logger.info(f"TOOL START: list_tables called. database_name={database_name}")
        if not database_name or not database_name.isidentifier():
            logger.warning(f"TOOL WARNING: list_tables called with invalid database_name: {database_name}")
            raise ValueError(f"Invalid database name provided: {database_name}")
        sql = "SHOW TABLES"
        try:
            results = await self._execute_query(sql, database=database_name)
            table_list = [list(row.values())[0] for row in results if row]
            logger.info(f"TOOL END: list_tables completed. Tables found: {len(table_list)}.")
            return table_list
        except Exception as e:
            logger.error(f"TOOL ERROR: list_tables failed for database_name={database_name}: {e}", exc_info=True)
            raise

    async def get_table_schema(self, database_name: str, table_name: str) -> Dict[str, Any]:
        """
        Retrieves the schema (column names, types, nullability, keys, default values)
        for a specific table in a database.
        """
        logger.info(f"TOOL START: get_table_schema called. database_name={database_name}, table_name={table_name}")
        if not database_name or not database_name.isidentifier():
            logger.warning(f"TOOL WARNING: get_table_schema called with invalid database_name: {database_name}")
            raise ValueError(f"Invalid database name provided: {database_name}")
        if not table_name or not table_name.isidentifier():
            logger.warning(f"TOOL WARNING: get_table_schema called with invalid table_name: {table_name}")
            raise ValueError(f"Invalid table name provided: {table_name}")

        sql = f"DESCRIBE `{database_name}`.`{table_name}`"
        try:
            schema_results = await self._execute_query(sql)
            schema_info = {}
            if not schema_results:
                exists_sql = "SELECT COUNT(*) as count FROM information_schema.tables WHERE table_schema = %s AND table_name = %s"
                exists_result = await self._execute_query(exists_sql, params=(database_name, table_name))
                if not exists_result or exists_result[0]['count'] == 0:
                    logger.warning(f"TOOL WARNING: Table '{database_name}'.'{table_name}' not found or inaccessible.")
                    raise FileNotFoundError(f"Table '{database_name}'.'{table_name}' not found or inaccessible.")
                else:
                    logger.warning(f"Could not describe table '{database_name}'.'{table_name}'. It might be a view or lack permissions.")

            for row in schema_results:
                col_name = row.get('Field')
                if col_name:
                    schema_info[col_name] = {
                        'type': row.get('Type'),
                        'nullable': row.get('Null', '').upper() == 'YES',
                        'key': row.get('Key'),
                        'default': row.get('Default'),
                        'extra': row.get('Extra')
                    }
            logger.info(f"TOOL END: get_table_schema completed. Columns found: {len(schema_info)}. Keys: {list(schema_info.keys())}")
            return schema_info
        except FileNotFoundError as e:
            logger.warning(f"TOOL WARNING: get_table_schema table not found: {e}")
            raise e
        except Exception as e:
            logger.error(f"TOOL ERROR: get_table_schema failed for database_name={database_name}, table_name={table_name}: {e}", exc_info=True)
            raise RuntimeError(f"Could not retrieve schema for table '{database_name}.{table_name}'.")
        
    async def get_table_schema_with_relations(self, database_name: str, table_name: str) -> Dict[str, Any]:
        """
        Retrieves table schema with foreign key relationship information.
        Includes all basic schema info plus foreign key relationships and referenced tables.
        """
        logger.info(f"TOOL START: get_table_schema_with_relations called. database_name={database_name}, table_name={table_name}")
        if not database_name or not database_name.isidentifier():
            logger.warning(f"TOOL WARNING: get_table_schema_with_relations called with invalid database_name: {database_name}")
            raise ValueError(f"Invalid database name provided: {database_name}")
        if not table_name or not table_name.isidentifier():
            logger.warning(f"TOOL WARNING: get_table_schema_with_relations called with invalid table_name: {table_name}")
            raise ValueError(f"Invalid table name provided: {table_name}")

        try:
            # 1. Get basic schema information
            basic_schema = await self.get_table_schema(database_name, table_name)
            
            # 2. Retrieve foreign key information
            fk_sql = """
            SELECT 
                kcu.COLUMN_NAME as column_name,
                kcu.CONSTRAINT_NAME as constraint_name,
                kcu.REFERENCED_TABLE_NAME as referenced_table,
                kcu.REFERENCED_COLUMN_NAME as referenced_column,
                rc.UPDATE_RULE as on_update,
                rc.DELETE_RULE as on_delete
            FROM information_schema.KEY_COLUMN_USAGE kcu
            INNER JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
                ON kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                AND kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
            WHERE kcu.TABLE_SCHEMA = %s 
              AND kcu.TABLE_NAME = %s 
              AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
            """
            
            fk_results = await self._execute_query(fk_sql, params=(database_name, table_name))
            
            # 3. Add foreign key information to the basic schema
            enhanced_schema = {}
            for col_name, col_info in basic_schema.items():
                enhanced_schema[col_name] = col_info.copy()
                enhanced_schema[col_name]['foreign_key'] = None
            
            # 4. Add foreign key information to the corresponding columns
            for fk_row in fk_results:
                column_name = fk_row['column_name']
                if column_name in enhanced_schema:
                    enhanced_schema[column_name]['foreign_key'] = {
                        'constraint_name': fk_row['constraint_name'],
                        'referenced_table': fk_row['referenced_table'],
                        'referenced_column': fk_row['referenced_column'],
                        'on_update': fk_row['on_update'],
                        'on_delete': fk_row['on_delete']
                    }
            
            # 5. Return the enhanced schema with foreign key relations
            result = {
                'table_name': table_name,
                'columns': enhanced_schema
            }
            
            logger.info(f"TOOL END: get_table_schema_with_relations completed. Columns: {len(enhanced_schema)}, Foreign keys: {len(fk_results)}")
            return result
            
        except Exception as e:
            logger.error(f"TOOL ERROR: get_table_schema_with_relations failed for database_name={database_name}, table_name={table_name}: {e}", exc_info=True)
            raise RuntimeError(f"Could not retrieve schema with relations for table '{database_name}.{table_name}': {str(e)}")


    async def execute_sql(self, sql_query: str, database_name: str, parameters: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        """
        Executes a SQL query (primarily SELECT, SHOW, DESCRIBE) against a specified database
        and returns the results. Uses parameterized queries for safety.
        Example `parameters`: ["value1", 123] corresponding to %s placeholders in `sql_query`.
        """
        logger.info(f"TOOL START: execute_sql called. database_name={database_name}, sql_query={sql_query[:100]}, parameters={parameters}")
        if database_name and not database_name.isidentifier():
            logger.warning(f"TOOL WARNING: execute_sql called with invalid database_name: {database_name}")
            raise ValueError(f"Invalid database name provided: {database_name}")
        param_tuple = tuple(parameters) if parameters is not None else None
        try:
            results = await self._execute_query(sql_query, params=param_tuple, database=database_name)
            logger.info(f"TOOL END: execute_sql completed. Rows returned: {len(results)}.")
            return results
        except Exception as e:
            logger.error(f"TOOL ERROR: execute_sql failed for database_name={database_name}, sql_query={sql_query[:100]}, parameters={parameters}: {e}", exc_info=True)
            raise
            
    async def create_database(self, database_name: str) -> Dict[str, Any]:
        """
        Creates a new database if it doesn't exist.
        """
        logger.info(f"TOOL START: create_database called for database: '{database_name}'")
        if not database_name or not database_name.isidentifier():
            logger.error(f"Invalid database_name for creation: '{database_name}'. Must be a valid identifier.")
            raise ValueError(f"Invalid database_name for creation: '{database_name}'. Must be a valid identifier.")

        # Check existence first to provide a clear message, though CREATE DATABASE IF NOT EXISTS is idempotent
        if await self._database_exists(database_name):
            message = f"Database '{database_name}' already exists."
            logger.info(f"TOOL END: create_database. {message}")
            return {"status": "exists", "message": message, "database_name": database_name}

        sql = f"CREATE DATABASE IF NOT EXISTS `{database_name}`;"

        try:
            await self._execute_query(sql, database=None)

            message = f"Database '{database_name}' created successfully."
            logger.info(f"TOOL END: create_database. {message}")
            return {"status": "success", "message": message, "database_name": database_name}
        except Exception as e:
            error_message = f"Failed to create database '{database_name}'."
            logger.error(f"TOOL ERROR: create_database. {error_message} Error: {e}", exc_info=True)
            raise RuntimeError(f"{error_message} Reason: {str(e)}")

    # --- Tool Registration (Synchronous) ---
    def register_tools(self):
        """Registers the class methods as MCP tools using the instance. This is synchronous."""
        if self.pool is None:
             logger.error("Cannot register tools: Database pool is not initialized.")
             raise RuntimeError("Database pool must be initialized before registering tools.")

        @self.mcp.tool
        async def list_databases() -> List[str]:
            """Lists all accessible databases on the connected MariaDB server."""
            return await self.list_databases()
            
        @self.mcp.tool
        async def list_tables(database_name: str) -> List[str]:
            """Lists all tables within the specified database."""
            return await self.list_tables(database_name)
            
        @self.mcp.tool
        async def get_table_schema(database_name: str, table_name: str) -> Dict[str, Any]:
            """Retrieves the schema for a specific table in a database."""
            return await self.get_table_schema(database_name, table_name)
            
        @self.mcp.tool
        async def get_table_schema_with_relations(database_name: str, table_name: str) -> Dict[str, Any]:
            """Retrieves table schema with foreign key relationship information."""
            return await self.get_table_schema_with_relations(database_name, table_name)
            
        @self.mcp.tool
        async def execute_sql(sql_query: str, database_name: str, parameters: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
            """Executes a read-only SQL query against a specified database."""
            return await self.execute_sql(sql_query, database_name, parameters)
            
        @self.mcp.tool
        async def create_database(database_name: str) -> Dict[str, Any]:
            """Creates a new database if it doesn't exist."""
            return await self.create_database(database_name)
            
        logger.info("Registered MCP tools explicitly.")

    # --- Async Main Server Logic ---
    async def run_async_server(self, transport="stdio", host="127.0.0.1", port=9001, path="/mcp"):
        """
        Initializes pool, registers tools, and runs the appropriate async MCP listener.
        This method should be the target for anyio.run().
        """
        try:
            # 1. Initialize pool within the anyio-managed loop
            await self.initialize_pool()

            # 2. Register tools (synchronous part, but called from async context)
            self.register_tools()

            # 3. Prepare transport arguments
            transport_kwargs = {}
            if transport != "stdio":
                middleware = [
                    Middleware(
                        CORSMiddleware,
                        allow_origins=ALLOWED_ORIGINS,
                        allow_methods=["GET", "POST"],
                        allow_headers=["*"],
                    ),
                    Middleware(TrustedHostMiddleware, 
                               allowed_hosts=ALLOWED_HOSTS)
                ]
            if transport == "sse":
                transport_kwargs = {"host": host, "port": port, "middleware": middleware}
                logger.info(f"Starting MCP server via {transport} on {host}:{port}...")
            elif transport == "http":
                transport_kwargs = {"host": host, "port": port, "path": path, "middleware": middleware}
                logger.info(f"Starting MCP server via {transport} on {host}:{port}{path}...")
            elif transport == "stdio":
                 logger.info(f"Starting MCP server via {transport}...")
            else:
                 logger.error(f"Unsupported transport type: {transport}")
                 return 

            # 4. Run the appropriate async listener from FastMCP
            await self.mcp.run_async(transport=transport, **transport_kwargs)

        except (ConnectionError, AsyncMyError, RuntimeError) as e:
            logger.critical(f"Server setup failed: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.critical(f"Server execution failed with an unexpected error: {e}", exc_info=True)
            raise
        finally:
            await self.close_pool()


# --- Main Execution Block ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MariaDB MCP Server")
    parser.add_argument('--transport', type=str, default='stdio', choices=['stdio', 'sse', 'http'],
                        help='MCP transport protocol (stdio, sse, or http)')
    parser.add_argument('--host', type=str, default='127.0.0.1',
                        help='Host for SSE or HTTP transport')
    parser.add_argument('--port', type=int, default=9001,
                        help='Port for SSE or HTTP transport')
    parser.add_argument('--path', type=str, default='/mcp',
                        help='Path for HTTP transport (default: /mcp)')
    args = parser.parse_args()

    # 1. Create the server instance
    server = MariaDBServer()
    exit_code = 0

    try:
        # 2. Use anyio.run to manage the event loop and call the main async server logic
        anyio.run(
            partial(server.run_async_server, 
                    transport=args.transport, 
                    host=args.host, 
                    port=args.port, 
                    path=args.path)
        )
        logger.info("Server finished gracefully.")

    except KeyboardInterrupt:
         logger.info("Server execution interrupted by user.")
    except Exception as e:
         logger.critical(f"Server failed to start or crashed: {e}", exc_info=True)
         exit_code = 1
    finally:
        logger.info(f"Server exiting with code {exit_code}.")