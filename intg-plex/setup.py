"""
Setup flow for Plex integration.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
import re
from enum import IntEnum
from urllib.parse import urlparse

import config
from config import PlexConfigDevice
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from ucapi import (
    AbortDriverSetup,
    DriverSetupRequest,
    IntegrationSetupError,
    RequestUserInput,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
    UserDataResponse,
)

_LOG = logging.getLogger(__name__)


class SetupSteps(IntEnum):
    """Enumeration of setup steps to keep track of user data responses."""

    INIT = 0
    CONFIGURATION_MODE = 1
    DISCOVER = 2
    DEVICE_CHOICE = 3


_setup_step = SetupSteps.INIT
_cfg_add_device: bool = False
_base_url: str = ""

_user_input_manual = RequestUserInput(
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
            "label": {"en": "Host Address", "de": "IP-Adresse", "fr": "Adresse IP"},
        },
        {
            "field": {"text": {"value": ""}},
            "id": "port",
            "label": {"en": "HTTP port", "fr": "Port HTTP"},
        },
        {
            "field": {"text": {"value": ""}},
            "id": "token",
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


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:
    """
    Dispatch driver setup requests to corresponding handlers.

    Either start the setup process or handle the selected Plex Client.

    :param msg: the setup driver request object, either DriverSetupRequest or UserDataResponse
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device

    if isinstance(msg, DriverSetupRequest):
        _setup_step = SetupSteps.INIT
        _cfg_add_device = False
        return await handle_driver_setup(msg)

    if isinstance(msg, UserDataResponse):
        _LOG.debug(msg)
        if (
            _setup_step == SetupSteps.CONFIGURATION_MODE
            and "player" in msg.input_values
            and msg.input_values["player"] == "no-session"
        ):
            return await _handle_server_config(msg, use_existing_config=True)
        if (
            _setup_step == SetupSteps.CONFIGURATION_MODE
            and "player" in msg.input_values
        ):
            return await _handle_client_selection(msg)
        if (
            _setup_step == SetupSteps.CONFIGURATION_MODE
            and "action" in msg.input_values
            and msg.input_values["action"] == "remove"
        ):
            return await handle_configuration_mode(msg)
        if (
            _setup_step == SetupSteps.CONFIGURATION_MODE
            and "action" in msg.input_values
            and msg.input_values["action"] == "reset"
        ):
            return await handle_configuration_mode(msg)
        if (
            _setup_step == SetupSteps.CONFIGURATION_MODE
            and "choice" in msg.input_values
        ):
            return await _handle_server_config(msg, use_existing_config=True)
        if _setup_step == SetupSteps.CONFIGURATION_MODE:
            return await _handle_server_config(msg)
        _LOG.error(
            "No or invalid user response was received: %s (step %s)", msg, _setup_step
        )

    elif isinstance(msg, AbortDriverSetup):
        _LOG.info("Setup was aborted with code: %s", msg.error)
        _setup_step = SetupSteps.INIT

    return SetupError()


