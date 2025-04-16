"""
Media-player entity functions.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import plex
from config import PlexConfigDevice
from const import PLEX_SIMPLE_COMMANDS
from ucapi import MediaPlayer, StatusCodes, media_player
from ucapi.media_player import Commands, DeviceClasses, Options

_LOG = logging.getLogger(__name__)


class PlexMediaPlayer(MediaPlayer):
    """Representation of a Plex Media Player entity."""

    def __init__(self, config_device: PlexConfigDevice, device: plex.PlexDevice):
        """Initialize the class."""
        self._device: plex.PlexDevice = device
        _LOG.debug("PlexMediaPlayer init")
        # entity_id = create_entity_id(config_device.identifier, EntityTypes.MEDIA_PLAYER)
        entity_id = config_device.identifier
        features = device.supported_features
        # attributes = device.attributes
        options = {Options.SIMPLE_COMMANDS: list(PLEX_SIMPLE_COMMANDS.keys())}
        super().__init__(
            entity_id,
            config_device.name,
            features,
            {
                media_player.Attributes.STATE: media_player.States.UNKNOWN,
                media_player.Attributes.VOLUME: 0,
                media_player.Attributes.MEDIA_DURATION: 0,
                media_player.Attributes.MEDIA_POSITION: 0,
                media_player.Attributes.MEDIA_IMAGE_URL: "",
                media_player.Attributes.MEDIA_TITLE: "",
                media_player.Attributes.MEDIA_ARTIST: "",
                media_player.Attributes.MEDIA_ALBUM: "",
            },
            device_class=DeviceClasses.TV,
            options=options,
            cmd_handler=self.command,
        )

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
        client = self._device.client

        try:
            if cmd_id == Commands.VOLUME:
                client.setVolume(params.get("volume"))
                self._device._is_volume_muted = False
                self._device._volume = params.get("volume")
            elif cmd_id == Commands.PLAY_PAUSE or cmd_id == Commands.CURSOR_ENTER:
                if self._device._play_state == "playing":
                    client.pause()
                elif self._device._play_state == "paused":
                    client.play()
            elif cmd_id == Commands.MUTE:
                client.setVolume(0)
                self._device._is_volume_muted = True
            elif cmd_id == Commands.STOP:
                client.stop()
            elif cmd_id in [Commands.NEXT, Commands.CURSOR_RIGHT]:
                client.moveRight()
            elif cmd_id in [Commands.PREVIOUS, Commands.CURSOR_LEFT]:
                client.stepBack()
            elif cmd_id == Commands.HOME:
                client.goToHome()
            elif cmd_id == Commands.FAST_FORWARD:
                client.skipNext()
            elif cmd_id == Commands.REWIND:
                client.skipPrevious()
            elif cmd_id == Commands.SEEK:
                media_position = params.get("media_position", 0)
                client.seekTo(media_position * 1000)
            elif cmd_id in [Commands.MENU, Commands.BACK]:
                client.goBack()
            elif cmd_id == Commands.CONTEXT_MENU:
                client.contextMenu()
            # elif cmd_id == Commands.CURSOR_ENTER:
            #     client.select()
            elif (
                cmd_id == Commands.FUNCTION_YELLOW
                or cmd_id == Commands.FUNCTION_GREEN
                or cmd_id == Commands.FUNCTION_BLUE
                or cmd_id == Commands.FUNCTION_RED
                or cmd_id == Commands.CHANNEL_DOWN
                or cmd_id == Commands.CHANNEL_UP
            ):
                return StatusCodes.OK
            else:
                return StatusCodes.NOT_IMPLEMENTED
            return StatusCodes.OK
        except Exception as ex:
            _LOG.info(
                f"Client does not support the {cmd_id} command. Additional Info: %s", ex
            )
            return StatusCodes.OK
