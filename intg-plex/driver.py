"""
This module implements a Remote Two integration driver for Plex.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
from typing import Any
import config
import media_player
import plex
import setup
import ucapi

_LOG = logging.getLogger("driver")
_LOOP = asyncio.get_event_loop()

# Global variables
api = ucapi.IntegrationAPI(_LOOP)
# Map of id -> device instance
_configured_clients: dict[str, plex.PlexDevice] = {}
_R2_IN_STANDBY = False


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured Plex Clients when the Remote Two sends the connect command."""
    _LOG.debug("Client connect command: connecting device(s)")
    await api.set_device_state(
        ucapi.DeviceStates.CONNECTED
    )  # just to make sure the device state is set
    for client in _configured_clients.values():
        # start background task
        await client.connect()


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured Plex Clients when the Remote Two sends the disconnect command."""
    _LOG.debug("Client disconnect command: disconnecting device(s)")
    for client in _configured_clients.values():
        await client.disconnect()


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_r2_enter_standby() -> None:
    """
    Enter standby notification from Remote Two.

    Disconnect every Plex instances.
    """
    _LOG.debug("Enter standby event: disconnecting device(s)")
    for client in _configured_clients.values():
        await client.disconnect()


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_r2_exit_standby() -> None:
    """
    Exit standby notification from Remote Two.

    Connect all Plex instances.
    """
    _LOG.debug("Exit standby event: connecting device(s)")
    for client in _configured_clients.values():
        await client.connect()


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """
    Subscribe to given entities.

    :param entity_ids: entity identifiers.
    """
    _LOG.debug("Subscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        if entity_id in _configured_clients:
            client = _configured_clients[entity_id]
            _LOG.info("Add '%s' to configured devices and connect", client.name)
            if client.is_on is None:
                state = ucapi.media_player.States.UNAVAILABLE
            else:
                state = (
                    ucapi.media_player.States.ON
                    if client.is_on
                    else ucapi.media_player.States.OFF
                )
            api.configured_entities.update_attributes(
                entity_id, {ucapi.media_player.Attributes.STATE: state}
            )
            await client.connect()
            continue

        device = config.devices.get(entity_id)
        if device:
            await _add_configured_client(device)
        else:
            _LOG.error(
                "Failed to subscribe entity %s: no Plex instance found", entity_id
            )


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    _LOG.debug("Unsubscribe entities event: %s", entity_ids)
    # TODO #11 add entity_id --> atv_id mapping. Right now the atv_id == entity_id!
    for entity_id in entity_ids:
        if entity_id in _configured_clients:
            device = _configured_clients.pop(entity_id)
            _LOG.info(
                "Removed '%s' from configured devices and disconnect", device.name
            )
            await device.disconnect()
            device.events.remove_all_listeners()


async def on_device_connected(device_id: str):
    """Handle device connection."""
    _LOG.debug("Plex connected: %s", device_id)
    state = ucapi.media_player.States.UNKNOWN
    if device_id in _configured_clients:
        atv = _configured_clients[device_id]
        if atv_state := atv.state:
            state = _plex_state_to_media_player_state(atv_state)

    api.configured_entities.update_attributes(
        device_id, {ucapi.media_player.Attributes.STATE: state}
    )
    await api.set_device_state(
        ucapi.DeviceStates.CONNECTED
    )  # just to make sure the device state is set


async def on_device_disconnected(device_id: str):
    """Handle device disconnection."""
    _LOG.debug("Plex disconnected: %s", device_id)

    api.configured_entities.update_attributes(
        device_id,
        {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE},
    )


async def on_device_connection_error(identifier: str, message) -> None:
    """Set entities of Plex client to state UNAVAILABLE if Plex connection error occurred."""
    _LOG.error(message)
    api.configured_entities.update_attributes(
        identifier,
        {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE},
    )
    await api.set_device_state(ucapi.DeviceStates.ERROR)


def _plex_state_to_media_player_state(device_state) -> ucapi.media_player.States:
    match device_state:
        case "on":
            state = ucapi.media_player.States.ON
        case "off":
            state = ucapi.media_player.States.OFF
        case "idle":
            state = ucapi.media_player.States.ON
        case "buffering":
            state = ucapi.media_player.States.BUFFERING
        case "paused":
            state = ucapi.media_player.States.PAUSED
        case "playing":
            state = ucapi.media_player.States.PLAYING
        case "seeking":
            state = ucapi.media_player.States.PLAYING
        case "stopped":
            state = ucapi.media_player.States.ON
        case _:
            state = ucapi.media_player.States.OFF
    return state


async def handle_device_address_change(device_id: str, address: str) -> None:
    """Update device configuration with changed IP address."""
    device = config.devices.get(device_id)
    if device and device.address != address:
        _LOG.info(
            "Updating IP address of configured Plex %s: %s -> %s",
            device_id,
            device.address,
            address,
        )
        device.address = address
        config.devices.update(device)


async def on_device_update(device_id: str, update: dict[str, Any] | None) -> None:
    """
    Update attributes of configured media-player entity if device properties changed.

    :param device_id: device identifier
    :param update: dictionary containing the updated properties or None if
    """
    attributes = {}

    # FIXME temporary workaround until ucapi has been refactored:
    #       there's shouldn't be separate lists for available and configured entities
    if api.configured_entities.contains(device_id):
        target_entity = api.configured_entities.get(device_id)
    else:
        target_entity = api.available_entities.get(device_id)
    if target_entity is None:
        return

    if "state" in update:
        state = _plex_state_to_media_player_state(update["state"])
        if (
            target_entity.attributes.get(ucapi.media_player.Attributes.STATE, None)
            != state
        ):
            attributes[ucapi.media_player.Attributes.STATE] = state

    # updates initiated by the poller always include the data, even if it hasn't changed
    if "position" in update:
        attributes[ucapi.media_player.Attributes.MEDIA_POSITION] = update["position"]
    if "total_time" in update:
        attributes[ucapi.media_player.Attributes.MEDIA_DURATION] = update["total_time"]
    if "artwork" in update:
        attributes[ucapi.media_player.Attributes.MEDIA_IMAGE_URL] = update["artwork"]
    if "title" in update:
        attributes[ucapi.media_player.Attributes.MEDIA_TITLE] = update["title"]
    if "artist" in update:
        attributes[ucapi.media_player.Attributes.MEDIA_ARTIST] = update["artist"]
    if "album" in update:
        attributes[ucapi.media_player.Attributes.MEDIA_ALBUM] = update["album"]

    if "media_type" in update:
        if update["media_type"] == "audio":
            media_type = ucapi.media_player.MediaType.MUSIC
        elif update["media_type"] == "episode":
            media_type = ucapi.media_player.MediaType.TVSHOW
        elif update["media_type"] == "video":
            media_type = ucapi.media_player.MediaType.VIDEO
        else:
            media_type = ""

        attributes[ucapi.media_player.Attributes.MEDIA_TYPE] = media_type

    if ucapi.media_player.Attributes.STATE in attributes:
        if (
            attributes[ucapi.media_player.Attributes.STATE]
            == ucapi.media_player.States.OFF
        ):
            attributes[ucapi.media_player.Attributes.MEDIA_IMAGE_URL] = ""
            attributes[ucapi.media_player.Attributes.MEDIA_ALBUM] = ""
            attributes[ucapi.media_player.Attributes.MEDIA_ARTIST] = ""
            attributes[ucapi.media_player.Attributes.MEDIA_TITLE] = ""
            attributes[ucapi.media_player.Attributes.MEDIA_TYPE] = ""
            attributes[ucapi.media_player.Attributes.SOURCE] = ""
            attributes[ucapi.media_player.Attributes.MEDIA_DURATION] = 0

    if attributes:
        if api.configured_entities.contains(device_id):
            api.configured_entities.update_attributes(device_id, attributes)
        else:
            api.available_entities.update_attributes(device_id, attributes)


def _entities_from_device_id(device_id: str) -> list[str]:
    """
    Return all associated entity identifiers of the given device.

    :param device_id: the device identifier
    :return: list of entity identifiers
    """
    return [device_id]


def _add_configured_client(
    device_config: config.PlexConfigDevice, connect: bool = True
) -> None:
    """
    Create and configure a new device.

    Supported entities of the device are created and registered in the integration library as available entities.

    :param device: the devices configuration.
    :param connect: True: start connection to client.
    """
    # # the device should not yet be configured, but better be safe
    if device_config.identifier in _configured_clients:
        device = _configured_clients[device_config.identifier]
        _LOOP.create_task(device.disconnect())
    else:
        _LOG.debug(
            "Adding new Plex device: %s (%s) %s",
            device_config.name,
            device_config.identifier,
            device_config.address if device_config.address else "",
        )
        device = plex.PlexDevice(device_config, loop=_LOOP)

        device.events.on(plex.Events.CONNECTED, on_device_connected)
        device.events.on(plex.Events.DISCONNECTED, on_device_disconnected)
        device.events.on(plex.Events.ERROR, on_device_connection_error)
        device.events.on(plex.Events.UPDATE, on_device_update)
        _configured_clients[device.identifier] = device

        async def start_connection():
            await device.connect()

        if connect:
            # start background task
            _LOOP.create_task(start_connection())

        _register_available_entities(device_config, device)


def _register_available_entities(
    device_config: config.PlexConfigDevice, device: plex.PlexDevice
) -> None:
    """
    Create entities for given device and register them as available entities.

    :param device_config: Receiver
    """
    entities = [
        media_player.PlexMediaPlayer(device_config, device),
    ]
    for entity in entities:
        if api.available_entities.contains(entity.id):
            api.available_entities.remove(entity.id)
        api.available_entities.add(entity)


def on_device_added(device: config.PlexConfigDevice) -> None:
    """Handle a newly added device in the configuration."""
    _LOG.debug("New device added: %s", device)
    _add_configured_client(device, connect=False)


def on_device_removed(device: config.PlexConfigDevice | None) -> None:
    """Handle a removed device in the configuration."""
    if device is None:
        _LOG.debug(
            "Configuration cleared, disconnecting & removing all configured Plex instances"
        )
        for configured in _configured_clients.values():
            _LOOP.create_task(_async_remove(configured))
        _configured_clients.clear()
        api.configured_entities.clear()
        api.available_entities.clear()
    else:
        if device.identifier in _configured_clients:
            _LOG.debug("Disconnecting from removed Plex %s", device.identifier)
            configured = _configured_clients.pop(device.identifier)
            _LOOP.create_task(_async_remove(configured))
            api.configured_entities.remove(device.identifier)
            api.available_entities.remove(device.identifier)


async def _async_remove(device: plex.PlexDevice) -> None:
    """Disconnect from receiver and remove all listeners."""
    await device.disconnect()
    device.events.remove_all_listeners()


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    # logging.getLogger("remote").setLevel(level)
    logging.getLogger("plex").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)
    logging.getLogger("config").setLevel(level)

    config.devices = config.Devices(
        api.config_dir_path, on_device_added, on_device_removed
    )
    for device_config in config.devices.all():
        _add_configured_client(device_config, connect=False)

    await api.init("driver.json", setup.driver_setup_handler)


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
