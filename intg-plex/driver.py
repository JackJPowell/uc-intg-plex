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
import remote
import setup
import ucapi
from config import device_from_entity_id

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
    _LOG.debug("R2 connect command: connecting device(s)")
    for device in _configured_clients.values():
        try:
            await _LOOP.create_task(connect_device(device))
        except RuntimeError as ex:
            _LOG.debug(
                "Could not connect to device %s : %s", device.device_config.id, ex
            )
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured Plex Clients when the Remote Two sends the disconnect command."""
    if len(api._clients) == 0:
        _LOG.debug("Disconnect requested")
        for device in _configured_clients.values():
            await _LOOP.create_task(device.disconnect())
    else:
        _LOG.debug("Disconnect requested but 1 client is connected %s", api._clients)


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_r2_enter_standby() -> None:
    """
    Enter standby notification from Remote Two.

    Disconnect every Plex instances.
    """
    global _R2_IN_STANDBY

    _R2_IN_STANDBY = True
    _LOG.debug("Enter standby event: disconnecting device(s)")
    for configured in _configured_clients.values():
        await configured.disconnect()


async def connect_device(device: plex.PlexDevice):
    """Connect device and send state."""
    try:
        _LOG.debug("Connecting device %s...", device.id)
        await device.connect()
        _LOG.debug(
            "Device %s connected, sending attributes for subscribed entities", device.id
        )
        state = plex.PLEX_STATE_MAPPING.get(device.state)
        for entity in api.configured_entities.get_all():
            entity_id = entity.get("entity_id", "")
            device_id = device_from_entity_id(entity_id)
            if device_id != device.id:
                continue
            if entity["entity_type"] == "media_player":
                _LOG.debug("Sending attributes %s : %s", entity_id, device.attributes)
                api.configured_entities.update_attributes(entity_id, device.attributes)
            if entity["entity_type"] == remote.PlexRemote:
                api.configured_entities.update_attributes(
                    entity_id,
                    {
                        ucapi.remote.Attributes.STATE: remote.PLEX_REMOTE_STATE_MAPPING.get(
                            state
                        )
                    },
                )
    except RuntimeError as ex:
        _LOG.error("Error while reconnecting to Plex %s", ex)


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_r2_exit_standby() -> None:
    """
    Exit standby notification from Remote Two.

    Connect all Plex instances.
    """
    global _R2_IN_STANDBY

    _R2_IN_STANDBY = False
    _LOG.debug("Exit standby event: connecting Plex device(s) %s", _configured_clients)

    for configured in _configured_clients.values():
        # start background task
        try:
            await _LOOP.create_task(connect_device(configured))
        except RuntimeError as ex:
            _LOG.error("Error while reconnecting to Plex %s", ex)
        # _LOOP.create_task(configured.connect())


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """
    Subscribe to given entities.

    :param entity_ids: entity identifiers.
    """
    global _R2_IN_STANDBY

    _R2_IN_STANDBY = False
    _LOG.debug("Subscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        entity = api.configured_entities.get(entity_id)
        device_id = device_from_entity_id(entity_id)
        if device_id in _configured_clients:
            device = _configured_clients[device_id]
            state = plex.PLEX_STATE_MAPPING.get(device.state)
            if isinstance(entity, media_player.PlexMediaPlayer):
                api.configured_entities.update_attributes(
                    entity_id, {ucapi.media_player.Attributes.STATE: state}
                )
            if isinstance(entity, remote.PlexRemote):
                api.configured_entities.update_attributes(
                    entity_id,
                    {
                        ucapi.remote.Attributes.STATE: remote.PLEX_REMOTE_STATE_MAPPING.get(
                            state
                        )
                    },
                )
            continue

        device = config.devices.get(device_id)
        if device:
            await _configure_new_device(device, connect=True)
            _LOOP.create_task(device.connect())
        else:
            _LOG.error(
                "Failed to subscribe entity %s: no Plex configuration found", entity_id
            )


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    _LOG.debug("Unsubscribe entities event: %s", entity_ids)
    devices_to_remove = set()
    for entity_id in entity_ids:
        device_id = device_from_entity_id(entity_id)
        if device_id is None:
            continue
        devices_to_remove.add(device_id)

    # Keep devices that are used by other configured entities not in this list
    for entity in api.configured_entities.get_all():
        entity_id = entity.get("entity_id", "")
        if entity_id in entity_ids:
            continue
        device_id = device_from_entity_id(entity_id)
        if device_id is None:
            continue
        if device_id in devices_to_remove:
            devices_to_remove.remove(device_id)

    for device_id in devices_to_remove:
        if device_id in _configured_clients:
            await _configured_clients[device_id].disconnect()
            _configured_clients[device_id].events.remove_all_listeners()


async def on_device_connected(device_id: str):
    """Handle device connection."""
    _LOG.debug("Plex connected: %s", device_id)

    if device_id not in _configured_clients:
        _LOG.warning("Plex %s is not configured", device_id)
        return

    await api.set_device_state(ucapi.DeviceStates.CONNECTED)

    for entity_id in _entities_from_device_id(device_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            continue

        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            if (
                configured_entity.attributes[ucapi.media_player.Attributes.STATE]
                == ucapi.media_player.States.UNAVAILABLE
            ):
                api.configured_entities.update_attributes(
                    entity_id,
                    {
                        ucapi.media_player.Attributes.STATE: ucapi.media_player.States.STANDBY
                    },
                )
        elif configured_entity.entity_type == ucapi.EntityTypes.REMOTE:
            if (
                configured_entity.attributes[ucapi.remote.Attributes.STATE]
                == ucapi.remote.States.UNAVAILABLE
            ):
                api.configured_entities.update_attributes(
                    entity_id, {ucapi.remote.Attributes.STATE: ucapi.remote.States.OFF}
                )


async def on_device_disconnected(device_id: str):
    """Handle device disconnection."""
    _LOG.debug("Plex disconnected: %s", device_id)

    for entity_id in _entities_from_device_id(device_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            continue

        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            api.configured_entities.update_attributes(
                entity_id,
                {
                    ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE
                },
            )
        elif configured_entity.entity_type == ucapi.EntityTypes.REMOTE:
            api.configured_entities.update_attributes(
                entity_id,
                {ucapi.remote.Attributes.STATE: ucapi.remote.States.UNAVAILABLE},
            )

    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


async def on_device_connection_error(device_id: str, message):
    """Set entities of Plex to state UNAVAILABLE if device connection error occurred."""
    _LOG.error(message)

    for entity_id in _entities_from_device_id(device_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            continue

        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            api.configured_entities.update_attributes(
                entity_id,
                {
                    ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE
                },
            )
        elif configured_entity.entity_type == ucapi.EntityTypes.REMOTE:
            api.configured_entities.update_attributes(
                entity_id,
                {ucapi.remote.Attributes.STATE: ucapi.remote.States.UNAVAILABLE},
            )

    await api.set_device_state(ucapi.DeviceStates.ERROR)


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
    if update is None:
        if device_id not in _configured_clients:
            return
        device = _configured_clients[device_id]
        update = device.attributes
    else:
        _LOG.info("[%s] Plex update: %s", device_id, update)

    attributes = None

    _LOG.info(
        "Update device %s for configured devices %s", device_id, api.configured_entities
    )
    for entity_id in _entities_from_device_id(device_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            return

        if isinstance(configured_entity, media_player.PlexMediaPlayer):
            attributes = update
        elif isinstance(configured_entity, remote.PlexRemote):
            attributes = configured_entity.filter_changed_attributes(update)

        if attributes:
            api.configured_entities.update_attributes(entity_id, attributes)


def _entities_from_device_id(device_id: str) -> list[str]:
    """
    Return all associated entity identifiers of the given device.

    :param device_id: the device identifier
    :return: list of entity identifiers
    """
    return [f"media_player.{device_id}"]
    #, f"remote.{device_id}"


def _configure_new_device(
    device_config: config.PlexConfigDevice, connect: bool = True
) -> None:
    """
    Create and configure a new device.

    Supported entities of the device are created and registered in the integration library as available entities.

    :param device: the devices configuration.
    :param connect: True: start connection to client.
    """
    # # the device should not yet be configured, but better be safe
    if device_config.id in _configured_clients:
        device = _configured_clients[device_config.id]
        device.disconnect()
    else:
        device = plex.PlexDevice(device_config, loop=_LOOP)

        # on_device_connected(device.id)
        device.events.on(plex.Events.CONNECTED, on_device_connected)
        device.events.on(plex.Events.DISCONNECTED, on_device_disconnected)
        device.events.on(plex.Events.ERROR, on_device_connection_error)
        device.events.on(plex.Events.UPDATE, on_device_update)
        _configured_clients[device.id] = device

    _register_available_entities(device_config, device)

    if connect:
        try:
            _LOOP.create_task(device.connect())
        except RuntimeError as ex:
            _LOG.debug(
                "Could not connect to device %s",
                ex,
            )


def _register_available_entities(
    device_config: config.PlexConfigDevice, device: plex.PlexDevice
) -> None:
    """
    Create entities for given device and register them as available entities.

    :param device_config: Receiver
    """
    entities = [
        media_player.PlexMediaPlayer(device_config, device),
        #remote.PlexRemote(device_config, device),
    ]
    for entity in entities:
        if api.available_entities.contains(entity.id):
            api.available_entities.remove(entity.id)
        api.available_entities.add(entity)


def on_device_added(device: config.PlexConfigDevice) -> None:
    """Handle a newly added device in the configuration."""
    _LOG.debug("New device added: %s", device)
    _configure_new_device(device, connect=False)


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
        if device.id in _configured_clients:
            _LOG.debug("Disconnecting from removed Plex %s", device.id)
            configured = _configured_clients.pop(device.id)
            _LOOP.create_task(_async_remove(configured))
            for entity_id in _entities_from_device_id(configured.id):
                api.configured_entities.remove(entity_id)
                api.available_entities.remove(entity_id)


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
    #logging.getLogger("remote").setLevel(level)
    logging.getLogger("plex").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)
    logging.getLogger("config").setLevel(level)

    config.devices = config.Devices(
        api.config_dir_path, on_device_added, on_device_removed
    )
    for device_config in config.devices.all():
        _configure_new_device(device_config, connect=False)

    await api.init("driver.json", setup.driver_setup_handler)


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
