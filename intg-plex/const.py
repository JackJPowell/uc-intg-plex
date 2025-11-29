"""Constants used for Plex integration."""

from typing import List
from dataclasses import dataclass
from ucapi.media_player import Commands, Features, MediaType
from ucapi.ui import Buttons, DeviceButtonMapping, UiPage


@dataclass
class PlexDevice:
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


PLEX_MEDIA_TYPES = {
    "music": MediaType.MUSIC,
    "artist": MediaType.MUSIC,
    "album": MediaType.MUSIC,
    "song": MediaType.MUSIC,
    "video": MediaType.VIDEO,
    "set": MediaType.MUSIC,
    "musicvideo": MediaType.VIDEO,
    "movie": MediaType.MOVIE,
    "tvshow": MediaType.TVSHOW,
    "season": MediaType.TVSHOW,
    "episode": MediaType.TVSHOW,
    "channel": MediaType.TVSHOW,
    "audio": MediaType.MUSIC,
}

PLEX_FEATURES = [
    Features.VOLUME,
    Features.MUTE,
    Features.PLAY_PAUSE,
    Features.STOP,
    Features.NEXT,
    Features.PREVIOUS,
    Features.FAST_FORWARD,
    Features.REWIND,
    Features.MEDIA_TITLE,
    Features.MEDIA_IMAGE_URL,
    Features.MEDIA_TYPE,
    Features.MEDIA_DURATION,
    Features.MEDIA_POSITION,
    Features.MEDIA_ARTIST,
    Features.DPAD,
    Features.HOME,
    Features.MENU,
    Features.CONTEXT_MENU,
    Features.GUIDE,
    Features.INFO,
    Features.SEEK,
]


PLEX_SIMPLE_COMMANDS = {}

PLEX_ACTIONS_KEYMAP = {}

PLEX_REMOTE_BUTTONS_MAPPING: List[DeviceButtonMapping] = [
    {"button": Buttons.BACK, "short_press": {"cmd_id": Commands.BACK}},
    {"button": Buttons.HOME, "short_press": {"cmd_id": Commands.HOME}},
    {"button": Buttons.CHANNEL_DOWN, "short_press": {"cmd_id": Commands.CHANNEL_DOWN}},
    {"button": Buttons.CHANNEL_UP, "short_press": {"cmd_id": Commands.CHANNEL_UP}},
    {"button": Buttons.DPAD_UP, "short_press": {"cmd_id": Commands.CURSOR_UP}},
    {"button": Buttons.DPAD_DOWN, "short_press": {"cmd_id": Commands.CURSOR_DOWN}},
    {"button": Buttons.DPAD_LEFT, "short_press": {"cmd_id": Commands.REWIND}},
    {"button": Buttons.DPAD_RIGHT, "short_press": {"cmd_id": Commands.FAST_FORWARD}},
    {"button": Buttons.DPAD_MIDDLE, "short_press": {"cmd_id": Commands.CURSOR_ENTER}},
    {"button": Buttons.PLAY, "short_press": {"cmd_id": Commands.PLAY_PAUSE}},
    {"button": Buttons.PREV, "short_press": {"cmd_id": Commands.PREVIOUS}},
    {"button": Buttons.NEXT, "short_press": {"cmd_id": Commands.NEXT}},
    {"button": Buttons.MUTE, "short_press": {"cmd_id": Commands.MUTE}},
]

PLEX_REMOTE_SIMPLE_COMMANDS = [
    Commands.MUTE,
    Commands.PLAY_PAUSE,
    Commands.STOP,
    Commands.HOME,
    Commands.BACK,
    Commands.PREVIOUS,
    Commands.NEXT,
    Commands.FAST_FORWARD,
    Commands.REWIND,
]

PLEX_REMOTE_UI_PAGES: List[UiPage] = [
    {
        "page_id": "Plex commands",
        "name": "Plex commands",
        "grid": {"width": 4, "height": 6},
        "items": [
            {
                "command": {
                    "cmd_id": "remote.send",
                    "params": {"command": Commands.PLAY_PAUSE, "repeat": 1},
                },
                "icon": "uc:play",
                "location": {"x": 0, "y": 0},
                "size": {"height": 1, "width": 1},
                "type": "icon",
            },
            {
                "command": {
                    "cmd_id": "remote.send",
                    "params": {"command": Commands.AUDIO_TRACK, "repeat": 1},
                },
                "icon": "uc:language",
                "location": {"x": 1, "y": 0},
                "size": {"height": 1, "width": 1},
                "type": "icon",
            },
            {
                "command": {
                    "cmd_id": "remote.send",
                    "params": {"command": Commands.SUBTITLE, "repeat": 1},
                },
                "icon": "uc:cc",
                "location": {"x": 2, "y": 0},
                "size": {"height": 1, "width": 1},
                "type": "icon",
            },
            {
                "command": {"cmd_id": Commands.CONTEXT_MENU},
                "icon": "uc:menu",
                "location": {"x": 3, "y": 5},
                "size": {"height": 1, "width": 1},
                "type": "icon",
            },
        ],
    },
]


def key_update_helper(input_attributes, key: str, value: str | None, attributes):
    """Return modified attributes only."""
    if value is None:
        return attributes

    if key in input_attributes:
        if input_attributes[key] != value:
            attributes[key] = value
    else:
        attributes[key] = value

    return attributes
