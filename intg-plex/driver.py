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


async def main():
    """Start the Remote integration driver."""
    logging.basicConfig()

    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    logging.getLogger("plex").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    driver = BaseIntegrationDriver(
        device_class=PlexServer,
        entity_classes=[PlexMediaPlayer],
        driver_id="plex_driver",
    )

    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=PlexConfig,
    )

    await driver.register_all_device_instances()

    setup_handler = PlexSetupFlow.create_handler(driver)
    await driver.api.init("driver.json", setup_handler)

    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
