"""
This module implements Plex communication of the Remote Two integration driver.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
from datetime import datetime, UTC
import io
import logging
from asyncio import AbstractEventLoop
from io import BytesIO
from typing import Any

import aiohttp
from const import PlexDevice
from const import PLEX_FEATURES
from PIL import Image
from plexapi.base import MediaContainer
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer as PlexApiServer, PlexClient
from plexwebsocket import SIGNAL_CONNECTION_STATE, STATE_CONNECTED, PlexWebsocket
from ucapi.media_player import (
    States as MediaStates,
    Features,
    Attributes as MediaAttr,
    MediaType,
)
from ucapi_framework import ExternalClientDevice
from ucapi_framework.device import DeviceEvents

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
WEBSOCKET_WATCHDOG_INTERVAL = 10
CONNECTION_RETRIES = 10


class PlexServer(ExternalClientDevice):
    """Representing a Plex Server connection and device control."""

    def __init__(
        self,
        device: PlexDevice,
        loop: AbstractEventLoop | None = None,
        config_manager: Any = None,
    ):
        """Create instance with Plex client."""
        # Initialize base class with watchdog settings
        # The ExternalClientDevice provides: self.events, self._device_config, self._loop, self._client
        super().__init__(
            device,
            loop,
            enable_watchdog=True,
            watchdog_interval=WEBSOCKET_WATCHDOG_INTERVAL,
            reconnect_delay=5,
            max_reconnect_attempts=CONNECTION_RETRIES,
            config_manager=config_manager,
        )

        self.event_loop = self._loop

        # Plex-specific state
        self._plex: PlexApiServer | None = None  # Server connection (stateless HTTP)
        self._plex_client: PlexClient | None = (
            None  # Player client for sending commands
        )
        self._supported_features = PLEX_FEATURES
        self._play_state = None
        self._key = None
        self._view_offset = None
        self._players: list[Any, Any] = None
        self._session: MediaContainer = None
        self._is_on: bool | None = False

        self._connect_error = False
        self._volume = 0
        self._is_volume_muted = False
        self._media_image_url = ""
        self._image_cache = None
        self._image_cache_url = None

        self._properties = {}
        self._background_tasks: set[asyncio.Task] = set()

        _LOG.debug("Plex instance created: %s", device.identifier)

    # ─────────────────────────────────────────────────────────────────
    # ExternalClientDevice abstract method implementations
    # ─────────────────────────────────────────────────────────────────

    async def create_client(self) -> PlexWebsocket:
        """
        Create the PlexWebsocket client instance.

        This also establishes the HTTP connection to the Plex server.
        """
        # First, establish connection to Plex server (stateless HTTP API)
        self._plex = self._get_plex_server()

        if not self._plex:
            raise ConnectionError(f"Failed to connect to Plex server at {self.address}")

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
        # Start listening (this runs in background)
        self._create_task(self._client.listen())

        # Wait for websocket to connect with timeout
        try:
            await asyncio.wait_for(self._wait_for_websocket_connection(), timeout=5.0)
        except asyncio.TimeoutError:
            _LOG.warning("Websocket connection timeout for %s", self.identifier)
            # Don't raise - the watchdog will handle reconnection if needed

        # Get initial session state
        await self._update_session_state()

        _LOG.debug("Websocket state: %s", self._client.state)

    async def disconnect_client(self) -> None:
        """
        Disconnect the PlexWebsocket client.
        """
        # Cancel all background tasks first
        if self._background_tasks:
            for task in self._background_tasks:
                task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Close websocket connection (not async)
        if self._client:
            try:
                self._client.close()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _LOG.warning(
                    "Exception occurred while closing PlexWebsocket client: %s",
                    exc,
                    exc_info=True,
                )

        # Clear player client reference
        if self._plex_client:
            self._plex_client = None

        # Reset state
        self._reset_state()

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

    async def _wait_for_websocket_connection(self):
        """Wait for websocket to connect."""
        while self._client and self._client.state != STATE_CONNECTED:
            await asyncio.sleep(0.1)

    async def _update_session_state(self):
        """Update session state asynchronously."""
        self._session = await self.event_loop.run_in_executor(
            None, self.get_session_by_client_id, self.device_config.identifier
        )
        if self._session:
            self._plex_client = self.get_plex_client()
            self._is_on = self._plex_client is not None
        else:
            self._is_on = False

    def _get_plex_server(self) -> PlexApiServer | None:
        """Get a reference to the PMS (stateless HTTP connection)."""
        config = self._device_config

        # Ensure address has http:// scheme
        address = config.address
        if not address.startswith("http://") and not address.startswith("https://"):
            address = f"http://{address}"

        url = f"{address}:{config.port}"
        try:
            if config.auth_token:
                return PlexApiServer(baseurl=url, token=config.auth_token, timeout=5)
            else:
                account = MyPlexAccount(config.username, config.password)
                return account.resource(config.server_name).connect()
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error("Cannot connect to %s over HTTP [%s]", url, ex)
            return None

    def get_state(self) -> MediaStates:
        """Get state of device."""
        if self._play_state == "paused":
            return MediaStates.PAUSED
        if self._play_state == "playing":
            return MediaStates.PLAYING
        if self._play_state == "stopped":
            return MediaStates.OFF
        if self.is_on:
            return MediaStates.ON
        if self._no_active_players:
            return MediaStates.OFF
        return MediaStates.OFF

    def _reset_state(self, players=None):
        self._players = players
        self._properties = {}
        # Clear image cache to free memory
        self._image_cache = None

    def _plex_ws_updates(self, msgtype, data, error) -> None:
        """Handle WS Messages from PlexWebsocket."""
        updated_data = {}
        payload = None

        if msgtype == "playing" or msgtype == "progress":
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
                            self._play_state = payload["state"]

                            if payload["state"] == "stopped":
                                self._image_cache = None
                                self._is_on = False
                                updated_data[MediaAttr.STATE] = self.get_state()
                            else:
                                self._is_on = True
                                updated_data[MediaAttr.STATE] = self.get_state()
                                self._view_offset = payload["viewOffset"] / 1000
                                updated_data[MediaAttr.MEDIA_POSITION] = (
                                    self._view_offset
                                )
                                updated_data[MediaAttr.MEDIA_POSITION_UPDATED_AT] = (
                                    datetime.now(tz=UTC).isoformat()
                                )

                                # Fetch full session details asynchronously
                                self._create_task(
                                    self._fetch_session_details(
                                        payload, self.identifier
                                    )
                                )

        if updated_data:
            self.events.emit(DeviceEvents.UPDATE, self.identifier, updated_data)

        if error:
            _LOG.debug(error)

    async def _fetch_session_details(self, payload: dict, identifier: str):
        """Fetch full session details asynchronously without blocking websocket."""
        try:
            # Run blocking call in executor
            session = await self.event_loop.run_in_executor(
                None, self.get_session_by_client_id, self.device_config.identifier
            )

            if not session:
                return

            self._session = session
            self._key = payload["key"]

            if session.TYPE == "audio":
                media_type = MediaType.MUSIC
            elif session.TYPE == "episode":
                media_type = MediaType.TVSHOW
            elif session.TYPE == "video":
                media_type = MediaType.VIDEO
            else:
                media_type = ""

            updated_data = {
                MediaAttr.MEDIA_DURATION: session.duration / 1000,
                MediaAttr.MEDIA_TYPE: media_type,
                MediaAttr.MEDIA_TITLE: session.title,
            }

            if session.type == "episode":
                updated_data[MediaAttr.MEDIA_ARTIST] = session.seasonEpisode.upper()

            # Get artwork URL
            url = self._get_artwork_url(session)

            # Fetch image asynchronously
            self._create_task(self._fetch_and_update_image(url, identifier))

            # Send immediate update with available data
            self.events.emit(DeviceEvents.UPDATE, identifier, updated_data)

        except Exception as ex:
            _LOG.error("Failed to fetch session details: %s", ex)

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
        except Exception:
            if session.type == "episode":
                return self.build_plex_url(session.grandparentThumb)
            else:
                return session.posterUrl

    async def _fetch_and_update_image(self, url: str, identifier: str):
        """Fetch image asynchronously and emit update event."""
        try:
            image_data = await self.store_image_as_base64(url, 400)
            if image_data:
                self._media_image_url = image_data
                # Emit update with the new artwork
                self.events.emit(
                    DeviceEvents.UPDATE,
                    identifier,
                    {MediaAttr.MEDIA_IMAGE_URL: image_data},
                )
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

                            byte_image = io.BytesIO()
                            resized_image.save(byte_image, format="PNG")
                            byte_image = byte_image.getvalue()

                            image_b64 = base64.b64encode(byte_image).decode("utf-8")
                            self._image_cache = f"data:image/png;base64,{image_b64}"
                            self._image_cache_url = url
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.error("Failed to fetch image from %s: %s", url, ex)
                return ""
        return self._image_cache

    def build_plex_url(self, path: str) -> str:
        """Build a plex url from config and supplied path"""
        config = self._device_config
        # Ensure address has http:// scheme
        address = config.address
        if not address.startswith("http://") and not address.startswith("https://"):
            address = f"http://{address}"
        return f"{address}:{config.port}{path}?X-Plex-Token={config.auth_token}"

    def get_players(self) -> list[Any, Any]:
        """Get active players from session."""
        self._players = None
        if self._plex:
            for session in self._plex.sessions():
                for player in session.players:
                    self._players.append(player)
        return self._players

    def get_session_by_client_id(self, identifier) -> MediaContainer | None:
        """Get session by client identifier."""
        if not self._plex:
            self.get_players()
        for session in self._plex.sessions():
            for player in session.players:
                if player.machineIdentifier == identifier and player.local is True:
                    return session
        return None

    def get_plex_client(self) -> PlexClient | None:
        """Get client from session for sending playback commands."""
        if self._session:
            try:
                self._plex_client = self._session.player
                self._plex_client.proxyThroughServer(True, self._plex)

                return self._plex_client
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.error(
                    "Unable to connect to client (%s) %s",
                    self._session.player.device,
                    ex,
                )
        updated_data = {}
        updated_data[MediaAttr.STATE] = self.get_state()
        self.events.emit(DeviceEvents.UPDATE, self.identifier, updated_data)
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
        if self._plex is None:
            return False
        if self._play_state in ["playing", "paused", "seeking", "buffering"]:
            self._is_on = True
        return self._is_on

    @property
    def play_state(self) -> str | None:
        """Return the play state of the device."""
        if self._play_state is None:
            return None
        return self._play_state

    @property
    def device_config(self) -> PlexDevice:
        """Return device configuration."""
        return self._device_config

    @property
    def host(self) -> str:
        """Return the host of the device as string."""
        return self._device_config.identifier

    @property
    def _no_active_players(self):
        """Returns True if no active players."""
        return not self._players

    @property
    def state(self) -> MediaStates:
        """Return the cached state of the device."""
        return self.get_state()

    @property
    def supported_features(self) -> list[Features]:
        """Return supported features."""
        return self._supported_features

    @property
    def is_volume_muted(self) -> bool:
        """Return boolean if volume is currently muted."""
        return self._is_volume_muted

    @property
    def volume_level(self) -> float | None:
        """Volume level of the media player (0..100)."""
        return self._volume

    @property
    def media_image_url(self) -> str:
        """Image url of current playing media."""
        return self._media_image_url

    @property
    def client(self) -> PlexClient:
        """Return Plex Client for sending playback commands."""
        if not self._plex_client:
            self._plex_client = self.get_plex_client()
        return self._plex_client


def print_info(msgtype, data, error):
    """Print info."""
    if msgtype == SIGNAL_CONNECTION_STATE:
        _LOG.debug("State: %s / Error: %s", data, error)
    else:
        _LOG.debug("Data: %s", data)
