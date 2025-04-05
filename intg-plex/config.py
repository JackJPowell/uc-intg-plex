"""
Configuration handling of the integration driver.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from typing import Iterator

from ucapi import EntityTypes

_LOG = logging.getLogger(__name__)

_CFG_FILENAME = "config.json"


def create_entity_id(device_id: str, entity_type: EntityTypes) -> str:
    """Create a unique entity identifier for the given receiver and entity type."""
    return f"{entity_type.value}.{device_id}"


def device_from_entity_id(entity_id: str) -> str | None:
    """
    Return the id prefix of an entity_id.

    The prefix is the part before the first dot in the name and refers to the plex device identifier.

    :param entity_id: the entity identifier
    :return: the device prefix, or None if entity_id doesn't contain a dot
    """
    return entity_id.split(".", 1)[1]


@dataclass
class PlexConfigDevice:
    """Plex device configuration."""

    identifier: str
    name: str
    address: str
    username: str
    password: str
    auth_token: str
    server_name: str
    port: str
    tv_selection: str
    movie_selection: str


class _EnhancedJSONEncoder(json.JSONEncoder):
    """Python dataclass json encoder."""

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


class Devices:
    """Integration driver configuration class. Manages all configured Plex devices."""

    def __init__(self, data_path: str, add_handler, remove_handler):
        """
        Create a configuration instance for the given configuration path.

        :param data_path: configuration path for the configuration file and client device certificates.
        """
        self._data_path: str = data_path
        self._cfg_file_path: str = os.path.join(data_path, _CFG_FILENAME)
        self._config: list[PlexConfigDevice] = []
        self._add_handler = add_handler
        self._remove_handler = remove_handler

        self.load()

    @property
    def data_path(self) -> str:
        """Return the configuration path."""
        return self._data_path

    def all(self) -> Iterator[PlexConfigDevice]:
        """Get an iterator for all device configurations."""
        return iter(self._config)

    def contains(self, device_id: str) -> bool:
        """Check if there's a device with the given device identifier."""
        for item in self._config:
            if item.identifier == device_id:
                return True
        return False

    def add(self, plex_device: PlexConfigDevice) -> None:
        """Add a new configured Plex device."""
        existing = self.get_by_id(plex_device.identifier)
        if existing:
            _LOG.debug("Replacing existing device %s => %s", existing, plex_device)
            self._config.remove(existing)

        self._config.append(plex_device)
        if self._add_handler is not None:
            self._add_handler(plex_device)

    def get(self, device_id: str) -> PlexConfigDevice | None:
        """Get device configuration for given identifier."""
        for item in self._config:
            if item.identifier == device_id:
                return dataclasses.replace(item)
        return None

    def update(self, device: PlexConfigDevice) -> bool:
        """Update a configured Plex device and persist configuration."""
        for item in self._config:
            if item.identifier == device.identifier:
                item.name = device.name
                item.address = device.address
                item.port = device.port
                item.username = device.username
                item.password = device.password
                item.ssl = device.ssl
                item.tv_selection = device.tv_selection
                item.movie_selection = device.movie_selection
                return self.store()
        return False

    def remove(self, device_id: str) -> bool:
        """Remove the given device configuration."""
        device = self.get(device_id)
        if device is None:
            return False
        try:
            self._config.remove(device)
            if self._remove_handler is not None:
                self._remove_handler(device)
            return True
        except ValueError:
            pass
        return False

    def clear(self) -> None:
        """Remove the configuration file."""
        self._config = []

        if os.path.exists(self._cfg_file_path):
            os.remove(self._cfg_file_path)

        if self._remove_handler is not None:
            self._remove_handler(None)

    def store(self) -> bool:
        """
        Store the configuration file.

        :return: True if the configuration could be saved.
        """
        try:
            with open(self._cfg_file_path, "w+", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, cls=_EnhancedJSONEncoder)
            return True
        except OSError:
            _LOG.error("Cannot write the config file")

        return False

    def load(self) -> bool:
        """
        Load the config into the config global variable.

        :return: True if the configuration could be loaded.
        """
        try:
            with open(self._cfg_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                try:
                    self._config.append(PlexConfigDevice(**item))
                except TypeError as ex:
                    _LOG.warning("Invalid configuration entry will be ignored: %s", ex)
            return True
        except OSError:
            _LOG.error("Cannot open the config file")
        except ValueError:
            _LOG.error("Empty or invalid config file")

        return False

    def get_by_id(self, machine_id: str) -> PlexConfigDevice | None:
        """
        Get device configuration for a matching id or address.

        :return: A copy of the device configuration or None if not found.
        """
        for item in self._config:
            if item.identifier == machine_id:
                return dataclasses.replace(item)
        return None


devices: Devices | None = None