async def handle_driver_setup(msg: DriverSetupRequest) -> RequestUserInput | SetupError:
    """
    Start driver setup.

    Initiated by Remote Two to set up the driver.
    Ask user to enter ip-address for manual configuration, otherwise auto-discovery is used.

    :param msg: not used, we don't have any input fields in the first setup screen.
    :return: the setup action on how to continue
    """
    global _setup_step

    # get all configured devices for the user to choose from
    dropdown_devices = []
    for device in config.devices.all():
        dropdown_devices.append({"id": device.id, "label": {"en": f"{device.name}"}})

    reconfigure = msg.reconfigure
    _LOG.debug("Starting driver setup, reconfigure=%s", reconfigure)
    if reconfigure and dropdown_devices:
        _setup_step = SetupSteps.CONFIGURATION_MODE

        dropdown_actions = [
            {
                "id": "add",
                "label": {
                    "en": "Add a new client",
                },
            },
        ]

        # add remove & reset actions if there's at least one configured device
        if dropdown_devices:
            dropdown_actions.append(
                {
                    "id": "remove",
                    "label": {
                        "en": "Delete selected client",
                    },
                },
            )
            dropdown_actions.append(
                {
                    "id": "reset",
                    "label": {
                        "en": "Reset configuration and reconfigure",
                        "de": "Konfiguration zurücksetzen und neu konfigurieren",
                        "fr": "Réinitialiser la configuration et reconfigurer",
                    },
                },
            )
        else:
            # dummy entry if no clients are available
            dropdown_devices.append({"id": "", "label": {"en": "---"}})

        return RequestUserInput(
            {"en": "Configuration mode", "de": "Konfigurations-Modus"},
            [
                {
                    "field": {
                        "dropdown": {
                            "value": dropdown_devices[0]["id"],
                            "items": dropdown_devices,
                        }
                    },
                    "id": "choice",
                    "label": {
                        "en": "Configured devices",
                        "de": "Konfigurierte Geräte",
                        "fr": "Appareils configurés",
                    },
                },
                {
                    "field": {
                        "dropdown": {
                            "value": dropdown_actions[0]["id"],
                            "items": dropdown_actions,
                        }
                    },
                    "id": "action",
                    "label": {
                        "en": "Action",
                        "de": "Aktion",
                        "fr": "Appareils configurés",
                    },
                },
            ],
        )

    # Initial setup, make sure we have a clean configuration
    # config.devices.clear()  # triggers device instance removal
    _setup_step = SetupSteps.CONFIGURATION_MODE
    _LOG.debug(_user_input_manual)
    return _user_input_manual


async def handle_configuration_mode(
    msg: UserDataResponse,
) -> RequestUserInput | SetupComplete | SetupError:
    """
    Process user data response in a setup process.

    If ``address`` field is set by the user: try connecting to device and retrieve model information.
    Otherwise, start Plex instances discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device

    action = msg.input_values["action"]

    match action:
        case "add":
            _cfg_add_device = True
        case "remove":
            choice = msg.input_values["choice"]
            if not config.devices.remove(choice):
                _LOG.warning("Could not remove device from configuration: %s", choice)
                return SetupError(error_type=IntegrationSetupError.OTHER)
            config.devices.store()
            return SetupComplete()
        case "reset":
            config.devices.clear()
            return SetupComplete()
        case _:
            _LOG.error("Invalid configuration action: %s", action)
            return SetupError(error_type=IntegrationSetupError.OTHER)

    _setup_step = SetupSteps.CONFIGURATION_MODE
    return _user_input_manual


async def _handle_server_config(
    msg: UserDataResponse, use_existing_config=False
) -> RequestUserInput | SetupComplete | SetupError:
    """
    Process user data response in a setup process.

    If ``address`` field is set by the user: try connecting to device and retrieve model information.
    Otherwise, start LG TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step
    global _base_url

    dropdown_devices = []

    if (
        use_existing_config
        and "choice" in msg.input_values
        and msg.input_values["choice"] != "no-session"
    ):
        ec = config.devices.get(msg.input_values["choice"])
        msg.input_values["address"] = ec.address
        msg.input_values["port"] = ec.port
        msg.input_values["username"] = ec.username
        msg.input_values["password"] = ec.password
        msg.input_values["token"] = ec.auth_token
        msg.input_values["server"] = ec.server_name

    address = msg.input_values.get("address", None)
    port = msg.input_values["port"]
    username = msg.input_values["username"]
    password = msg.input_values["password"]
    auth_token = msg.input_values["token"]
    server_name = msg.input_values["server"]

    url = validate_url(address)

    try:
        server = get_server(
            server_name=server_name,
            username=username,
            password=password,
            auth_token=auth_token,
            url=url,
            port=port,
        )

        existing_players = get_configured_device_ids()
        for session in server.sessions():
            for player in session.players:
                if (
                    player.machineIdentifier not in existing_players
                    and player.local is True
                ):
                    dropdown_devices.append(
                        {
                            "id": player.machineIdentifier,
                            "label": {"en": f"{player.title} ({player.product})"},
                        }
                    )
        if not dropdown_devices:
            dropdown_devices.append(
                {"id": "no-session", "label": {"en": "No Active Sessions"}}
            )

        return RequestUserInput(
            {"en": "Unregistered Players"},
            [
                {
                    "id": "info",
                    "label": {
                        "en": "Client Selection",
                    },
                    "field": {
                        "label": {
                            "value": {
                                "en": "Please select the Plex Client you would like to control.\n\n \
                                If it's not in the list, make sure the machine is on and the client is active.",
                            }
                        }
                    },
                },
                {
                    "id": "player",
                    "label": {"en": "Unregistered Players"},
                    "field": {
                        "dropdown": {
                            "value": dropdown_devices[0]["id"],
                            "items": dropdown_devices,
                        }
                    },
                },
            ],
        )

    except Exception as ex:
        _LOG.error("Cannot connect to manually entered address %s: %s", address, ex)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)


