"""
Media-player entity functions.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from const import PLEX_SIMPLE_COMMANDS, PlexConfig
from plex import PlexServer
from ucapi import StatusCodes, media_player
from ucapi.media_player import Commands, DeviceClasses, Options
from ucapi_framework import create_entity_id
from ucapi_framework.entities import MediaPlayerEntity

_LOG = logging.getLogger(__name__)


class PlexMediaPlayer(MediaPlayerEntity):
    """Representation of a Plex Media Player entity."""

    def __init__(self, config_device: PlexConfig, device: PlexServer):
        """Initialize the class."""
        self._device: PlexServer = device
        _LOG.debug("PlexMediaPlayer init")

        entity_id = create_entity_id(
            media_player.EntityTypes.MEDIA_PLAYER, config_device.identifier
        )
        features = device.supported_features

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
            cmd_handler=self.command_handler,
        )

        # Subscribe to device push updates — sync_state() is called on every push_update()
        self.subscribe_to_device(device)

    async def sync_state(self) -> None:
        """
        Sync entity state from device.

        Called automatically when the device calls push_update() or on reconnect.
        Reads fresh state from the device and pushes changes to the Remote.
        """
        attrs = self._device.get_media_player_attributes()
        self.update(attrs)

    async def command_handler(
        self,
        _entity: MediaPlayerEntity,
        cmd_id: str,
        params: dict[str, Any] | None,
        _: Any | None = None,
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

        if client is None:
            _LOG.warning("No Plex client available for entity: %s", self.id)
            return StatusCodes.SERVICE_UNAVAILABLE

        try:
            if cmd_id == Commands.VOLUME:
                if params:
                    volume = params.get("volume")
                    client.setVolume(volume)
                    self.set_muted(False)
                    self.set_volume(volume, update=True)
            elif cmd_id == Commands.PLAY_PAUSE or cmd_id == Commands.CURSOR_ENTER:
                if self._device.play_state == "playing":
                    client.pause()
                elif self._device.play_state == "paused":
                    client.play()
            elif cmd_id == Commands.MUTE:
                client.setVolume(0)
                self.set_muted(True, update=True)
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
                if params:
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
        except Exception as ex:  # pylint: disable=broad-exception-caught
            _LOG.info(
                f"Client does not support the {cmd_id} command. Additional Info: %s", ex
            )
            return StatusCodes.OK
