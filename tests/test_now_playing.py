"""Regression coverage for artwork, setup preferences, and session transitions."""

import asyncio
import base64
import sys
import time
import unittest
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
from xml.etree.ElementTree import fromstring

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "intg-plex"))

import browser
from const import PlexConfig
from media_player import PlexMediaPlayer
from now_playing import (
    IMAGE_SIZES,
    artist_album_labels,
    episode_artwork_path,
    media_content_type,
    pick_session,
)
from PIL import Image
from plex import PlexServer
from plexapi.client import PlexClient
from plexapi.server import PlexServer as PlexApiServer
from setup import PlexSetupFlow
from ucapi.media_player import Attributes as Attrs
from ucapi.media_player import States


def config(**kwargs):
    """Use fake connection details; tests never connect to Plex."""
    values = {
        "identifier": "client",
        "name": "Plex",
        "address": "localhost",
        "username": "",
        "password": "",
        "auth_token": "test-token",
        "server_name": "",
        "port": "32400",
        "tv_selection": "tv-poster-series",
        "movie_selection": "movie-poster",
    }
    values.update(kwargs)
    return PlexConfig(**values)


def device(**kwargs):
    """Isolate Plex media logic from the websocket lifecycle."""
    server = PlexServer.__new__(PlexServer)
    server._device_config = config(**kwargs)
    server._attributes = {Attrs.STATE: States.PLAYING}
    server._active_session_key = "new"
    server._session_revision = 1
    server._session_notification = None
    server._session = None
    server._plex_client = None
    api = PlexApiServer.__new__(PlexApiServer)
    api._baseurl = "http://localhost:32400"
    api._token = "test-token"
    server._plex = api
    server.push_update = Mock()
    return server