async def _handle_client_selection(msg: UserDataResponse) -> SetupComplete | SetupError:
    """
    Process user data response in a setup process.

    If ``address`` field is set by the user: try connecting to device and retrieve model information.
    Otherwise, start LG TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step

    if "choice" in msg.input_values:
        ec = config.devices.get(msg.input_values["choice"])
        msg.input_values["address"] = ec.address
        msg.input_values["port"] = ec.port
        msg.input_values["username"] = ec.username
        msg.input_values["password"] = ec.password
        msg.input_values["token"] = ec.auth_token
        msg.input_values["server"] = ec.server_name

    address = msg.input_values["address"]
    port = msg.input_values["port"]
    username = msg.input_values["username"]
    password = msg.input_values["password"]
    auth_token = msg.input_values["token"]
    server_name = msg.input_values["server"]
    machine_identifier = msg.input_values["player"]
    name = "Plex"
    url = validate_url(address)

    server = get_server(
        server_name=server_name,
        username=username,
        password=password,
        auth_token=auth_token,
        url=url,
        port=port,
    )
    for session in server.sessions():
        for player in session.players:
            if player.machineIdentifier == machine_identifier:
                name = f"{player.title} ({player.product})"
                break

    if not server_name:
        server_name = server.friendlyName

    pcd = PlexConfigDevice(
        id=machine_identifier,
        name=name,
        address=url,
        port=port,
        username=username,
        password=password,
        auth_token=auth_token,
        server_name=server_name,
    )

    config.devices.add(pcd)
    config.devices.store()
    _LOG.info("Setup successfully completed for %s", machine_identifier)
    return SetupComplete()


def get_server(
    server_name, username, password, auth_token, url="", port=""
) -> PlexServer:
    """Get plex server."""
    address = f"{url}:{port}"
    try:
        if auth_token:
            server = PlexServer(baseurl=address, token=auth_token, timeout=5)
        else:
            account = MyPlexAccount(username, password)
            server: PlexServer = account.resource(server_name).connect()
        _LOG.debug("Connection %s succeeded over HTTP", address)
    except Exception as ex:
        _LOG.warning("Cannot connect to %s over HTTP [%s]", address, ex)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)

    return server


def get_configured_device_ids() -> list:
    """Get configuration IDs."""
    all_devices = config.devices.all()
    ids = []
    for device in all_devices:
        ids.append(device.id)
    return ids


def validate_url(uri):
    """Validate passed in URL and attempts to correct api endpoint if path isn't supplied."""
    if re.search("^http.*", uri) is None:
        uri = (
            "http://" + uri
        )  # Normalize to absolute URLs so urlparse will parse the way we want
    parsed_url = urlparse(uri)
    if parsed_url.scheme == "":
        uri = "http://" + uri
    return uri
