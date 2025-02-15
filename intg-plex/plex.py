"""
This module implements Plex communication of the Remote Two integration driver.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from asyncio import AbstractEventLoop, Future, Lock, shield
from enum import IntEnum
from typing import Any, ParamSpec, TypeVar

from aiohttp import ClientSession
from config import PlexConfigDevice
from const import PLEX_FEATURES
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
}


class PlexDevice:
    """Representing a Plex Device."""

    def __init__(
        self,
        device_config: PlexConfigDevice,
        loop: AbstractEventLoop | None = None,
    ):
        """Create instance with Plex client"""
        self._device_config = device_config
        self.id: str = device_config.id
        self._name: str = device_config.name
        self.event_loop = loop or asyncio.get_running_loop()
        self.events = AsyncIOEventEmitter(self.event_loop)
        self._http_session: ClientSession | None = None

        self._plex_connection: PlexWebsocket | None = None
        self._plex: PlexServer | None = None
        self._supported_features = PLEX_FEATURES
        self._play_state = None
        self._key = None
        self._view_offset = None
        self._attr_state = States.OFF
        self._players: list[Any, Any] = None
        self._session: MediaContainer = None
        self._client: PlexClient = None

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

        self._websocket_task = None
        self._connect_lock = Lock()
        self._reconnect_retry = 0

        _LOG.debug("Plex instance created: %s", device_config.id)
        self.event_loop.create_task(self.init_connection())

        self._connection_status: Future | None = None
        self._previous_state = States.OFF

    async def init_connection(self):
        """Initialize connection to device."""
        if self._plex_connection:
            try:
                await self._plex_connection.close()
            except Exception:
                pass
            finally:
                self._plex_connection = None
        if self._http_session:
            try:
                await self._http_session.close()
            except Exception as ex:
                _LOG.warning(
                    "Error closing session to %s : %s", self._device_config.id, ex
                )
            self._session = None

        self._http_session = ClientSession(raise_for_status=True)
        self._http_session.loop.set_exception_handler(self.exception_handler)
        self._plex = self.get_plex_server()

        self._plex_connection: PlexWebsocket = PlexWebsocket(
            plex_server=self._plex,
            callback=self.plex_ws_updates,
            subscriptions=["playing", "status", "progress"],
        )
        self.event_loop.create_task(self._plex_connection.listen())
        self._plex_connection.state = STATE_CONNECTED
        self._session = self.get_session_by_client_id(self.device_config.id)
        if self._session:
            self._client = self.get_plex_client()

        _LOG.debug(self._plex_connection.state)

    def get_plex_server(self) -> PlexServer:
        config = self._device_config

        url = f"{config.address}:{config.port}"
        try:
            
            if config.auth_token:
                self._plex = PlexServer(baseurl=url, token=config.auth_token, timeout=5)
            else:
                account = MyPlexAccount(config.username, config.password)
                self._plex: PlexServer = account.resource(config.server_name).connect()
            _LOG.debug("Connection %s succeeded over HTTP", url)
        except Exception as ex:
            _LOG.error("Cannot connect to %s over HTTP [%s]", url, ex)

        return self._plex

    def get_state(self) -> States:
        """Get state of device."""
        if self._plex_is_off:
            return States.OFF
        if self._no_active_players:
            return States.IDLE
        if self._play_state == 'paused':
            return States.PAUSED
        return States.PLAYING

    async def _clear_connection(self, close=True):
        self._reset_state()
        if close:
            try:
                await self._plex_connection.close()
            except Exception:
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
                self._device_config.id,
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
                    "Plex websocket too slow to reconnect on %s", self._device_config.id
                )
        else:
            if self._reconnect_retry > 0:
                self._reconnect_retry = 0
                _LOG.debug("Plex websocket is connected")
        return True

    def exception_handler(self, loop, context):
        """Handle exception for running loop."""
        if not context or context.get("exception", None) is None:
            return
        exception = context.get("exception", None)
        message = context.get("message", None)
        if message is None:
            message = ""
        # log exception
        _LOG.error(
            f"Websocket task failed to %s, msg={message}, exception={exception}",
            self._device_config.id,
        )

    async def start_watchdog(self):
        """Start websocket watchdog."""
        while True:
            await asyncio.sleep(WEBSOCKET_WATCHDOG_INTERVAL)
            try:
                if not await self._reconnect_websocket_if_disconnected():
                    _LOG.debug("Stop watchdog for %s", self._device_config.id)
                    self._websocket_task = None
                    break
            except Exception as ex:
                _LOG.error("Unknown exception %s", ex)

    async def connect(self) -> bool:
        """Connect to Plex via websocket protocol."""
        try:
            if self._connect_lock.locked():
                _LOG.debug(
                    "Connect to %s : already in progress, returns",
                    self._device_config.id,
                )
                return True
            _LOG.debug("Connecting to %s", self._device_config.id)
            await self._connect_lock.acquire()
            if self._plex_connection and self._plex_connection.state == STATE_CONNECTED:
                _LOG.debug("Already connected to %s", self._device_config.id)
                return True
            await self.init_connection()

            self._connect_error = False
            _LOG.debug("Connection successful to %s", self._device_config.id)
            self._reconnect_retry = 0
            if self._websocket_task is None:
                self._websocket_task = self.event_loop.create_task(
                    self.start_watchdog()
                )
            if self._connection_status and not self._connection_status.done():
                self._connection_status.set_result(True)
            return True

        except Exception as ex:
            _LOG.error(
                "Unknown exception connect to %s : %s", self._device_config.id, ex
            )
        finally:
            # After 10 retries, reconnection delay will go from 10 to 30s and stop logging
            if self._reconnect_retry >= CONNECTION_RETRIES and self._connect_error:
                _LOG.debug(
                    "Plex websocket not connected, abort retries to %s",
                    self._device_config.id,
                )
                if self._websocket_task:
                    try:
                        self._websocket_task.cancel()
                    except Exception as ex:
                        _LOG.error("Failed to cancel websocket task %s", ex)
                    self._websocket_task = None
            elif self._websocket_task is None:
                self._websocket_task = self.event_loop.create_task(
                    self.start_watchdog()
                )
            self._available = True
            self.events.emit(Events.CONNECTED, self.id)
            self._connect_lock.release()

    async def disconnect(self):
        """Disconnect from Plex Websocket."""
        _LOG.debug("Disconnect %s", self.id)
        try:
            if self._websocket_task:
                self._websocket_task.cancel()
            if self._plex_connection:
                self._plex_connection.close()
            self._previous_state = self._attr_state
            self._attr_state = States.OFF
        except Exception as error:
            _LOG.error(
                "Logout to %s failed: [%s]",
                self._device_config.id,
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
        updated_data[MediaAttr.STATE] = States.OFF
        updated_data[MediaAttr.MEDIA_POSITION] = 0
        updated_data[MediaAttr.MEDIA_DURATION] = 0
        updated_data[MediaAttr.MEDIA_TITLE] = ""
        updated_data[MediaAttr.MEDIA_IMAGE_URL] = ""
        updated_data[MediaAttr.MEDIA_ALBUM] = ""
        updated_data[MediaAttr.MEDIA_ARTIST] = ""
        return updated_data

    def plex_ws_updates(self, msgtype, data, error) -> None:
        updated_data = {}
        payload = None

        if msgtype == "playing" or msgtype == "progress":
            match data["type"]:
                case "playing" | "paused":
                    if data["PlaySessionStateNotification"]:
                        for item in data["PlaySessionStateNotification"]:
                            if item["clientIdentifier"] == self.device_config.id:
                                payload = item
                                break

                        if payload and payload["state"] == "stopped":
                            updated_data = self._reset_media_state()
                        elif payload:
                            if self._play_state != payload["state"]:
                                self._play_state = payload["state"]
                                updated_data[MediaAttr.STATE] = self._play_state

                            if self._view_offset != payload["viewOffset"]:
                                self._view_offset = payload["viewOffset"] / 1000
                                updated_data[MediaAttr.MEDIA_POSITION] = (
                                    self._view_offset
                                )

                            self._session = self.get_session_by_client_id(
                                self.device_config.id
                            )
                            if self._session:
                                self._key = payload["key"]

                                self._media_duration = self._session.duration / 1000
                                updated_data[MediaAttr.MEDIA_DURATION] = (
                                    self._media_duration
                                )

                                self._media_title = self._session.title
                                if self._session.type == "episode":
                                    self._media_artist = (
                                        self._session.seasonEpisode.upper()
                                    )
                                    updated_data[MediaAttr.MEDIA_ARTIST] = (
                                        self._media_artist
                                    )
                                updated_data[MediaAttr.MEDIA_TITLE] = self._media_title

                                if self._session.type == "episode":
                                    self._media_image_url = self._session.artUrl
                                else:
                                    self._media_image_url = self._session.posterUrl
                                updated_data[MediaAttr.MEDIA_IMAGE_URL] = (
                                    self._media_image_url
                                )
        if msgtype == "plexwebsocket_state":
            match data:
                case "stopped":
                    updated_data = self._reset_media_state()

        if updated_data:
            self.events.emit(Events.UPDATE, self.id, updated_data)

    def get_players(self) -> list[Any, Any]:
        self._players = None
        if self._plex:
            for session in self._plex.sessions():
                for player in session.players:
                    self._players.append(player)
        return self._players

    def get_session_by_client_id(self, id) -> MediaContainer | None:
        if not self._plex:
            self.get_players()
        for session in self._plex.sessions():
            for player in session.players:
                if player.machineIdentifier == id and player.local is True:
                    return session
        return None

    def get_plex_client(self) -> PlexClient | None:
        if self._session:
            try:
                self._client = PlexClient(
                    server=self._plex,
                    baseurl=f"http://{self._session.player.address}:32500",
                    identifier=self._session.player.machineIdentifier,
                    token=self._plex.createToken(),
                )
                return self._client
            except Exception:
                pass
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
            MediaAttr.MEDIA_IMAGE_URL: self.media_image_url
            if self.media_image_url
            else "",
            MediaAttr.MEDIA_TITLE: self.media_title if self.media_title else "",
            MediaAttr.MEDIA_ALBUM: self.media_album if self.media_album else "",
            MediaAttr.MEDIA_ARTIST: self.media_artist if self.media_artist else "",
            MediaAttr.MEDIA_POSITION: self.media_position,
            MediaAttr.MEDIA_DURATION: self.media_duration,
        }
        return attributes

    @property
    def available(self) -> bool:
        """Return True if device is available."""
        return self._available

    @available.setter
    def available(self, value: bool):
        """Set device availability and emit CONNECTED / DISCONNECTED event on change."""
        if self._available != value:
            self._available = value
            self.events.emit(Events.CONNECTED if value else Events.DISCONNECTED, self.id)

    @property
    def device_config(self) -> PlexConfigDevice:
        """Return device configuration."""
        return self._device_config

    @property
    def host(self) -> str:
        """Return the host of the device as string."""
        return self._device_config.id

    @property
    def _plex_is_off(self):
        return self._players is None

    @property
    def _no_active_players(self):
        return not self._players

    @property
    def state(self) -> States:
        """Return the cached state of the device."""
        return self._attr_state

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
        """Return Plex Client"""
        if not self._client:
            self._client = self.get_plex_client()
        return self._client


def print_info(msgtype, data, error):
    if msgtype == SIGNAL_CONNECTION_STATE:
        _LOG.debug(f"State: {data} / Error: {error}")
    else:
        _LOG.debug(f"Data: {data}")
