"""
Remote entity functions.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from typing import Any

import plex
from config import PlexConfigDevice, create_entity_id
from const import (
    PLEX_REMOTE_BUTTONS_MAPPING,
    PLEX_REMOTE_SIMPLE_COMMANDS,
    PLEX_REMOTE_UI_PAGES,
    key_update_helper,
)
from ucapi import EntityTypes, Remote, StatusCodes
from ucapi.media_player import Commands as MediaPlayerCommands
from ucapi.media_player import States as MediaStates
from ucapi.remote import Attributes, Features
from ucapi.remote import States as RemoteStates

_LOG = logging.getLogger(__name__)


PLEX_REMOTE_STATE_MAPPING = {
    MediaStates.OFF: RemoteStates.OFF,
    MediaStates.ON: RemoteStates.ON,
    MediaStates.STANDBY: RemoteStates.ON,
    MediaStates.PLAYING: RemoteStates.ON,
    MediaStates.PAUSED: RemoteStates.ON,
}


class PlexRemote(Remote):
    """Representation of a Plex Remote entity."""

    def __init__(self, config_device: PlexConfigDevice, device: plex.PlexDevice):
        """Initialize the class."""
        self._device: plex.PlexDevice = device
        _LOG.debug("PlexRemote init")
        entity_id = create_entity_id(config_device.id, EntityTypes.REMOTE)
        features = [Features.SEND_CMD, Features.ON_OFF]
        attributes = {
            Attributes.STATE: PLEX_REMOTE_STATE_MAPPING.get(
                plex.PLEX_STATE_MAPPING.get(device.state)
            ),
        }
        super().__init__(
            entity_id,
            config_device.name,
            features,
            attributes,
            simple_commands=PLEX_REMOTE_SIMPLE_COMMANDS,
            button_mapping=PLEX_REMOTE_BUTTONS_MAPPING,
            ui_pages=PLEX_REMOTE_UI_PAGES,
        )

    def get_int_param(self, param: str, params: dict[str, Any], default: int):
        """Get parameter in integer format."""
        value = params.get(param, default)
        if isinstance(value, str) and len(value) > 0:
            return int(float(value))
        return default

    async def command(
        self, cmd_id: str, params: dict[str, Any] | None = None
    ) -> StatusCodes:
        """
        Media-player entity command handler.

        Called by the integration-API if a command is sent to a configured media-player entity.

        :param cmd_id: command
        :param params: optional command parameters
        :return: status code of the command request
        """
        _LOG.info("Got %s command request: %s %s", self.id, cmd_id, params)

        if self._device is None:
            _LOG.warning("No Plex instance for entity: %s", self.id)
            return StatusCodes.SERVICE_UNAVAILABLE

        repeat = self.get_int_param("repeat", params, 1)

        for _i in range(0, repeat):
            await self.handle_command(cmd_id, params)
        return StatusCodes.OK

    async def handle_command(
        self, cmd_id: str, params: dict[str, Any] | None = None
    ) -> StatusCodes:
        """Handle command."""
        # hold = self.get_int_param("hold", params, 0)
        delay = self.get_int_param("delay", params, 0)
        command = params.get("command", "")

        client = self._device.client
        client.connect()

        try:
            if command == MediaPlayerCommands.VOLUME:
                client.setVolume(params.get("volume"))
            elif command == MediaPlayerCommands.PLAY_PAUSE:
                if self._device._play_state == "playing":
                    client.pause()
                elif self._device._play_state == "paused":
                    client.play()
            elif command == MediaPlayerCommands.MUTE:
                client.setVolume(0)
            elif command == MediaPlayerCommands.STOP:
                client.stop()
            elif (command in [MediaPlayerCommands.NEXT, MediaPlayerCommands.CURSOR_RIGHT]):
                client.stepForward()
            elif (
                command in [MediaPlayerCommands.PREVIOUS, MediaPlayerCommands.CURSOR_LEFT]):
                client.stepBack()
            elif command == MediaPlayerCommands.HOME:
                client.goToHome()
            elif command == MediaPlayerCommands.FAST_FORWARD:
                client.skipNext()
            elif command == MediaPlayerCommands.REWIND:
                client.skipPrevious()
            elif command == MediaPlayerCommands.SEEK:
                media_position = params.get("media_position", 0)
                client.seekTo(media_position * 1000)
            elif (
                command == MediaPlayerCommands.MENU
                or command == MediaPlayerCommands.BACK
            ):
                client.goBack()
            elif command == MediaPlayerCommands.CONTEXT_MENU:
                client.contextMenu()
            elif (
                command == MediaPlayerCommands.FUNCTION_YELLOW
                or command == MediaPlayerCommands.FUNCTION_GREEN
                or command == MediaPlayerCommands.FUNCTION_BLUE
                or command == MediaPlayerCommands.FUNCTION_RED
                or command == MediaPlayerCommands.CHANNEL_DOWN
                or command == MediaPlayerCommands.CHANNEL_UP
                or command == MediaPlayerCommands.CURSOR_ENTER
            ):
                return StatusCodes.OK
            else:
                return StatusCodes.NOT_IMPLEMENTED

            if delay > 0 and command != MediaPlayerCommands.SEND_CMD_SEQUENCE:
                await asyncio.sleep(delay)
            return StatusCodes.OK
        except Exception as ex:
            _LOG.info(
                f"Client does not support the {command} command. Additional Info: %s",
                ex,
            )
            return StatusCodes.OK

    def filter_changed_attributes(self, update: dict[str, Any]) -> dict[str, Any]:
        """
        Filter the given attributes and return only the changed values.

        :param update: dictionary with attributes.
        :return: filtered entity attributes containing changed attributes only.
        """
        attributes = {}

        if Attributes.STATE in update:
            state = PLEX_REMOTE_STATE_MAPPING.get(update[Attributes.STATE])
            attributes = key_update_helper(
                self.attributes, Attributes.STATE, state, attributes
            )

        _LOG.debug("Plex Remote update attributes %s -> %s", update, attributes)
        return attributes
