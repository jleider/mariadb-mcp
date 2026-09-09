"""
Integration tests for the connection pool's shutdown path, run against a real
MariaDB — the server's own log is the only place a non-graceful disconnect shows
up, so a mock cannot catch this.

Start the database first:
    docker compose -f docker-compose.test.yml up -d --wait

Then:
    python -m unittest src.tests.test_integration_pool_lifecycle -v

These skip (rather than fail) when the container is not reachable.
"""
from server import MariaDBServer

from src.tests.support import MariaDBIntegrationTestCase, TEST_DB_NAME


class TestPoolCloseIsGraceful(MariaDBIntegrationTestCase):
    """
    close_pool() must send COM_QUIT for every connection it owns.

    asyncmy's Pool.wait_closed() and Pool.release() both close free connections
    with Connection.close(), which drops the socket outright; the server counts
    each one in Aborted_clients and logs "Got an error reading communication
    packets". Only Pool.clear() sends COM_QUIT, and only before close() sets
    _closing.
    """

    async def _aborted_clients(self) -> int:
        # self.server's own pool stays open across the measurement, so reading
        # the counter cannot perturb it.
        rows = await self.server._execute_query(
            "SHOW GLOBAL STATUS LIKE 'Aborted_clients'"
        )
        return int(rows[0]['Value'])

    async def test_close_pool_aborts_no_connections(self):
        before = await self._aborted_clients()

        server = MariaDBServer(server_name="MariaDB_Pool_Lifecycle_Test")
        await server.initialize_pool()
        # Force at least one connection beyond the pool's minsize into use, so
        # the test covers a released connection and not only a never-acquired one.
        await server.list_tables(TEST_DB_NAME)
        await server.close_pool()

        self.assertEqual(await self._aborted_clients(), before)
