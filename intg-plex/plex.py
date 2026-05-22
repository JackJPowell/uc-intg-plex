"""
This module implements Plex communication of the Remote Two integration driver.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
import io
import logging
import time
from asyncio import AbstractEventLoop
from io import BytesIO
from typing import Any

import aiohttp
from const import PlexConfig
from PIL import Image
from plexapi.base import MediaContainer
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexClient
from plexapi.server import PlexServer as PlexApiServer
from plexwebsocket import SIGNAL_CONNECTION_STATE, STATE_CONNECTED, PlexWebsocket

from ucapi.media_player import Attributes as MediaPlayerAttrs, MediaContentType
from ucapi.media_player import States as MediaStates
from ucapi_framework import (
    ExternalClientDevice,
    BaseIntegrationDriver,
)

_LOG = logging.getLogger(__name__)

_STATE_STOPPED = "stopped"  # plexwebsocket STATE_STOPPED constant
DEFAULT_TIMEOUT = 8.0
WEBSOCKET_WATCHDOG_INTERVAL = 10
CONNECTION_RETRIES = 10


class PlexServer(ExternalClientDevice):
    """Representing a Plex Server connection and device control."""

    def __init__(
        self,
        device_config: PlexConfig,
        loop: AbstractEventLoop | None = None,
        config_manager: Any = None,
        driver: BaseIntegrationDriver | None = None,
    ):
        """Create instance with Plex client."""
        super().__init__(
            device_config,
            loop,
            enable_watchdog=True,
            watchdog_interval=WEBSOCKET_WATCHDOG_INTERVAL,
            reconnect_delay=5,
            max_reconnect_attempts=CONNECTION_RETRIES,
            config_manager=config_manager,
            driver=driver,
        )

        self.event_loop = self._loop

        # Plex-specific state
        self._plex: PlexApiServer | None = None  # Server connection (stateless HTTP)
        self._plex_client: PlexClient | None = (
            None  # Player client for sending commands
        )
        self._session: MediaContainer | None = None
        self._image_cache = None
        self._image_cache_url = None
        self._background_tasks: set[asyncio.Task] = set()
        self._listen_task: asyncio.Task | None = None
        self._connect_lock = asyncio.Lock()
        self._connect_client_lock = asyncio.Lock()

        # State stored as a plain dict keyed by MediaPlayer Attributes enum.
        # Entities read from get_media_player_attributes(); device calls push_update().
        self._attributes: dict[str, object] = {
            MediaPlayerAttrs.STATE: MediaStates.UNKNOWN,
            MediaPlayerAttrs.VOLUME: 0,
            MediaPlayerAttrs.MUTED: False,
            MediaPlayerAttrs.MEDIA_DURATION: 0,
            MediaPlayerAttrs.MEDIA_POSITION: 0,
            MediaPlayerAttrs.MEDIA_IMAGE_URL: "",
            MediaPlayerAttrs.MEDIA_TITLE: "",
            MediaPlayerAttrs.MEDIA_ARTIST: "",
            MediaPlayerAttrs.MEDIA_ALBUM: "",
        }

        _LOG.debug(
            "Plex instance created: %s", device_config.identifier
        )  # ─────────────────────────────────────────────────────────────────

    # ExternalClientDevice abstract method implementations
    # ─────────────────────────────────────────────────────────────────

    async def create_client(self) -> PlexWebsocket:
        """
        Create the PlexWebsocket client instance.

        This also establishes the HTTP connection to the Plex server.
        """
        if self._connect_lock.locked():
            _LOG.warning(
                "[%s] Connection already in progress, waiting for lock...",
                self.identifier,
            )

        async with self._connect_lock:
            return await self._create_client_locked()

    async def _create_client_locked(self) -> PlexWebsocket:
        """Inner create_client logic, always called with _connect_lock held."""
        if self._client is not None:
            if self._client.state != _STATE_STOPPED:
                # Client is brand-new, starting, or connected — reuse it.
                # This handles the framework's double create_client call: the second
                # call should not close the client that the first call is still setting up.
                _LOG.debug(
                    "[%s] WebSocket client already in state=%s, reusing",
                    self.identifier,
                    self._client.state,
                )
                return self._client
            # Client is stopped (stale from a previous session) — clean up and reconnect.
            _LOG.debug(
                "[%s] Closing stale WebSocket client before new connection",
                self.identifier,
            )
            await self._close_websocket_gracefully()

        t0 = time.perf_counter()
        _LOG.info("[%s] Connecting to Plex server...", self.identifier)

        # Run blocking HTTP connection in executor to avoid blocking the event loop
        self._plex = await self.event_loop.run_in_executor(None, self._get_plex_server)

        if not self._plex:
            elapsed = time.perf_counter() - t0
            _LOG.error(
                "[%s] Server connection failed after %.2fs", self.identifier, elapsed
            )
            raise ConnectionError(f"Failed to connect to Plex server at {self.address}")

        _LOG.info(
            "[%s] Server connected in %.2fs, creating WebSocket client",
            self.identifier,
            time.perf_counter() - t0,
        )

        # Create the websocket client
        return PlexWebsocket(
            plex_server=self._plex,
            callback=self._plex_ws_updates,
            subscriptions=["playing", "status", "progress"],
        )

    async def connect_client(self) -> None:
        """
        Connect the PlexWebsocket client and set up event handlers.

        The PlexWebsocket.listen() method starts the connection.
        """
        if self._connect_client_lock.locked():
            _LOG.debug(
                "[%s] connect_client already in progress, skipping duplicate call",
                self.identifier,
            )
            return

        async with self._connect_client_lock:
            t0 = time.perf_counter()
            _LOG.info("[%s] Starting WebSocket listen...", self.identifier)

            # Start listening (this runs in background)
            # Track separately so _close_websocket_gracefully only waits for this task.
            self._listen_task = self.event_loop.create_task(self._client.listen())

            # Wait for websocket to connect with timeout
            try:
                await asyncio.wait_for(
                    self._wait_for_websocket_connection(), timeout=5.0
                )
                _LOG.info(
                    "[%s] WebSocket connected in %.2fs",
                    self.identifier,
                    time.perf_counter() - t0,
                )
            except asyncio.TimeoutError:
                _LOG.warning(
                    "[%s] WebSocket connection timed out after %.2fs — watchdog will retry if needed",
                    self.identifier,
                    time.perf_counter() - t0,
                )

            # Get initial session state
            t1 = time.perf_counter()
            _LOG.debug("[%s] Fetching initial session state...", self.identifier)
            await self._update_session_state()
            _LOG.info(
                "[%s] Initial session state fetched in %.2fs (state=%s, total=%.2fs)",
                self.identifier,
                time.perf_counter() - t1,
                self._attributes.get(MediaPlayerAttrs.STATE),
                time.perf_counter() - t0,
            )

            _LOG.debug("[%s] WebSocket state: %s", self.identifier, self._client.state)

    async def disconnect_client(self) -> None:
        """
        Disconnect the PlexWebsocket client.
        """
        t0 = time.perf_counter()
        _LOG.info("[%s] Disconnecting...", self.identifier)

        # Signal stop and wait for listen() to clean up its aiohttp session gracefully
        await self._close_websocket_gracefully()

        # Clear player client reference
        if self._plex_client:
            self._plex_client = None

        # Reset state
        self._reset_state()

        _LOG.info(
            "[%s] Disconnected in %.2fs", self.identifier, time.perf_counter() - t0
        )

    def check_client_connected(self) -> bool:
        """
        Check if the PlexWebsocket is connected.

        This queries the actual connection state of the websocket.
        """
        return self._client is not None and self._client.state == STATE_CONNECTED

    # ─────────────────────────────────────────────────────────────────
    # Helper methods
    # ─────────────────────────────────────────────────────────────────

    def _create_task(self, coro):
        """Create a background task and track it."""
        task = self.event_loop.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _close_websocket_gracefully(self, timeout: float = 2.0) -> None:
        """
        Cleanly close the WebSocket by signalling stop, then waiting for the
        listen() coroutine to exit naturally so aiohttp can close its session.
        Force-cancels remaining tasks if they don't finish within *timeout* seconds.
        """
        if self._client is not None:
            try:
                self._client.close()  # sets the internal stop flag
            except Exception:  # pylint: disable=broad-exception-caught
                pass

        # Wait specifically for the listen task so aiohttp can close its session cleanly.
        if self._listen_task is not None and not self._listen_task.done():
            _LOG.debug(
                "[%s] Waiting for listen task to finish gracefully...", self.identifier
            )
            done, pending = await asyncio.wait({self._listen_task}, timeout=timeout)
            if pending:
                _LOG.debug(
                    "[%s] Listen task still running after %.1fs, force-cancelling",
                    self.identifier,
                    timeout,
                )
                self._listen_task.cancel()
                await asyncio.gather(self._listen_task, return_exceptions=True)
        self._listen_task = None

        # Cancel any in-flight fetch tasks immediately (don't wait for them).
        if self._background_tasks:
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

    async def _wait_for_websocket_connection(self):
        """Wait for websocket to connect."""
        while self._client and self._client.state != STATE_CONNECTED:
            await asyncio.sleep(0.1)

    async def _update_session_state(self):
        """Update session state asynchronously."""
        t0 = time.perf_counter()
        self._session = await self.event_loop.run_in_executor(
            None, self.get_session_by_client_id, self.device_config.identifier
        )
        elapsed = time.perf_counter() - t0
        if self._session:
            title = getattr(self._session, "title", "unknown")
            _LOG.info(
                "[%s] Active session found in %.2fs: %s",
                self.identifier,
                elapsed,
                title,
            )
            self._plex_client = self.get_plex_client()

            # Determine play state from the player
            play_state = "playing"
            players = getattr(self._session, "players", [])
            if players:
                play_state = getattr(players[0], "state", "playing")

            if play_state == "paused":
                self._attributes[MediaPlayerAttrs.STATE] = MediaStates.PAUSED
            else:
                self._attributes[MediaPlayerAttrs.STATE] = MediaStates.ON

            # Populate all media attributes so the remote sees correct info immediately
            if self._session.TYPE == "audio":
                media_type = MediaContentType.MUSIC
            elif self._session.TYPE == "episode":
                media_type = MediaContentType.TV_SHOW
            elif self._session.TYPE == "video":
                media_type = MediaContentType.VIDEO
            else:
                media_type = ""

            duration = getattr(self._session, "duration", 0)
            self._attributes[MediaPlayerAttrs.MEDIA_DURATION] = int(
                duration / 1000 if isinstance(duration, (int, float)) else 0
            )
            self._attributes[MediaPlayerAttrs.MEDIA_TYPE] = media_type
            self._attributes[MediaPlayerAttrs.MEDIA_TITLE] = getattr(
                self._session, "title", ""
            )

            if hasattr(self._session, "type") and self._session.type == "episode":
                season_episode = getattr(self._session, "seasonEpisode", "")
                if season_episode and isinstance(season_episode, str):
                    self._attributes[MediaPlayerAttrs.MEDIA_ARTIST] = (
                        season_episode.upper()
                    )

            url = self._get_artwork_url(self._session)
            self._attributes[MediaPlayerAttrs.MEDIA_IMAGE_URL] = url
        else:
            _LOG.info("[%s] No active session (%.2fs)", self.identifier, elapsed)
            self._attributes[MediaPlayerAttrs.STATE] = MediaStates.OFF
        self.push_update()

    def _get_plex_server(self) -> PlexApiServer | None:
        """Get a reference to the PMS (stateless HTTP connection)."""
        config = self._device_config

        # Ensure address has http:// scheme
        address = config.address
        if not address.startswith("http://") and not address.startswith("https://"):
            address = f"http://{address}"

        url = f"{address}:{config.port}"
        _LOG.info("[%s] Attempting Plex server connection to %s", self.identifier, url)
        t0 = time.perf_counter()
        try:
            if config.auth_token:
                server = PlexApiServer(baseurl=url, token=config.auth_token, timeout=5)
            else:
                account = MyPlexAccount(config.username, config.password)
                server = account.resource(config.server_name).connect()
            _LOG.info(
                "[%s] Plex server connected in %.2fs (baseurl=%s)",
                self.identifier,
                time.perf_counter() - t0,
                server._baseurl,
            )
            return server
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error(
                "[%s] Cannot connect to %s after %.2fs: %s",
                self.identifier,
                url,
                time.perf_counter() - t0,
                ex,
            )
            return None

    def get_state(self) -> MediaStates | object:
        """Get state of device."""
        return self._attributes.get(MediaPlayerAttrs.STATE, MediaStates.OFF)  # type: ignore[return-value]

    def _reset_state(self):
        # Reset state attributes to defaults
        self._attributes = {
            MediaPlayerAttrs.STATE: MediaStates.UNKNOWN,
            MediaPlayerAttrs.VOLUME: 0,
            MediaPlayerAttrs.MUTED: False,
            MediaPlayerAttrs.MEDIA_DURATION: 0,
            MediaPlayerAttrs.MEDIA_POSITION: 0,
            MediaPlayerAttrs.MEDIA_IMAGE_URL: "",
            MediaPlayerAttrs.MEDIA_TITLE: "",
            MediaPlayerAttrs.MEDIA_ARTIST: "",
            MediaPlayerAttrs.MEDIA_ALBUM: "",
        }
        # Clear image cache to free memory
        self._image_cache = None

    def _plex_ws_updates(self, msgtype, data, error) -> None:
        """Handle WS Messages from PlexWebsocket."""
        # Handle connection state change signals from PlexWebsocket
        if msgtype == SIGNAL_CONNECTION_STATE:
            _LOG.info("[%s] WebSocket connection state: %s", self.identifier, data)
            return

        # _LOG.debug("[%s] WebSocket message received: type=%s", self.identifier, msgtype)

        payload = None

        if msgtype == "playing":
            match data["type"]:
                case "playing" | "paused":
                    if data["PlaySessionStateNotification"]:
                        for item in data["PlaySessionStateNotification"]:
                            if (
                                item["clientIdentifier"]
                                == self.device_config.identifier
                            ):
                                payload = item
                                break

                        if payload:
                            # Update state immediately from websocket data
                            play_state = payload["state"]
                            view_offset = payload.get("viewOffset", 0)
                            _LOG.debug(
                                "[%s] Session update: state=%s, position=%.1fs",
                                self.identifier,
                                play_state,
                                view_offset / 1000,
                            )

                            if play_state == "stopped":
                                self._image_cache = None
                                self._attributes[MediaPlayerAttrs.STATE] = (
                                    MediaStates.OFF
                                )
                                self.push_update()
                            elif play_state == "paused":
                                media_position = payload["viewOffset"] / 1000
                                self._attributes[MediaPlayerAttrs.STATE] = (
                                    MediaStates.PAUSED
                                )
                                self._attributes[MediaPlayerAttrs.MEDIA_POSITION] = int(
                                    media_position
                                )
                                self._create_task(
                                    self._fetch_session_details(
                                        payload, self.identifier
                                    )
                                )
                            elif play_state == "playing":
                                media_position = payload["viewOffset"] / 1000
                                self._attributes[MediaPlayerAttrs.STATE] = (
                                    MediaStates.PLAYING
                                )
                                self._attributes[MediaPlayerAttrs.MEDIA_POSITION] = int(
                                    media_position
                                )
                                self._create_task(
                                    self._fetch_session_details(
                                        payload, self.identifier
                                    )
                                )

        if error:
            _LOG.warning("[%s] WebSocket error: %s", self.identifier, error)

    async def _fetch_session_details(self, payload: dict, identifier: str):
        """Fetch full session details without blocking the event loop."""
        t0 = time.perf_counter()
        _LOG.debug("[%s] Fetching session details...", identifier)
        try:
            # Run blocking Plex HTTP call in a thread pool executor
            session = await self.event_loop.run_in_executor(
                None, self.get_session_by_client_id, self.device_config.identifier
            )

            if not session:
                _LOG.debug(
                    "[%s] No session found (%.2fs)",
                    identifier,
                    time.perf_counter() - t0,
                )
                return

            _LOG.debug(
                "[%s] Session details fetched in %.2fs: %s",
                identifier,
                time.perf_counter() - t0,
                getattr(session, "title", "unknown"),
            )

            self._session = session

            if session.TYPE == "audio":
                media_type = MediaContentType.MUSIC
            elif session.TYPE == "episode":
                media_type = MediaContentType.TV_SHOW
            elif session.TYPE == "video":
                media_type = MediaContentType.VIDEO
            else:
                media_type = ""

            # Build updated data with safe attribute access
            duration = getattr(session, "duration", 0)
            title = getattr(session, "title", "")
            duration_seconds = int(
                duration / 1000 if isinstance(duration, (int, float)) else 0
            )

            # Update attributes dict
            self._attributes[MediaPlayerAttrs.MEDIA_DURATION] = duration_seconds
            self._attributes[MediaPlayerAttrs.MEDIA_TYPE] = media_type
            self._attributes[MediaPlayerAttrs.MEDIA_TITLE] = title

            if hasattr(session, "type") and session.type == "episode":
                season_episode = getattr(session, "seasonEpisode", "")
                if season_episode and isinstance(season_episode, str):
                    self._attributes[MediaPlayerAttrs.MEDIA_ARTIST] = (
                        season_episode.upper()
                    )

            # Get artwork URL
            url = self._get_artwork_url(session)
            self._attributes[MediaPlayerAttrs.MEDIA_IMAGE_URL] = url

            # Notify entities that state changed
            self.push_update()

            # Fetch image asynchronously
            # self._create_task(self._fetch_and_update_image(url, identifier))

        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error(
                "Failed to fetch session details for %s: %s",
                identifier,
                ex,
                exc_info=True,
            )

    def _get_artwork_url(self, session) -> str:
        """Get artwork URL based on configuration."""
        try:
            if session.type == "episode":
                match self._device_config.tv_selection:
                    case "tv-poster-series":
                        return self.build_plex_url(session.grandparentThumb)
                    case "tv-poster-season":
                        return self.build_plex_url(session.parentThumb)
                    case "tv-poster-episode":
                        return self.build_plex_url(session.thumb)
                    case "tv-poster-art":
                        return session.artUrl
                    case _:
                        return self.build_plex_url(session.grandparentThumb)
            else:
                match self._device_config.movie_selection:
                    case "movie-poster":
                        return session.posterUrl
                    case "movie-art":
                        return session.artUrl
                    case _:
                        return session.posterUrl
        except (AttributeError, KeyError) as ex:
            _LOG.debug("Error getting artwork URL, using fallback: %s", ex)
            # Fallback to default artwork
            try:
                if session.type == "episode":
                    return self.build_plex_url(session.grandparentThumb)
                else:
                    return session.posterUrl
            except (AttributeError, KeyError):
                return ""

    async def _fetch_and_update_image(self, url: str, identifier: str):
        """Fetch image asynchronously and push state update to subscribed entities."""
        try:
            image_data = await self.store_image_as_base64(url, 400)
            if image_data:
                self._attributes[MediaPlayerAttrs.MEDIA_IMAGE_URL] = image_data
                self.push_update()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error("Failed to fetch and update image: %s", ex)

    async def store_image_as_base64(self, url, max_size):
        """Retrieve and store image as base64 data."""

        # Check if we need to fetch a new image (cache miss or different URL)
        if not self._image_cache or self._image_cache_url != url:
            try:
                # Use a transient session for each image fetch to avoid lifecycle issues
                # This ensures the session is always properly closed
                timeout = aiohttp.ClientTimeout(total=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            f = await response.read()
                            image = Image.open(BytesIO(f))

                            width, height = image.size

                            if max_size >= max(width, height):
                                byte_image = io.BytesIO()
                                image.save(byte_image, format="PNG")
                                byte_image = byte_image.getvalue()
                                image_b64 = base64.b64encode(byte_image).decode("utf-8")
                                self._image_cache = f"data:image/png;base64,{image_b64}"
                                self._image_cache_url = url
                                return self._image_cache

                            if width > height:
                                new_width = max_size
                                new_height = int(height * (max_size / width))
                            else:
                                new_height = max_size
                                new_width = int(width * (max_size / height))

                            resized_image = image.resize(
                                (new_width, new_height), Image.Resampling.LANCZOS
                            )

                            resized_bytes = io.BytesIO()
                            resized_image.save(resized_bytes, format="PNG")
                            resized_bytes_value = resized_bytes.getvalue()

                            image_b64 = base64.b64encode(resized_bytes_value).decode(
                                "utf-8"
                            )
                            self._image_cache = f"data:image/png;base64,{image_b64}"
                            self._image_cache_url = url
                            return self._image_cache
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.error("Failed to fetch image from %s: %s", url, ex)
                return ""
        return self._image_cache if self._image_cache else ""

    def build_plex_url(self, path: str) -> str:
        """Build a plex url from config and supplied path."""
        if not path:
            _LOG.warning("Empty path provided to build_plex_url")
            return ""

        config = self._device_config
        # Ensure address has http:// scheme
        address = config.address
        if not address.startswith("http://") and not address.startswith("https://"):
            address = f"http://{address}"
        return f"{address}:{config.port}{path}?X-Plex-Token={config.auth_token}"

    def get_players(self) -> list[Any]:
        """Get active players from session."""
        self._players = []
        if self._plex:
            for session in self._plex.sessions():
                for player in session.players:
                    self._players.append(player)
        return self._players

    def get_session_by_client_id(self, identifier) -> MediaContainer | None:
        """Get session by client identifier."""
        if not self._plex:
            return None
        t0 = time.perf_counter()
        try:
            sessions = self._plex.sessions()
            elapsed = time.perf_counter() - t0
            _LOG.debug(
                "[%s] Fetched %d session(s) from Plex in %.2fs",
                identifier,
                len(sessions),
                elapsed,
            )
            for session in sessions:
                for player in session.players:
                    if player.machineIdentifier == identifier and player.local is True:
                        return session
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error(
                "[%s] Failed to fetch sessions after %.2fs: %s",
                identifier,
                time.perf_counter() - t0,
                ex,
            )
        return None

    def get_plex_client(self) -> PlexClient | None:
        """Get client from session for sending playback commands."""
        if self._session:
            try:
                self._plex_client = self._session.player  # ty:ignore[unresolved-attribute]
                self._plex_client.proxyThroughServer(True, self._plex)

                return self._plex_client
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.error(
                    "Unable to connect to client (%s) %s",
                    self._session.player.device,  # ty:ignore[unresolved-attribute]
                    ex,
                )
        # self.events.emit(DeviceEvents.UPDATE, self.identifier, {MediaAttr.STATE: self.get_state()})
        return None

    # ─────────────────────────────────────────────────────────────────
    # Properties
    # ─────────────────────────────────────────────────────────────────

    @property
    def identifier(self) -> str:
        """Return device identifier."""
        return self._device_config.identifier

    @property
    def name(self) -> str:
        """Return device name."""
        return self._device_config.name

    @property
    def address(self) -> str:
        """Return device address."""
        return self._device_config.address

    @property
    def log_id(self) -> str:
        """Return log identifier for this device."""
        return f"PlexServer[{self.identifier}]"

    @property
    def available(self) -> bool:
        """Return True if device is available (connected)."""
        return self.is_connected

    @property
    def is_on(self) -> bool | None:
        """Whether the player is on (has active session)."""
        state = self._attributes.get(MediaPlayerAttrs.STATE)
        return state not in [MediaStates.OFF, MediaStates.UNKNOWN, None]

    @property
    def play_state(self) -> str | None:
        """Return the play state of the device."""
        state = self._attributes.get(MediaPlayerAttrs.STATE)
        if state == MediaStates.PLAYING:
            return "playing"
        elif state == MediaStates.PAUSED:
            return "paused"
        elif state == MediaStates.OFF:
            return "stopped"
        return None

    @property
    def device_config(self) -> PlexConfig:
        """Return device configuration."""
        return self._device_config

    @property
    def host(self) -> str:
        """Return the host of the device as string."""
        return self._device_config.identifier

    @property
    def state(self) -> MediaStates | object:
        """Return the cached state of the device."""
        return self.get_state()

    @property
    def is_volume_muted(self) -> bool | object:
        """Return boolean if volume is currently muted."""
        return self._attributes.get(MediaPlayerAttrs.MUTED, False)  # type: ignore[return-value]

    @property
    def volume_level(self) -> float | object:
        """Volume level of the media player (0..100)."""
        return self._attributes.get(MediaPlayerAttrs.VOLUME, 0)  # type: ignore[return-value]

    @property
    def media_image_url(self) -> str | object:
        """Image url of current playing media."""
        return self._attributes.get(MediaPlayerAttrs.MEDIA_IMAGE_URL, "")  # type: ignore[return-value]

    @property
    def client(self) -> PlexClient | None:
        """Return Plex Client for sending playback commands."""
        if not self._plex_client:
            self._plex_client = self.get_plex_client()
        return self._plex_client

    def get_media_player_attributes(self) -> dict:
        """
        Return current device state as a dict keyed by MediaPlayer Attributes enum.

        Entities call this inside ``sync_state()`` to pull fresh state from the device.

        :return: Shallow copy of the current attributes dict.
        """
        return dict(self._attributes)
