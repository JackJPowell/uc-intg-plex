"""Verify framework recovery after wake and Plex WebSocket failure reporting."""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "intg-plex"))

from const import PlexConfig
from plex import PlexServer


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = PlexServer(PlexConfig(
            identifier="player", name="Plex", address="10.0.0.10",
            username="", password="", auth_token="test", server_name="",
            port="32400", tv_selection="tv-poster-series",
            movie_selection="movie-poster",
        ))
        self.addAsyncCleanup(self.device.disconnect)

    async def test_framework_recovers_after_initial_network_failure(self):
        recovered = asyncio.Event()
        self.device._watchdog_interval = 0.001
        self.device._reconnect_delay = 0

        async def connected():
            recovered.set()

        with (
            patch.object(self.device, "create_client", new_callable=AsyncMock,
                         side_effect=[ConnectionError("Network is unreachable"), Mock()]) as create,
            patch.object(self.device, "connect_client", side_effect=connected),
            patch.object(self.device, "disconnect_client", new_callable=AsyncMock),
        ):
            self.assertFalse(await self.device.connect())
            self.assertIsNotNone(self.device._watchdog_task)
            await asyncio.wait_for(recovered.wait(), 1)
            self.assertEqual(create.await_count, 2)
            await self.device.disconnect()
            self.assertIsNone(self.device._watchdog_task)

    async def test_standby_stops_framework_retry_delay(self):
        retrying = asyncio.Event()
        self.device._watchdog_interval = 0.001
        self.device._reconnect_delay = 30
        wait_for_stop = self.device._stop_watchdog.wait

        async def wait_for_retry():
            retrying.set()
            await wait_for_stop()

        with (
            patch.object(self.device, "create_client", new_callable=AsyncMock,
                         side_effect=ConnectionError("Network is unreachable")) as create,
            patch.object(self.device._stop_watchdog, "wait", side_effect=wait_for_retry),
            patch.object(self.device, "_cleanup_client", new_callable=AsyncMock) as cleanup,
        ):
            self.assertFalse(await self.device.connect())
            await asyncio.wait_for(retrying.wait(), 1)
            watchdog = self.device._watchdog_task
            self.assertFalse(watchdog.done())
            cleanup.assert_not_awaited()
            await asyncio.wait_for(self.device.disconnect(), 1)
            self.assertEqual(create.await_count, 1)
            cleanup.assert_not_awaited()
            self.assertTrue(watchdog.done())
            self.assertIsNone(self.device._watchdog_task)

    async def test_websocket_timeout_is_a_failed_connection(self):
        self.device._client = SimpleNamespace(listen=AsyncMock(), close=Mock())
        with patch.object(self.device, "_wait_for_websocket_connection",
                          new_callable=AsyncMock, side_effect=TimeoutError):
            with self.assertRaisesRegex(ConnectionError, "WebSocket connection timed out"):
                await self.device.connect_client()


if __name__ == "__main__":
    unittest.main()
