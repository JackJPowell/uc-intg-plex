"""
This module implements Plex communication of the Remote Two integration driver.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
import io
import logging
import os
from asyncio import AbstractEventLoop, Future, Lock, shield
from enum import IntEnum
from io import BytesIO
from typing import Any, ParamSpec, TypeVar
from urllib.request import urlopen

from config import PlexConfigDevice
from const import PLEX_FEATURES
from PIL import Image
from plexapi.base import MediaContainer
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexClient, PlexServer
from plexwebsocket import SIGNAL_CONNECTION_STATE, STATE_CONNECTED, PlexWebsocket
from pyee.asyncio import AsyncIOEventEmitter
from ucapi.media_player import Features
from ucapi.media_player import States as MediaStates

_PlexDeviceT = TypeVar("_PlexDeviceT", bound="PlexDevice")
_P = ParamSpec("_P")

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
WEBSOCKET_WATCHDOG_INTERVAL = 10
CONNECTION_RETRIES = 10
CACHE_DIR = "cache"  # Cache directory for fallback images
PLEX_LOGO_FILENAME = "plex_logo.png"  # Specific logo file name


class Events(IntEnum):
    """Internal driver events."""

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    ERROR = 3
    UPDATE = 4


class States(IntEnum):
    """State of a connected AVR."""

    UNKNOWN = 0
    UNAVAILABLE = 1
    OFF = 2
    ON = 3
    PLAYING = 4
    PAUSED = 5
    STOPPED = 6
    BUFFERING = 7
    SEEKING = 8
    IDLE = 9


PLEX_STATE_MAPPING = {
    States.OFF: MediaStates.OFF,
    States.ON: MediaStates.ON,
    States.STOPPED: MediaStates.STANDBY,
    States.PLAYING: MediaStates.PLAYING,
    States.PAUSED: MediaStates.PAUSED,
    States.IDLE: MediaStates.ON,
    States.UNAVAILABLE: MediaStates.STANDBY,
}


class PlexDevice:
    """Representing a Plex Device."""

    def __init__(
        self,
        device_config: PlexConfigDevice,
        loop: AbstractEventLoop | None = None,
    ):
        """Create instance with Plex client."""
        self._device = device_config
        self.identifier: str = device_config.identifier
        self._name: str = device_config.name
        self.event_loop = loop or asyncio.get_running_loop()
        self.events = AsyncIOEventEmitter(self.event_loop)

        self._plex_connection: PlexWebsocket | None = None
        self._plex: PlexServer | None = None
        self._supported_features = PLEX_FEATURES
        self._play_state = None
        self._key = None
        self._view_offset = None
        self._players: list[Any, Any] = None
        self._session: MediaContainer = None
        self._client: PlexClient = None
        self._is_on: bool | None = False

        self._connect_error = False
        self._available: bool = True
        self._volume = 0
        self._is_volume_muted = False
        self._media_image_url = ""
        self._image_cache = None
        self._plex_logo_cache = None  # Cache for Plex logo

        self._websocket_task = None
        self._connect_lock = Lock()
        self._reconnect_retry = 0
        self._properties = {}

        # Initialize cache directory and logo
        self._cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), CACHE_DIR)
        self._plex_logo_path = os.path.join(self._cache_dir, PLEX_LOGO_FILENAME)
        self._ensure_cache_directory()
        self._load_plex_logo()

        _LOG.debug("Plex instance created: %s", device_config.identifier)
        self.event_loop.create_task(self.init_connection())

        self._connection_status: Future | None = None

    def _ensure_cache_directory(self):
        """Ensure the cache directory exists."""
        try:
            if not os.path.exists(self._cache_dir):
                os.makedirs(self._cache_dir)
                _LOG.debug("Created cache directory: %s", self._cache_dir)
        except OSError as ex:
            _LOG.warning("Could not create cache directory %s: %s", self._cache_dir, ex)

    def _load_plex_logo(self):
        """Load the Plex logo image and cache it as base64."""
        try:
            # Add extensive debugging
            _LOG.debug("=== PLEX LOGO DEBUG START ===")
            _LOG.debug("Current working directory: %s", os.getcwd())
            _LOG.debug("__file__ path: %s", __file__)
            _LOG.debug("dirname(__file__): %s", os.path.dirname(__file__))
            _LOG.debug("dirname(dirname(__file__)): %s", os.path.dirname(os.path.dirname(__file__)))
            _LOG.debug("CACHE_DIR constant: %s", CACHE_DIR)
            _LOG.debug("PLEX_LOGO_FILENAME constant: %s", PLEX_LOGO_FILENAME)
            _LOG.debug("Calculated cache dir: %s", self._cache_dir)
            _LOG.debug("Calculated plex logo path: %s", self._plex_logo_path)
            _LOG.debug("Cache dir exists: %s", os.path.exists(self._cache_dir))
            _LOG.debug("Plex logo file exists: %s", os.path.exists(self._plex_logo_path))
            
            # List contents of various directories
            try:
                _LOG.debug("Contents of current working directory (%s):", os.getcwd())
                for item in os.listdir(os.getcwd()):
                    _LOG.debug("  %s", item)
            except Exception as ex:
                _LOG.debug("Could not list cwd: %s", ex)
                
            # Check if cache dir exists and list its contents
            try:
                if os.path.exists(self._cache_dir):
                    _LOG.debug("Contents of cache directory (%s):", self._cache_dir)
                    for item in os.listdir(self._cache_dir):
                        _LOG.debug("  %s", item)
                else:
                    _LOG.debug("Cache directory does not exist: %s", self._cache_dir)
            except Exception as ex:
                _LOG.debug("Could not list cache dir: %s", ex)
            
            # Try alternative paths
            alt_paths = [
                os.path.join(os.getcwd(), CACHE_DIR, PLEX_LOGO_FILENAME),
                os.path.join(os.path.dirname(__file__), "..", CACHE_DIR, PLEX_LOGO_FILENAME),
                os.path.join(os.path.dirname(__file__), CACHE_DIR, PLEX_LOGO_FILENAME),
                os.path.join(".", CACHE_DIR, PLEX_LOGO_FILENAME),
                PLEX_LOGO_FILENAME,  # Just filename in case it's in root
                os.path.join(CACHE_DIR, PLEX_LOGO_FILENAME)  # Relative path
            ]
            
            _LOG.debug("Trying alternative paths:")
            for i, alt_path in enumerate(alt_paths):
                abs_path = os.path.abspath(alt_path)
                exists = os.path.exists(alt_path)
                _LOG.debug("Path %d: %s -> %s (exists: %s)", i+1, alt_path, abs_path, exists)
                if exists:
                    _LOG.debug("*** FOUND LOGO AT: %s ***", alt_path)
                    self._plex_logo_path = alt_path
                    break
            
            _LOG.debug("=== PLEX LOGO DEBUG END ===")

            if not os.path.exists(self._plex_logo_path):
                _LOG.debug("Plex logo not found at final path: %s", self._plex_logo_path)
                return

            with open(self._plex_logo_path, 'rb') as f:
                image_data = f.read()

            image = Image.open(BytesIO(image_data))
            
            # Resize if needed (same logic as store_image_as_base64)
            width, height = image.size
            max_size = 400
            
            if max_size < max(width, height):
                if width > height:
                    new_width = max_size
                    new_height = int(height * (max_size / width))
                else:
                    new_height = max_size
                    new_width = int(width * (max_size / height))

                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

            # Convert to base64
            byte_image = io.BytesIO()
            image.save(byte_image, format="PNG")
            byte_image = byte_image.getvalue()

            logo_image = base64.b64encode(byte_image).decode("utf-8")
            self._plex_logo_cache = f"data:image/png;base64,{logo_image}"
            
            _LOG.debug("Successfully loaded Plex logo from: %s", self._plex_logo_path)
            
        except Exception as ex:
            _LOG.warning("Could not load Plex logo from %s: %s", self._plex_logo_path, ex)
            self._plex_logo_cache = None

    def _get_plex_logo(self) -> str | None:
        """Get the Plex logo image as base64."""
        return self._plex_logo_cache

    async def init_connection(self):
        """Initialize connection to device."""
        if self._plex_connection:
            try:
                await self._plex_connection.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            finally:
                self._plex_connection = None

        self.events.emit(Events.CONNECTING, self.identifier)
        self._plex = self.get_plex_server()

        self._plex_connection: PlexWebsocket = PlexWebsocket(
            plex_server=self._plex,
            callback=self.plex_ws_updates,
            subscriptions=["playing", "status", "progress"],
        )
        self.event_loop.create_task(self._plex_connection.listen())
        self._plex_connection.state = STATE_CONNECTED
        self._session = self.get_session_by_client_id(self.device_config.identifier)
        if self._session:
            self._client = self.get_plex_client()
            if self._client:
                self._is_on = True
            else:
                self.is_on = False
        else:
            self._is_on = False

        _LOG.debug(self._plex_connection.state)

    def get_plex_server(self) -> PlexServer:
        """Get a reference to the PMS."""
        config = self._device

        url = f"{config.address}:{config.port}"
        try:
            if config.auth_token:
                self._plex = PlexServer(baseurl=url, token=config.auth_token, timeout=5)
            else:
                account = MyPlexAccount(config.username, config.password)
                self._plex: PlexServer = account.resource(config.server_name).connect()
            _LOG.debug("Connection %s succeeded over HTTP", url)
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error("Cannot connect to %s over HTTP [%s]", url, ex)

        return self._plex

    def get_state(self) -> States:
        """Get state of device."""
        if self._play_state == "paused":
            return States.PAUSED
        if self._play_state == "playing":
            return States.PLAYING
        if self._play_state == "stopped":
            return States.OFF
        if self.is_on:
            return States.ON
        if self._no_active_players:
            return States.IDLE
        return States.OFF

    async def _clear_connection(self, close=True):
        self._reset_state()
        if close:
            try:
                await self._plex_connection.close()
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    async def _reconnect_websocket_if_disconnected(self, *_) -> bool:
        """Reconnect the websocket if it fails."""
        if (
            not self._plex_connection.state == STATE_CONNECTED
            and self._reconnect_retry >= CONNECTION_RETRIES
        ):
            return False
        if not self._plex_connection.state == STATE_CONNECTED:
            self._reconnect_retry += 1
            _LOG.debug(
                "Plex websocket %s not connected, retry %s / %s",
                self._device.identifier,
                self._reconnect_retry,
                CONNECTION_RETRIES,
            )
            # Connection status result has to be reset if connection fails and future result is still okay
            if not self._connection_status or self._connection_status.done():
                self._connection_status = self.event_loop.create_future()
            try:
                await asyncio.wait_for(shield(self.connect()), DEFAULT_TIMEOUT * 2)
            except asyncio.TimeoutError:
                _LOG.debug(
                    "Plex websocket too slow to reconnect on %s",
                    self._device.identifier,
                )
        else:
            if self._reconnect_retry > 0:
                self._reconnect_retry = 0
                _LOG.debug("Plex websocket is connected")
        return True

    async def start_watchdog(self):
        """Start websocket watchdog."""
        while True:
            await asyncio.sleep(WEBSOCKET_WATCHDOG_INTERVAL)
            try:
                if not await self._reconnect_websocket_if_disconnected():
                    _LOG.debug("Stop watchdog for %s", self._device.identifier)
                    self._websocket_task = None
                    break
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.error("Unknown exception %s", ex)

    async def connect(self) -> bool:
        """Connect to Plex via websocket protocol."""
        try:
            if self._connect_lock.locked():
                _LOG.debug(
                    "Connect to %s : already in progress, returns",
                    self._device.identifier,
                )
                return True
            _LOG.debug("Connecting to %s", self._device.identifier)
            await self._connect_lock.acquire()
            if self._plex_connection and self._plex_connection.state == STATE_CONNECTED:
                _LOG.debug("Already connected to %s", self._device.identifier)
                return True
            await self.init_connection()

            self._connect_error = False
            _LOG.debug("Connection successful to %s", self._device.identifier)
            self._reconnect_retry = 0
            if self._websocket_task is None:
                self._websocket_task = self.event_loop.create_task(
                    self.start_watchdog()
                )
            if self._connection_status and not self._connection_status.done():
                self._connection_status.set_result(True)

            return True

        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.error(
                "Unknown exception connect to %s : %s", self._device.identifier, ex
            )
        finally:
            # After 10 retries, reconnection delay will go from 10 to 30s and stop logging
            if self._reconnect_retry >= CONNECTION_RETRIES and self._connect_error:
                _LOG.debug(
                    "Plex websocket not connected, abort retries to %s",
                    self._device.identifier,
                )
                if self._websocket_task:
                    try:
                        self._websocket_task.cancel()
                    except Exception as ex:  # pylint: disable=broad-exception-caught
                        _LOG.error("Failed to cancel websocket task %s", ex)
                    self._websocket_task = None
            elif self._websocket_task is None:
                self._websocket_task = self.event_loop.create_task(
                    self.start_watchdog()
                )
            self._available = True
            self.events.emit(Events.CONNECTED, self.identifier)
            self._connect_lock.release()

    async def disconnect(self):
        """Disconnect from Plex Websocket."""
        _LOG.debug("Disconnect %s", self.identifier)
        try:
            if self._websocket_task:
                self._websocket_task.cancel()
            if self._plex_connection:
                self._plex_connection.close()
        except Exception as error:  # pylint: disable=broad-exception-caught
            _LOG.error(
                "Logout to %s failed: [%s]",
                self._device.identifier,
                error,
            )
            # self._available = False
        finally:
            self._websocket_task = None

    def _reset_state(self, players=None):
        self._players = players
        self._properties = {}

    def plex_ws_updates(self, msgtype, data, error) -> None:
        """Handle WS Messages."""
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

                        if payload and payload["state"] == "stopped":
                            self._image_cache = None
                            self._is_on = False
                            self._play_state = payload["state"]
                            updated_data["state"] = self.get_state()
                            
                            # FIXED: Set Plex logo when nothing is playing
                            plex_logo = self._get_plex_logo()
                            if plex_logo:
                                self._media_image_url = plex_logo  # Set the instance variable
                                updated_data["media_image_url"] = plex_logo  # Set in update data
                                _LOG.debug("Using Plex logo for idle state")
                            else:
                                self._media_image_url = ""
                                updated_data["media_image_url"] = ""
                                _LOG.debug("No Plex logo available, using empty image")
                                
                        elif payload:
                            self._is_on = True
                            self._play_state = payload["state"]
                            updated_data["state"] = self.get_state()

                            self._view_offset = payload["viewOffset"] / 1000
                            updated_data["position"] = self._view_offset

                            self._session = self.get_session_by_client_id(
                                self.device_config.identifier
                            )
                            if self._session:
                                self._key = payload["key"]

                                updated_data["total_time"] = (
                                    self._session.duration / 1000
                                )
                                updated_data["media_type"] = self._session.TYPE

                                # if self._media_title != self._session.title:
                                #     self._image_cache = None

                                if self._session.type == "episode":
                                    updated_data["artist"] = (
                                        self._session.seasonEpisode.upper()
                                    )
                                updated_data["title"] = self._session.title

                                url = ""
                                try:
                                    if self._session.type == "episode":
                                        match self._device.tv_selection:
                                            case "tv-poster-series":
                                                url = self.build_plex_url(
                                                    self._session.grandparentThumb
                                                )
                                            case "tv-poster-season":
                                                url = self.build_plex_url(
                                                    self._session.parentThumb
                                                )
                                            case "tv-poster-episode":
                                                url = self.build_plex_url(
                                                    self._session.thumb
                                                )
                                            case "tv-poster-art":
                                                url = self._session.artUrl
                                            case _:
                                                url = self.build_plex_url(
                                                    self._session.grandparentThumb
                                                )
                                    else:
                                        match self._device.movie_selection:
                                            case "movie-poster":
                                                url = self._session.posterUrl
                                            case "movie-art":
                                                url = self._session.artUrl
                                            case _:
                                                url = self._session.posterUrl
                                except Exception:  # pylint: disable=broad-exception-caught
                                    if self._session.type == "episode":
                                        url = self.build_plex_url(
                                            self._session.grandparentThumb
                                        )
                                    else:
                                        url = self._session.posterUrl

                                # FIXED: Use live API image when actively playing
                                self._media_image_url = self.store_image_as_base64(
                                    url, 400
                                )
                                updated_data["media_image_url"] = self._media_image_url  # Changed from "artwork"
                                _LOG.debug("Using live API image for active playback")

        # FIXED: Handle case when transitioning to idle/off state without active session
        if updated_data.get("state") in [States.OFF, States.IDLE] and "media_image_url" not in updated_data:
            plex_logo = self._get_plex_logo()
            if plex_logo:
                self._media_image_url = plex_logo
                updated_data["media_image_url"] = plex_logo
                _LOG.debug("Using Plex logo for idle/off state")

        if updated_data:
            self.events.emit(Events.UPDATE, self.identifier, updated_data)

        if error:
            _LOG.debug(error)

    def store_image_as_base64(self, url, max_size):
        """Retrieve and store image as base64 data."""
        if not self._image_cache:
            with urlopen(url) as url:
                f = url.read()
                image = Image.open(BytesIO(f))

                width, height = image.size

                if max_size >= max(width, height):
                    return image

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

                image = base64.b64encode(byte_image).decode("utf-8")
                self._image_cache = f"data:image/png;base64,{image}"
        return self._image_cache

    def build_plex_url(self, path: str) -> str:
        """Build a plex url from config and supplied path"""
        config = self._device
        return f"{config.address}:{config.port}{path}?X-Plex-Token={config.auth_token}"

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
        """Get client from session."""
        if self._session:
            try:
                self._client = self._session.player
                self._client.proxyThroughServer(True, self._plex)

                self.events.emit(Events.CONNECTED, self.identifier)
                return self._client
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOG.error(
                    "Unable to connect to client (%s) %s",
                    self._session.player.device,
                    ex,
                )
        updated_data = {}
        updated_data["state"] = self.get_state()
        self.events.emit(Events.UPDATE, self.identifier, updated_data)
        return None

    # def command_button(self, button: ButtonKeymap):
    #     """Call a button command."""
    #     self._client.sendCommand(button.get("keymap"))

    # def command_action(self, command: str):
    #     """Send custom command."""
    #     self._client.sendCommand(command)

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        """Return True if device is available."""
        return self._available

    @available.setter
    def available(self, value: bool):
        """Set device availability and emit CONNECTED / DISCONNECTED event on change."""
        if self._available != value:
            self._available = value
            self.events.emit(
                Events.CONNECTED if value else Events.DISCONNECTED, self.identifier
            )

    @property
    def is_on(self) -> bool | None:
        """Whether the Apple TV is on or off. Returns None if not connected."""
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
    def device_config(self) -> PlexConfigDevice:
        """Return device configuration."""
        return self._device

    @property
    def host(self) -> str:
        """Return the host of the device as string."""
        return self._device.identifier

    @property
    def _no_active_players(self):
        """Returns players."""
        return not self._players

    @property
    def state(self) -> States:
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
        """Return Plex Client."""
        if not self._client:
            self._client = self.get_plex_client()
        return self._client


def print_info(msgtype, data, error):
    """Print info."""
    if msgtype == SIGNAL_CONNECTION_STATE:
        _LOG.debug("State: %s / Error: %s", data, error)
    else:
        _LOG.debug("Data: %s", data)