class ArtworkTests(unittest.TestCase):
    def test_all_tv_preferences_and_live_tv_fallback(self):
        item = SimpleNamespace(
            type="episode",
            grandparentThumb="/series",
            parentThumb="/season",
            thumb="/episode",
            grandparentArt="/series-art",
            art="/episode-art",
        )
        for selection, path in (
            ("tv-poster-series", "/series"),
            ("tv-poster-season", "/season"),
            ("tv-poster-episode", "/episode"),
            ("tv-poster-art", "/series-art"),
        ):
            with self.subTest(selection=selection):
                self.assertEqual(episode_artwork_path(item, selection), path)
        live = SimpleNamespace(
            type="episode", grandparentThumb="https://guide.test/poster"
        )
        server = device(tv_selection="tv-poster-episode")
        self.assertEqual(server.get_artwork_url(live), live.grandparentThumb)
        self.assertNotIn("X-Plex-Token", server.get_artwork_url(live))

    def test_movie_preference_in_browse_on_deck_and_search(self):
        server = device(movie_selection="movie-art")
        movie = SimpleNamespace(
            type="movie",
            thumb="/poster",
            art="/art",
            ratingKey=1,
            title="Film",
            year=2026,
            duration=1000,
        )
        api = SimpleNamespace(continueWatching=lambda: [movie])
        result = browser._browse_on_deck(server, api, 1, 20)
        self.assertEqual(
            parse_qs(urlparse(result.media.items[0].thumbnail).query)["url"],
            [server.build_plex_url("/art")],
        )
        api.library = SimpleNamespace(search=lambda **kwargs: [movie])
        result = browser._search_sync(server, api, "Film", None, None, 1, 20)
        self.assertEqual(
            parse_qs(urlparse(result.media[0].thumbnail).query)["url"],
            [server.build_plex_url("/art")],
        )

    def test_music_uses_cover_despite_movie_art_preference(self):
        server = device(movie_selection="movie-art")
        track = SimpleNamespace(type="track", parentThumb="/album")
        self.assertIn("/album?", server.get_artwork_url(track))

    def test_old_config_defaults_to_placeholders(self):
        values = asdict(config())
        del values["show_placeholders"]
        self.assertTrue(PlexConfig(**values).show_placeholders)
        server = device()
        del server._device_config.show_placeholders
        self.assertTrue(
            server.get_artwork_url(SimpleNamespace(type="movie")).startswith("data:")
        )

    def test_disabling_placeholders_clears_all_images_and_labels(self):
        server = device(show_placeholders=False)
        self.assertEqual(server.get_artwork_url(SimpleNamespace(type="episode")), "")
        server._attributes.update({Attrs.STATE: States.OFF, Attrs.MEDIA_TITLE: "Old"})
        server._set_idle_media()
        attrs = server.get_media_player_attributes()
        for attribute in (
            Attrs.MEDIA_IMAGE_URL,
            *IMAGE_SIZES,
            Attrs.MEDIA_TITLE,
            Attrs.MEDIA_ARTIST,
            Attrs.MEDIA_ALBUM,
        ):
            self.assertEqual(attrs[attribute], "")
        self.assertEqual(attrs[Attrs.MEDIA_POSITION], 0)
        self.assertIsNone(server._session)

    def test_transcoded_sizes_are_authenticated_and_preserve_aspect_ratio(self):
        server = device()
        server._attributes[Attrs.MEDIA_IMAGE_URL] = server.build_plex_url("/poster")
        attrs = server.get_media_player_attributes()
        for attribute, size in {**IMAGE_SIZES, Attrs.MEDIA_IMAGE_URL: 480}.items():
            query = parse_qs(urlparse(attrs[attribute]).query)
            self.assertEqual(query["width"], [str(size)])
            self.assertEqual(query["height"], [str(size)])
            self.assertEqual(query["minSize"], ["0"])
            self.assertEqual(query["upscale"], ["0"])
            self.assertEqual(query["X-Plex-Token"], ["test-token"])

    def test_placeholder_variants_have_requested_dimensions(self):
        server = device()
        for item_type in ("episode", "movie", "track", "idle"):
            with self.subTest(item_type=item_type):
                if item_type == "idle":
                    server._set_idle_media()
                else:
                    server._attributes[Attrs.MEDIA_IMAGE_URL] = server.get_artwork_url(
                        SimpleNamespace(type=item_type)
                    )
                attrs = server.get_media_player_attributes()
                for attribute, size in IMAGE_SIZES.items():
                    data = base64.b64decode(attrs[attribute].split(",", 1)[1])
                    with Image.open(BytesIO(data)) as image:
                        self.assertEqual(image.size, (size, size))

    def test_music_and_episode_labels_and_types(self):
        self.assertEqual(
            artist_album_labels(
                SimpleNamespace(
                    type="track",
                    originalTitle="Singer",
                    grandparentTitle="Various",
                    parentTitle="Album",
                )
            ),
            ("Singer", "Album"),
        )
        self.assertEqual(
            artist_album_labels(SimpleNamespace(type="episode", index=188)),
            ("E188", ""),
        )
        self.assertEqual(artist_album_labels(SimpleNamespace(type="movie")), ("", ""))
        self.assertEqual(media_content_type(SimpleNamespace(type="movie")), "movie")

    def test_session_selection_and_unmatched_notification(self):
        old = SimpleNamespace(
            sessionKey=1, ratingKey=10, players=[SimpleNamespace(state="paused")]
        )
        new = SimpleNamespace(
            sessionKey=2, ratingKey=20, players=[SimpleNamespace(state="playing")]
        )
        self.assertIs(pick_session([old, new]), new)
        self.assertIs(pick_session([old, new], {"sessionKey": "2"}), new)
        self.assertIs(pick_session([old, new], {"ratingKey": "10"}), old)
        self.assertIsNone(
            pick_session([old, new], {"sessionKey": "3", "ratingKey": "30"})
        )


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_rapid_toggles_poll_fresh_state_and_use_music_type(self):
        server = device()
        server.event_loop = asyncio.get_running_loop()
        client = PlexClient.__new__(PlexClient)
        # A stale cached timeline must not override the fresh second response.
        client._timeline_cache = [SimpleNamespace(state="playing", type="video")]
        client._timeline_cache_timestamp = time.time()
        client.sendCommand = Mock(
            side_effect=[
                fromstring(
                    '<MediaContainer><Timeline state="stopped" type="video"/>'
                    '<Timeline state="playing" type="music"/></MediaContainer>'
                ),
                fromstring('<MediaContainer><Timeline state="paused" type="music"/></MediaContainer>'),
            ]
        )
        calls = []

        def pause(mtype):
            calls.append(("pause", mtype))
            server._attributes[Attrs.STATE] = States.PAUSED

        def play(mtype):
            calls.append(("play", mtype))

        client.pause, client.play = pause, play
        server._plex_client = client
        await server.async_toggle_play_pause()
        await server.async_toggle_play_pause()
        self.assertEqual(calls, [("pause", "music"), ("play", "music")])
        self.assertEqual(client.sendCommand.call_count, 2)

    async def test_buffering_fallback_pauses_on_failed_or_timed_out_poll(self):
        for error in (ConnectionError("No timeline"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                server = device()
                server.event_loop = asyncio.get_running_loop()
                server._attributes[Attrs.STATE] = States.BUFFERING
                server._session = SimpleNamespace(type="track")
                calls = []

                def pause(mtype):
                    calls.append(("pause", mtype))

                def play(mtype):
                    calls.append(("play", mtype))

                server._plex_client = SimpleNamespace(
                    sendCommand=Mock(side_effect=error), pause=pause, play=play
                )
                await server.async_toggle_play_pause()
                self.assertEqual(calls, [("pause", "music")])

    async def test_entity_update_clears_timestamp_for_live_idle_and_reset(self):
        server = device(show_placeholders=False)
        entity = PlexMediaPlayer.__new__(PlexMediaPlayer)
        entity._device = server
        entity._entity_id = "test"
        entity.attributes = {}
        registry = Mock()
        registry.contains.return_value = True
        registry.get.return_value = entity
        registry.update_attributes.side_effect = lambda _entity_id, attrs: entity.attributes.update(attrs)
        entity._api = SimpleNamespace(configured_entities=registry)

        for transition in ("live", "idle", "reset"):
            with self.subTest(transition=transition):
                server._attributes.update(
                    {Attrs.STATE: States.PLAYING, Attrs.MEDIA_DURATION: 100}
                )
                server._set_media_position(10)
                await entity.sync_state()
                self.assertTrue(entity.attributes[Attrs.MEDIA_POSITION_UPDATED_AT])
                if transition == "live":
                    server._attributes[Attrs.MEDIA_DURATION] = 0
                    server._set_media_position(0)
                elif transition == "idle":
                    server._set_idle_media()
                else:
                    server._reset_state()
                await entity.sync_state()
                self.assertEqual(entity.attributes[Attrs.MEDIA_POSITION_UPDATED_AT], "")
                self.assertEqual(
                    registry.update_attributes.call_args.args[1][Attrs.MEDIA_POSITION_UPDATED_AT], ""
                )

    async def test_position_events_do_not_starve_metadata_fetches(self):
        server = device()
        tasks = []
        server._create_task = tasks.append
        event = {
            "type": "playing",
            "PlaySessionStateNotification": [
                {
                    "clientIdentifier": "client",
                    "sessionKey": "new",
                    "ratingKey": "20",
                    "state": "playing",
                }
            ],
        }
        try:
            server._plex_ws_updates("playing", event, None)
            revision = server._session_revision
            event["PlaySessionStateNotification"][0]["viewOffset"] = 1000
            server._plex_ws_updates("playing", event, None)
            self.assertEqual(server._session_revision, revision)
            self.assertEqual(server._attributes[Attrs.MEDIA_POSITION], 1)
        finally:
            for task in tasks:
                task.close()

    async def test_setup_preserves_preferences_and_saves_placeholder_setting(self):
        flow = PlexSetupFlow.__new__(PlexSetupFlow)
        current = config(
            tv_selection="tv-poster-episode",
            movie_selection="movie-art",
            show_placeholders=False,
        )
        form = await flow.get_additional_configuration_screen(current, {})
        fields = {setting["id"]: setting["field"] for setting in form.settings}
        self.assertEqual(
            fields["tv_selection"]["dropdown"]["value"], "tv-poster-episode"
        )
        self.assertEqual(fields["movie_selection"]["dropdown"]["value"], "movie-art")
        self.assertFalse(fields["show_placeholders"]["checkbox"]["value"])
        flow._pending_device_config = current
        updated = await flow.handle_additional_configuration_response(
            SimpleNamespace(input_values={"show_placeholders": False})
        )
        self.assertFalse(updated.show_placeholders)
        self.assertEqual(updated.tv_selection, current.tv_selection)
        self.assertEqual(updated.movie_selection, current.movie_selection)

    async def test_stale_notification_does_not_hide_valid_notification_in_batch(self):
        server = device()
        tasks = []
        server._create_task = tasks.append
        server._plex_ws_updates(
            "playing",
            {
                "type": "playing",
                "PlaySessionStateNotification": [
                    {
                        "clientIdentifier": "client",
                        "sessionKey": "old",
                        "state": "stopped",
                    },
                    {
                        "clientIdentifier": "client",
                        "sessionKey": "new",
                        "state": "playing",
                    },
                ],
            },
            None,
        )
        try:
            self.assertEqual(server._attributes[Attrs.STATE], States.PLAYING)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(server._session_revision, 2)
        finally:
            for task in tasks:
                task.close()

    async def test_fetch_finishing_after_session_change_is_ignored(self):
        server = device()
        server.event_loop = asyncio.get_running_loop()

        def late_result(*_args):
            server._session_revision += 1
            return SimpleNamespace(type="movie", title="Old", thumb="/old")

        server.get_session_by_client_id = late_result
        await server._fetch_session_details({"sessionKey": "old"}, "client", 1)
        self.assertIsNone(server._session)
        server.push_update.assert_not_called()

    async def test_initial_session_seeds_key_and_position(self):
        server = device()
        server.event_loop = asyncio.get_running_loop()
        session = SimpleNamespace(
            type="movie",
            title="New",
            thumb="/new",
            sessionKey=5,
            viewOffset=12000,
            duration=30000,
            players=[SimpleNamespace(state="playing")],
        )
        server.get_session_by_client_id = lambda *_args: session
        server.get_plex_client = Mock()
        await server._update_session_state()
        self.assertEqual(server._active_session_key, "5")
        self.assertEqual(server._attributes[Attrs.MEDIA_POSITION], 12)
        self.assertFalse(
            server._follows_session({"sessionKey": "4", "state": "stopped"})
        )

    async def test_old_idle_timer_does_not_clear_newer_stop(self):
        server = device()
        server._attributes.update({Attrs.STATE: States.OFF, Attrs.MEDIA_TITLE: "New"})
        with patch("plex.IDLE_DELAY_SECONDS", 0):
            await server._show_idle_after_delay(0)
            self.assertEqual(server._attributes[Attrs.MEDIA_TITLE], "New")
            await server._show_idle_after_delay(1)
            self.assertEqual(server._attributes[Attrs.MEDIA_TITLE], "")


if __name__ == "__main__":
    unittest.main()
