"""
Shared helpers for the integration tests.

The tests run against the disposable MariaDB defined in docker-compose.test.yml:

    docker compose -f docker-compose.test.yml up -d --wait

If that container is not reachable, the integration tests skip with an
explanatory message rather than failing, so the suite stays green on a machine
with no Docker. Connection details can be overridden with TEST_DB_* env vars.
"""
import os
import unittest
from unittest.mock import patch

import server as server_module
from server import MariaDBServer

TEST_DB_HOST = os.getenv("TEST_DB_HOST", "127.0.0.1")
TEST_DB_PORT = int(os.getenv("TEST_DB_PORT", 3307))
TEST_DB_USER = os.getenv("TEST_DB_USER", "mcp_test")
TEST_DB_PASSWORD = os.getenv("TEST_DB_PASSWORD", "mcp_test_pw")
TEST_DB_NAME = os.getenv("TEST_DB_NAME", "mcp_test")
TEST_DB_OTHER_NAME = os.getenv("TEST_DB_OTHER_NAME", "mcp_test_other")

_SKIP_MESSAGE = (
    f"Test MariaDB not available at {TEST_DB_HOST}:{TEST_DB_PORT} with the fixture "
    "schema loaded. Start it with: "
    "docker compose -f docker-compose.test.yml up -d --wait"
)

# Set REQUIRE_TEST_DATABASE=1 where the database is supposed to be present (CI).
# Skipping is the right default locally, but in CI a container that fails to
# start would otherwise leave the build green with every integration test
# skipped — passing while verifying almost nothing.
REQUIRE_TEST_DATABASE = os.getenv(
    "REQUIRE_TEST_DATABASE", "").strip().lower() not in ("", "0", "false", "no")

# Cache the probe result so a missing container costs one connection attempt
# for the whole run rather than one per test.
_availability = None


async def _probe() -> bool:
    """Connects and confirms the fixture schema is present."""
    import asyncmy

    try:
        conn = await asyncmy.connect(
            host=TEST_DB_HOST, port=TEST_DB_PORT, user=TEST_DB_USER,
            password=TEST_DB_PASSWORD, db=TEST_DB_NAME, connect_timeout=3,
        )
    except Exception:
        return False

    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'documented'",
                (TEST_DB_NAME,),
            )
            row = await cursor.fetchone()
            return bool(row and row[0])
    except Exception:
        return False
    finally:
        # ensure_closed() sends COM_QUIT; close() just drops the socket, which the
        # server logs as an aborted connection.
        await conn.ensure_closed()


async def skip_unless_test_database():
    """
    Raises SkipTest unless the fixture database is reachable.

    With REQUIRE_TEST_DATABASE set, raises RuntimeError instead so the run fails
    loudly rather than skipping.
    """
    global _availability
    if _availability is None:
        _availability = await _probe()
    if not _availability:
        if REQUIRE_TEST_DATABASE:
            raise RuntimeError(
                f"REQUIRE_TEST_DATABASE is set, so skipping is not allowed. {_SKIP_MESSAGE}"
            )
        raise unittest.SkipTest(_SKIP_MESSAGE)


class MariaDBIntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    """
    Base class that points MariaDBServer at the test container and manages the
    connection pool. Subclasses get a live `self.server`.

    `server.py` binds the DB settings at import time (`from config import ...`),
    so they are patched in the server module's namespace rather than in config.
    """

    #: Set to True to exercise read-only enforcement.
    read_only = False

    async def asyncSetUp(self):
        await skip_unless_test_database()

        patcher = patch.multiple(
            server_module,
            DB_HOST=TEST_DB_HOST,
            DB_PORT=TEST_DB_PORT,
            DB_USER=TEST_DB_USER,
            DB_PASSWORD=TEST_DB_PASSWORD,
            DB_NAME=TEST_DB_NAME,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.server = MariaDBServer(server_name="MariaDB_Test_Server")
        self.server.is_read_only = self.read_only
        await self.server.initialize_pool()
        self.addAsyncCleanup(self.server.close_pool)


def tool_payload(result):
    """
    Extracts a tool call's payload from a fastmcp CallToolResult.

    fastmcp 3.x returns an object with `.data` (deserialized) and `.content`
    (the raw content blocks); it is not subscriptable.
    """
    return result.data
