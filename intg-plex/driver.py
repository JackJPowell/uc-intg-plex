"""
This module implements a Remote Two integration driver for Plex.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os

from const import PlexConfig
from media_player import PlexMediaPlayer
from plex import PlexServer
from setup import PlexSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path


class PlexIntegrationDriver(BaseIntegrationDriver[PlexServer, PlexConfig]):
    """Plex Integration Driver"""

    def device_from_entity_id(self, entity_id: str) -> str | None:
        """
        Extract device identifier from entity identifier.

        For Plex, the entity_id IS the device identifier.

        :param entity_id: Entity identifier
        :return: Device identifier
        """
        return entity_id


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    logging.getLogger("plex").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    driver = PlexIntegrationDriver(
        device_class=PlexServer, entity_classes=[PlexMediaPlayer]
    )

    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=PlexConfig,
    )

    await driver.register_all_configured_devices()

    setup_handler = PlexSetupFlow.create_handler(driver)
    await driver.api.init("driver.json", setup_handler)

    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
