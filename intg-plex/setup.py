"""
Setup flow for Plex integration.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any
from urllib.parse import urlparse

from const import PlexConfig
from packaging.version import Version
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from ucapi import (
    IntegrationSetupError,
    RequestUserInput,
    SetupAction,
    SetupError,
    UserDataResponse,
)
from ucapi_framework import BaseSetupFlow, MigrationData, EntityMigrationMapping

_LOG = logging.getLogger(__name__)


class PlexSetupFlow(BaseSetupFlow[PlexConfig]):
    """Plex integration setup flow handler."""

    def __init__(self, config_manager, *, driver, discovery=None, device_class=None):
        """Initialize setup flow with state tracking."""
        super().__init__(
            config_manager,
            discovery=discovery,
            driver=driver,
            device_class=device_class,
        )
        self._available_clients = []  # Store client list across screens

    def get_manual_entry_form(self) -> RequestUserInput:
        """Get the manual entry form for Plex server connection."""
        return RequestUserInput(
            {"en": "Connection Details"},
            [
                {
                    "id": "info",
                    "label": {
                        "en": "There are two options for establishing a connection to your Plex server. Please fill out one",
                    },
                    "field": {
                        "label": {
                            "value": {
                                "en": "Local Direct",
                            }
                        }
                    },
                },
                {
                    "field": {"text": {"value": ""}},
                    "id": "address",
                    "label": {
                        "en": "Host Address",
                        "de": "IP-Adresse",
                        "fr": "Adresse IP",
                    },
                },
                {
                    "field": {"text": {"value": ""}},
                    "id": "port",
                    "label": {"en": "HTTP port", "fr": "Port HTTP"},
                },
                {
                    "field": {"text": {"value": ""}},
                    "id": "auth_token",
                    "label": {"en": "Auth Token"},
                },
                {
                    "id": "info2",
                    "label": {
                        "en": "Internet Required",
                    },
                    "field": {
                        "label": {
                            "value": {
                                "en": "MyPlex",
                            }
                        }
                    },
                },
                {
                    "field": {"text": {"value": ""}},
                    "id": "server",
                    "label": {"en": "Plex Server Name"},
                },
                {
                    "field": {"text": {"value": ""}},
                    "id": "username",
                    "label": {"en": "Username", "fr": "Utilisateur"},
                },
                {
                    "field": {"text": {"value": ""}},
                    "id": "password",
                    "label": {"en": "Password", "fr": "Mot de passe"},
                },
            ],
        )

    async def query_device(
        self, input_values: dict[str, Any]
    ) -> RequestUserInput | SetupError:
        """
        Validate server connection and return a form for client selection.

        This method is called after the user fills in server connection details.
        It connects to the Plex server and returns a screen listing available clients for selection.
        """
        address = input_values.get("address", "")
        port = input_values.get("port", "32400")
        username = input_values.get("username", "")
        password = input_values.get("password", "")
        auth_token = input_values.get("auth_token", "")
        server_name = input_values.get("server", "")

        if address:
            address = validate_url(address)

        try:
            server = get_server(
                server_name=server_name,
                username=username,
                password=password,
                auth_token=auth_token,
                url=address,
                port=port,
            )

            if not auth_token:
                address = server._baseurl

            # Store server config for later use
            self._pending_device_config = {  # ty:ignore[invalid-assignment]
                "address": address,
                "port": port,
                "username": username,
                "password": password,
                "auth_token": auth_token,
                "server_name": server_name if server_name else "",
            }

            # Get list of active clients
            self._available_clients = []

            for session in server.sessions():
                for player in session.players:
                    if (
                        player.machineIdentifier
                        not in [d.identifier for d in self.config.all()]
                        and player.local is True
                    ):
                        self._available_clients.append(
                            {
                                "id": player.machineIdentifier,
                                "label": {"en": f"{player.title} ({player.product})"},
                                "title": player.title,
                                "product": player.product,
                            }
                        )

            if not self._available_clients:
                # No clients found, show message and ask to try again
                return RequestUserInput(
                    {"en": "No Active Sessions"},
                    [
                        {
                            "id": "info",
                            "label": {
                                "en": "No active Plex clients found. Please start playing something on a client and try again.",
                            },
                            "field": {
                                "label": {
                                    "value": {
                                        "en": "Make sure the client is actively playing media and on the same network.",
                                    }
                                }
                            },
                        },
                        {
                            "id": "retry",
                            "label": {"en": "Try Again"},
                            "field": {"checkbox": {"value": True}},
                        },
                    ],
                )

            # Return client selection screen
            return RequestUserInput(
                {"en": "Select Plex Client"},
                [
                    {
                        "id": "info",
                        "label": {
                            "en": "Client Selection",
                        },
                        "field": {
                            "label": {
                                "value": {
                                    "en": "Please select the Plex Client you would like to control.",
                                }
                            }
                        },
                    },
                    {
                        "id": "player",
                        "label": {"en": "Available Players"},
                        "field": {
                            "dropdown": {
                                "value": self._available_clients[0]["id"],
                                "items": [
                                    {"id": c["id"], "label": c["label"]}
                                    for c in self._available_clients
                                ],
                            }
                        },
                    },
                ],
            )

        except Exception as ex:
            _LOG.error("Cannot connect to server %s: %s", address, ex)
            return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)

    async def get_additional_configuration_screen(
        self, device_config: PlexConfig, previous_input: dict[str, Any]
    ) -> RequestUserInput | None:
        """
        Show artwork selection screen after client is selected.

        This is called after the client selection is made.
        """
        dropdown_tv_settings = [
            {"id": "tv-poster-series", "label": {"en": "Series Poster"}},
            {"id": "tv-poster-season", "label": {"en": "Season Poster"}},
            {"id": "tv-poster-episode", "label": {"en": "Episode Poster"}},
            {"id": "tv-poster-art", "label": {"en": "Series Background Art"}},
        ]

        dropdown_movie_settings = [
            {"id": "movie-poster", "label": {"en": "Movie Poster"}},
            {"id": "movie-art", "label": {"en": "Movie Background Art"}},
        ]

        dropdown_page_size = [
            {"id": "10",  "label": {"en": "10 items per page"}},
            {"id": "20",  "label": {"en": "20 items per page"}},
            {"id": "50",  "label": {"en": "50 items per page"}},
            {"id": "100", "label": {"en": "100 items per page"}},
        ]

        dropdown_sort_order = [
            {"id": "titleSort:asc",  "label": {"en": "Title (A → Z)"}},
            {"id": "titleSort:desc", "label": {"en": "Title (Z → A)"}},
            {"id": "year:desc",      "label": {"en": "Year (newest first)"}},
            {"id": "addedAt:desc",   "label": {"en": "Date Added (newest first)"}},
            {"id": "rating:desc",    "label": {"en": "Rating (highest first)"}},
        ]

        # Current values (if editing an existing device)
        current_page_size = str(getattr(device_config, "page_size", 20) or 20)
        current_sort_order = getattr(device_config, "sort_order", "titleSort:asc") or "titleSort:asc"

        return RequestUserInput(
            {"en": "Artwork & Browse Settings"},
            [
                {
                    "id": "details",
                    "label": {"en": "Artwork Settings"},
                    "field": {
                        "label": {
                            "value": {
                                "en": "Choose which artwork to display for TV shows and movies.",
                            }
                        }
                    },
                },
                {
                    "id": "tv_selection",
                    "label": {"en": "TV Shows"},
                    "field": {
                        "dropdown": {
                            "value": dropdown_tv_settings[0]["id"],
                            "items": dropdown_tv_settings,
                        }
                    },
                },
                {
                    "id": "movie_selection",
                    "label": {"en": "Movies"},
                    "field": {
                        "dropdown": {
                            "value": dropdown_movie_settings[0]["id"],
                            "items": dropdown_movie_settings,
                        }
                    },
                },
                {
                    "id": "browse_settings",
                    "label": {"en": "Media Browse Settings"},
                    "field": {
                        "label": {
                            "value": {
                                "en": "Configure pagination and sort order for media browsing.",
                            }
                        }
                    },
                },
                {
                    "id": "page_size",
                    "label": {"en": "Items per Page"},
                    "field": {
                        "dropdown": {
                            "value": current_page_size,
                            "items": dropdown_page_size,
                        }
                    },
                },
                {
                    "id": "sort_order",
                    "label": {"en": "Sort Order"},
                    "field": {
                        "dropdown": {
                            "value": current_sort_order,
                            "items": dropdown_sort_order,
                        }
                    },
                },
            ],
        )

    async def handle_additional_configuration_response(
        self, msg: UserDataResponse
    ) -> SetupAction | RequestUserInput | PlexConfig:
        """
        Handle the artwork selection response and complete setup.

        This is called after user selects artwork preferences.
        """
        # Check if user clicked retry on "no clients" screen
        if "retry" in msg.input_values and "player" not in msg.input_values:
            # Return to manual entry to re-scan for clients
            # Pass the server config stored in _pending_device_config
            return await self.query_device(self._pending_device_config)  # ty:ignore[invalid-argument-type]

        # Check if we're selecting a client (pending device is still a dict, not PlexDevice)
        if "player" in msg.input_values and isinstance(
            self._pending_device_config, dict
        ):
            # User selected a client, now we need to create device and show artwork screen
            machine_identifier = msg.input_values["player"]

            # Find the selected client
            selected_client = None
            for client in self._available_clients:
                if client["id"] == machine_identifier:
                    selected_client = client
                    break

            if not selected_client:
                _LOG.error("Selected client not found: %s", machine_identifier)
                return SetupError(error_type=IntegrationSetupError.OTHER)

            name = f"{selected_client.get('title', 'Unknown')} ({selected_client.get('product', 'Unknown')})"

            # Create device config with server details
            device = PlexConfig(
                identifier=machine_identifier,
                name=name,
                address=self._pending_device_config["address"],  # ty:ignore[invalid-argument-type]
                port=self._pending_device_config["port"],  # ty:ignore[invalid-argument-type]
                username=self._pending_device_config["username"],  # ty:ignore[invalid-argument-type]
                password=self._pending_device_config["password"],  # ty:ignore[invalid-argument-type]
                auth_token=self._pending_device_config["auth_token"],  # ty:ignore[invalid-argument-type]
                server_name=self._pending_device_config["server_name"],  # ty:ignore[invalid-argument-type]
                tv_selection="",  # Will be set in next screen
                movie_selection="",
            )

            # Store as pending and show artwork screen
            self._pending_device_config = device
            return await self.get_additional_configuration_screen(
                device, msg.input_values
            )  # ty:ignore[invalid-return-type]

        # User completed artwork + browse settings selection
        tv_selection = msg.input_values.get("tv_selection", "tv-poster-series")
        movie_selection = msg.input_values.get("movie_selection", "movie-poster")
        page_size = int(msg.input_values.get("page_size", 20))
        sort_order = msg.input_values.get("sort_order", "titleSort:asc")

        # Update the pending device with artwork + browse selections
        return PlexConfig(
            identifier=self._pending_device_config.identifier,
            name=self._pending_device_config.name,
            address=self._pending_device_config.address,
            port=self._pending_device_config.port,
            username=self._pending_device_config.username,
            password=self._pending_device_config.password,
            auth_token=self._pending_device_config.auth_token,
            server_name=self._pending_device_config.server_name,
            tv_selection=tv_selection,
            movie_selection=movie_selection,
            page_size=page_size,
            sort_order=sort_order,
        )

    async def is_migration_required(self, previous_version: str) -> bool:
        """
        Check if migration is required for existing configuration.

        :param previous_version: Previous version of the integration
        :return: True if migration is required, False otherwise
        """
        # Migrations required for versions 1.0.2 and below
        if Version(previous_version) <= Version("1.0.2"):
            return True
        return False

    async def get_migration_data(
        self, previous_version: str, current_version: str
    ) -> MigrationData:
        """Generate entity ID mappings for migration.

        Returns:
            MigrationData with driver IDs and entity mappings
        """

        mappings: list[EntityMigrationMapping] = []

        # Iterate through all configured devices
        for device in self.config.all():
            mappings.append(
                {
                    "previous_entity_id": f"{device.identifier}",
                    "new_entity_id": f"media_player.{device.identifier}",
                }
            )

        return {
            "previous_driver_id": "plex_driver",
            "new_driver_id": "plex_driver",
            "entity_mappings": mappings,
        }


def get_server(
    server_name, username, password, auth_token, url="", port=""
) -> PlexServer | SetupError:
    """Get plex server."""
    address = f"{url}:{port}"
    try:
        if auth_token:
            server = PlexServer(baseurl=address, token=auth_token, timeout=5)
        else:
            account = MyPlexAccount(username, password)
            server: PlexServer = account.resource(server_name).connect()
        _LOG.debug("Connection %s succeeded over HTTP", server._baseurl)
    except Exception as ex:
        _LOG.warning("Cannot connect to %s over HTTP [%s]", address, ex)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)

    return server


def validate_url(uri):
    """Validate passed in URL and attempts to correct api endpoint if path isn't supplied."""
    if not uri.startswith("http://") and not uri.startswith("https://"):
        uri = f"http://{uri}"
    parsed_url = urlparse(uri)
    if parsed_url.scheme == "":
        uri = "http://" + uri
    return uri
