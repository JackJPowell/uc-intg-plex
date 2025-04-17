"""
This module implements Plex communication of the Remote Two integration driver.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import base64
import io
import logging
from datetime import UTC, datetime
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
from ucapi.media_player import Attributes as MediaAttr
from ucapi.media_player import Features, MediaType
from ucapi.media_player import States as MediaStates

_PlexDeviceT = TypeVar("_PlexDeviceT", bound="PlexDevice")
_P = ParamSpec("_P")

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
WEBSOCKET_WATCHDOG_INTERVAL = 10
CONNECTION_RETRIES = 10


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
    IDLE = 7


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
        self._attr_state = "OFF"
        self._previous_state = "OFF"
        self._players: list[Any, Any] = None
        self._session: MediaContainer = None
        self._client: PlexClient = None
        self._is_on: bool | None = False

        self._connect_error = False
        self._available: bool = True
        self._volume = 0
        self._is_volume_muted = False
        self._media_position = 0
        self._media_duration = 0
        self._media_type = MediaType.VIDEO
        self._media_title = ""
        self._media_image_url = ""
        self._media_artist = ""
        self._media_album = ""
        self._image_cache = None

        self._websocket_task = None
        self._connect_lock = Lock()
        self._reconnect_retry = 0
        self._properties = {}

        _LOG.debug("Plex instance created: %s", device_config.identifier)
        self.event_loop.create_task(self.init_connection())

        self._connection_status: Future | None = None

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
            self._attr_state = "ON"

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
        if self.plex_is_off:
            return States.OFF
        if self._no_active_players:
            return States.IDLE
        return States.ON

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
            self._previous_state = self._attr_state
            self._attr_state = States.OFF
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
        self._media_position = None

    def _reset_media_state(self) -> dict:
        updated_data = {}
        self._media_position = 0
        self._media_duration = 0
        self._media_title = ""
        self._media_album = ""
        self._media_artist = ""
        self._media_image_url = ""
        self._image_cache = None
        updated_data["state"] = "OFF"
        updated_data["position"] = 0
        updated_data["duration"] = 0
        updated_data["title"] = ""
        updated_data["artwork"] = ""
        updated_data["album"] = ""
        updated_data["artist"] = ""
        updated_data["media_type"] = ""
        updated_data["media_position_updated_at"] = 0
        return updated_data

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
                            updated_data = self._reset_media_state()
                            self._image_cache = None
                        elif payload:
                            self._play_state = payload["state"]
                            updated_data["state"] = self._play_state
                            self._is_on = True

                            self._view_offset = payload["viewOffset"] / 1000
                            updated_data["position"] = self._view_offset
                            updated_data["media_position_updated_at"] = datetime.now(tz=UTC).isoformat()

                            self._session = self.get_session_by_client_id(
                                self.device_config.identifier
                            )
                            if self._session:
                                self._key = payload["key"]

                                self._media_duration = self._session.duration / 1000
                                updated_data["total_time"] = self._media_duration

                                updated_data["media_type"] = self._session.TYPE

                                # if self._media_title != self._session.title:
                                #     self._image_cache = None

                                self._media_title = self._session.title
                                if self._session.type == "episode":
                                    self._media_artist = (
                                        self._session.seasonEpisode.upper()
                                    )
                                    updated_data["artist"] = self._media_artist
                                updated_data["title"] = self._media_title

                                url = ""
                                try:
                                    if self._session.type == "episode":
                                        match self._device.tv_selection:
                                            case "tv-poster-series":
                                                url = self.build_plex_url(self._session.grandparentThumb)
                                            case "tv-poster-season":
                                                url = self.build_plex_url(self._session.parentThumb)
                                            case "tv-poster-episode":
                                                url = self.build_plex_url(self._session.thumb)
                                            case "tv-poster-art":
                                                url = self._session.artUrl
                                            case _:
                                                url = self.build_plex_url(self._session.grandparentThumb)
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
                                        url = self.build_plex_url(self._session.grandparentThumb)
                                    else:
                                        url = self._session.posterUrl

                                self._media_image_url = self.store_image_as_base64(
                                    url, 400
                                )

                                updated_data["artwork"] = self._media_image_url

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
        return None

    # def command_button(self, button: ButtonKeymap):
    #     """Call a button command."""
    #     self._client.sendCommand(button.get("keymap"))

    # def command_action(self, command: str):
    #     """Send custom command."""
    #     self._client.sendCommand(command)

    @property
    def attributes(self) -> dict[str, any]:
        """Return the device attributes."""
        attributes = {
            MediaAttr.STATE: PLEX_STATE_MAPPING[self.get_state()],
            MediaAttr.MUTED: self.is_volume_muted,
            MediaAttr.MEDIA_TYPE: self.media_type,
            MediaAttr.MEDIA_IMAGE_URL: (
                self.media_image_url if self.media_image_url else ""
            ),
            MediaAttr.MEDIA_TITLE: self.media_title if self.media_title else "",
            MediaAttr.MEDIA_ALBUM: self.media_album if self.media_album else "",
            MediaAttr.MEDIA_ARTIST: self.media_artist if self.media_artist else "",
            MediaAttr.MEDIA_POSITION: self.media_position,
            MediaAttr.MEDIA_DURATION: self.media_duration,
        }
        return attributes

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
            return None
        if self._play_state:
            self._is_on = True
        return self._is_on

    @property
    def device_config(self) -> PlexConfigDevice:
        """Return device configuration."""
        return self._device

    @property
    def host(self) -> str:
        """Return the host of the device as string."""
        return self._device.identifier

    @property
    def plex_is_off(self):
        """Signals if plex client is on or off."""
        return self._client is None

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
    def media_position(self):
        """Return current media position."""
        return self._media_position

    @property
    def media_duration(self):
        """Return current media duration."""
        return self._media_duration

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
    def media_title(self) -> str:
        """Title of current playing media."""
        return self._media_title

    @property
    def media_album(self) -> str:
        """Title of current playing media."""
        return self._media_album

    @property
    def media_artist(self) -> str:
        """Title of current playing media."""
        return self._media_artist

    @property
    def media_type(self) -> MediaType:
        """Return current media type."""
        return self._media_type

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
