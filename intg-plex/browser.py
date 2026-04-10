"""
Plex media browser and search implementation.

Implements the UC Remote Two media browsing / searching API against a Plex server.

Browse hierarchy:
  ROOT
  ├─ On Deck                          (media_type="on_deck")
  └─ Libraries                        (media_type="library_root")
     ├─ Movies                         (media_type="movie_library", media_id=<section_key>)
     │   ├─ All Movies                 (media_type="movies",         media_id=<section_key>)
     │   └─ Recently Added            (media_type="movies_recent",  media_id=<section_key>)
     │       └─ Movie                 (can_play=True)
     ├─ TV Shows                       (media_type="show_library",   media_id=<section_key>)
     │   ├─ All Shows                  (media_type="shows",          media_id=<section_key>)
     │   └─ Recently Added            (media_type="shows_recent",   media_id=<section_key>)
     │       └─ Show                  (media_type="show",            media_id=<rating_key>)
     │           └─ Season            (media_type="season",          media_id=<rating_key>)
     │               └─ Episode       (can_play=True)
     └─ Music                          (media_type="music_library",  media_id=<section_key>)
         ├─ All Artists               (media_type="artists",        media_id=<section_key>)
         └─ Recently Added            (media_type="music_recent",   media_id=<section_key>)
             └─ Artist                (media_type="artist",          media_id=<rating_key>)
                 └─ Album             (media_type="album",           media_id=<rating_key>)
                     └─ Track         (can_play=True)

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import TYPE_CHECKING

from plexapi.library import LibrarySection, MovieSection, ShowSection, MusicSection

from ucapi import (
    BrowseMediaItem,
    BrowseOptions,
    BrowseResults,
    MediaClass,
    MediaContentType,
    Pagination,
    SearchOptions,
    SearchResults,
)

if TYPE_CHECKING:
    from plex import PlexServer

_LOG = logging.getLogger(__name__)

# Map library section TYPE to a media_type identifier used in browse IDs
_SECTION_TYPE_MAP = {
    "movie": "movie_library",
    "show": "show_library",
    "artist": "music_library",
}

# Default page size if not configured
DEFAULT_PAGE_SIZE = 20


def _pagination(
    items_returned: int, total: int | None, page: int, _limit: int
) -> Pagination:
    """Build the pagination block for a browse response."""
    return Pagination(limit=items_returned, page=page, count=total)


def _thumb_url(server: "PlexServer", path: str | None) -> str | None:
    """Build an authenticated Plex thumbnail URL, or None if no path."""
    if not path:
        return None
    return server.build_plex_url(path)


def _episode_thumb(server: "PlexServer", item) -> str | None:
    """Return the correct thumbnail for an episode, respecting tv_selection config."""
    selection = getattr(server.device_config, "tv_selection", "tv-poster-series")
    match selection:
        case "tv-poster-series":
            path = getattr(item, "grandparentThumb", None)
        case "tv-poster-season":
            path = getattr(item, "parentThumb", None)
        case "tv-poster-episode":
            path = getattr(item, "thumb", None)
        case "tv-poster-art":
            # artUrl is a full URL already
            art = getattr(item, "artUrl", None)
            return art or _thumb_url(server, getattr(item, "grandparentThumb", None))
        case _:
            path = getattr(item, "grandparentThumb", None)
    return _thumb_url(server, path)


def _make_item(
    media_id: str,
    title: str,
    *,
    subtitle: str | None = None,
    media_class: str | None = None,
    media_type: str | None = None,
    can_browse: bool = False,
    can_play: bool = False,
    can_search: bool = False,
    thumbnail: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    duration: int | None = None,
    children: list[BrowseMediaItem] | None = None,
) -> BrowseMediaItem:
    """Build a BrowseMediaItem dataclass instance."""
    return BrowseMediaItem(
        media_id=media_id,
        title=title,
        subtitle=subtitle,
        media_class=media_class or "",
        media_type=media_type or "",
        can_browse=can_browse,
        can_play=can_play,
        can_search=can_search,
        thumbnail=thumbnail,
        artist=artist,
        album=album,
        duration=duration,
        items=children,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def browse(server: "PlexServer", options: BrowseOptions) -> BrowseResults:
    """
    Handle a browse_media request.

    :param server: Connected PlexServer device instance
    :param options: Typed browse options from the remote
    :return: BrowseResults dataclass
    """
    media_id: str | None = options.media_id
    media_type: str | None = options.media_type
    paging = options.paging
    limit: int = int(
        (paging.limit if paging and paging.limit else None)
        or server.device_config.page_size
        or DEFAULT_PAGE_SIZE
    )
    page: int = int((paging.page if paging and paging.page else None) or 1)

    plex = server._plex  # pylint: disable=protected-access
    if plex is None:
        _LOG.warning("browse_media called but Plex server is not connected")
        return _empty_response()

    try:
        return await server.event_loop.run_in_executor(
            None,
            lambda: _browse_sync(server, plex, media_id, media_type, page, limit),
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _LOG.error(
            "Error browsing media (media_id=%s media_type=%s): %s",
            media_id,
            media_type,
            ex,
            exc_info=True,
        )
        return _empty_response()


async def search(server: "PlexServer", options: SearchOptions) -> SearchResults:
    """
    Handle a search_media request.

    :param server: Connected PlexServer device instance
    :param options: Typed search options from the remote
    :return: SearchResults dataclass
    """
    query: str = (options.query or "").strip()
    media_id: str | None = options.media_id
    media_type: str | None = options.media_type
    paging = options.paging
    limit: int = int(
        (paging.limit if paging and paging.limit else None)
        or server.device_config.page_size
        or DEFAULT_PAGE_SIZE
    )
    page: int = int((paging.page if paging and paging.page else None) or 1)

    if not query:
        return SearchResults(media=[], pagination=Pagination(limit=0, page=1, count=0))

    plex = server._plex  # pylint: disable=protected-access
    if plex is None:
        return SearchResults(media=[], pagination=Pagination(limit=0, page=1, count=0))

    try:
        return await server.event_loop.run_in_executor(
            None,
            lambda: _search_sync(
                server, plex, query, media_id, media_type, page, limit
            ),
        )
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _LOG.error("Error searching media (query=%s): %s", query, ex, exc_info=True)
        return SearchResults(media=[], pagination=Pagination(limit=0, page=1, count=0))


# ---------------------------------------------------------------------------
# Synchronous helpers (run in executor)
# ---------------------------------------------------------------------------


def _empty_response() -> BrowseResults:
    return BrowseResults(
        media=_make_item(
            "root", "Plex", media_class=MediaClass.DIRECTORY, can_browse=True
        ),
        pagination=Pagination(limit=0, page=1, count=None),
    )


def _browse_sync(server, plex, media_id, media_type, page, limit) -> BrowseResults:
    """Synchronous browse logic (runs in thread pool)."""

    sort = server.device_config.sort_order or "titleSort:asc"

    # ── ROOT: list all libraries ────────────────────────────────────────────
    if not media_id or media_type in (None, "root", "library_root"):
        return _browse_root(server, plex)

    # ── On Deck ─────────────────────────────────────────────────────────────
    if media_type == "on_deck":
        return _browse_on_deck(server, plex, page, limit)

    # ── Library-level views ─────────────────────────────────────────────────
    if media_type in ("movie_library", "show_library", "music_library"):
        return _browse_library(server, plex, media_id, media_type)

    # ── All Movies ──────────────────────────────────────────────────────────
    if media_type == "movies":
        return _browse_movies(server, plex, media_id, sort, page, limit)

    if media_type == "movies_recent":
        return _browse_movies_recent(server, plex, media_id, page, limit)

    # ── All Shows ───────────────────────────────────────────────────────────
    if media_type == "shows":
        return _browse_shows(server, plex, media_id, sort, page, limit)

    if media_type == "shows_recent":
        return _browse_shows_recent(server, plex, media_id, page, limit)

    # ── Show → Seasons ───────────────────────────────────────────────────────
    if media_type == "show":
        return _browse_seasons(server, plex, media_id)

    # ── Season → Episodes ────────────────────────────────────────────────────
    if media_type == "season":
        return _browse_episodes(server, plex, media_id)

    # ── All Artists ─────────────────────────────────────────────────────────
    if media_type == "artists":
        return _browse_artists(server, plex, media_id, sort, page, limit)

    if media_type == "music_recent":
        return _browse_music_recent(server, plex, media_id, page, limit)

    # ── Artist → Albums ─────────────────────────────────────────────────────
    if media_type == "artist":
        return _browse_albums(server, plex, media_id)

    # ── Album → Tracks ──────────────────────────────────────────────────────
    if media_type == "album":
        return _browse_tracks(server, plex, media_id)

    _LOG.warning(
        "Unknown media_type for browse: %s (media_id=%s)", media_type, media_id
    )
    return _browse_root(server, plex)


def _browse_on_deck(server, plex, page: int, limit: int) -> BrowseResults:
    """Return all On Deck items (in-progress content) across all libraries."""
    all_items = plex.continueWatching()
    start = (page - 1) * limit
    items = all_items[start : start + limit]

    children = []
    for item in items:
        item_type = getattr(item, "type", "")
        if item_type == "episode":
            show_title = getattr(item, "grandparentTitle", None)
            season_num = getattr(item, "parentIndex", None)
            ep_num = getattr(item, "index", None)
            subtitle = (
                f"S{season_num:02d}E{ep_num:02d}" if season_num and ep_num else None
            )
            children.append(
                _make_item(
                    str(item.ratingKey),
                    item.title,
                    subtitle=subtitle,
                    artist=show_title,
                    media_class=MediaClass.EPISODE,
                    media_type=MediaContentType.EPISODE,
                    can_play=True,
                    thumbnail=_episode_thumb(server, item),
                    duration=int(item.duration / 1000)
                    if getattr(item, "duration", None)
                    else None,
                )
            )
        elif item_type == "movie":
            children.append(
                _make_item(
                    str(item.ratingKey),
                    item.title,
                    subtitle=str(item.year) if getattr(item, "year", None) else None,
                    media_class=MediaClass.MOVIE,
                    media_type=MediaContentType.MOVIE,
                    can_play=True,
                    thumbnail=_thumb_url(server, getattr(item, "thumb", None)),
                    duration=int(item.duration / 1000)
                    if getattr(item, "duration", None)
                    else None,
                )
            )
        else:
            children.append(
                _make_item(
                    str(item.ratingKey),
                    item.title,
                    media_class=MediaClass.DIRECTORY,
                    media_type=item_type,
                    can_play=True,
                    thumbnail=_thumb_url(server, getattr(item, "thumb", None)),
                )
            )

    parent = _make_item(
        "on_deck",
        "On Deck",
        media_class=MediaClass.DIRECTORY,
        media_type="on_deck",
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(all_items), page, limit),
    )


def _browse_root(server, plex) -> BrowseResults:
    """Return top-level library sections."""
    sections = plex.library.sections()
    children: list[BrowseMediaItem] = [
        _make_item(
            "on_deck",
            "On Deck",
            media_class=MediaClass.DIRECTORY,
            media_type="on_deck",
            can_browse=True,
        )
    ]
    for section in sections:
        mt = _SECTION_TYPE_MAP.get(section.type)
        if mt is None:
            continue
        children.append(
            _make_item(
                str(section.key),
                section.title,
                media_class=MediaClass.DIRECTORY,
                media_type=mt,
                can_browse=True,
                can_search=True,
                thumbnail=_thumb_url(server, getattr(section, "thumb", None)),
            )
        )

    root = _make_item(
        "root",
        "Plex",
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    total = len(children)
    return BrowseResults(
        media=root,
        pagination=_pagination(total, total, 1, total or 1),
    )


def _browse_library(server, plex, section_key: str, media_type: str) -> BrowseResults:
    """Return sub-categories for a library section (All Items / Recently Added)."""
    section = plex.library.sectionByID(int(section_key))

    if media_type == "movie_library":
        sub_all = _make_item(
            section_key,
            "All Movies",
            media_type="movies",
            media_class=MediaClass.MOVIE,
            can_browse=True,
            can_search=True,
        )
        sub_rec = _make_item(
            section_key,
            "Recently Added",
            media_type="movies_recent",
            media_class=MediaClass.MOVIE,
            can_browse=True,
        )
    elif media_type == "show_library":
        sub_all = _make_item(
            section_key,
            "All Shows",
            media_type="shows",
            media_class=MediaClass.TV_SHOW,
            can_browse=True,
            can_search=True,
        )
        sub_rec = _make_item(
            section_key,
            "Recently Added",
            media_type="shows_recent",
            media_class=MediaClass.TV_SHOW,
            can_browse=True,
        )
    else:  # music_library
        sub_all = _make_item(
            section_key,
            "All Artists",
            media_type="artists",
            media_class=MediaClass.ARTIST,
            can_browse=True,
            can_search=True,
        )
        sub_rec = _make_item(
            section_key,
            "Recently Added",
            media_type="music_recent",
            media_class=MediaClass.ALBUM,
            can_browse=True,
        )

    thumb = _thumb_url(server, getattr(section, "thumb", None))
    parent = _make_item(
        section_key,
        section.title,
        media_class=MediaClass.DIRECTORY,
        media_type=media_type,
        thumbnail=thumb,
        can_browse=True,
        can_search=True,
        children=[sub_all, sub_rec],
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(2, 2, 1, 2),
    )


def _browse_movies(
    server, plex, section_key: str, sort: str, page: int, limit: int
) -> BrowseResults:
    section: MovieSection = plex.library.sectionByID(int(section_key))
    start = (page - 1) * limit
    movies = section.search(
        libtype="movie",
        sort=sort,
        container_start=start,
        container_size=limit,
        maxresults=limit,
    )
    total = len(section.search(libtype="movie"))  # total count

    children = [
        _make_item(
            str(m.ratingKey),
            m.title,
            subtitle=str(m.year) if getattr(m, "year", None) else None,
            media_class=MediaClass.MOVIE,
            media_type=MediaContentType.MOVIE,
            can_play=True,
            thumbnail=_thumb_url(server, getattr(m, "thumb", None)),
            duration=int(getattr(m, "duration", 0) / 1000)
            if getattr(m, "duration", None)
            else None,
        )
        for m in movies
    ]
    parent = _make_item(
        section_key,
        section.title,
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), total, page, limit),
    )


def _browse_movies_recent(
    server, plex, section_key: str, page: int, limit: int
) -> BrowseResults:
    section: MovieSection = plex.library.sectionByID(int(section_key))
    all_recent = section.recentlyAdded(maxresults=100, libtype="movie")
    start = (page - 1) * limit
    movies = all_recent[start : start + limit]

    children = [
        _make_item(
            str(m.ratingKey),
            m.title,
            subtitle=str(m.year) if getattr(m, "year", None) else None,
            media_class=MediaClass.MOVIE,
            media_type=MediaContentType.MOVIE,
            can_play=True,
            thumbnail=_thumb_url(server, getattr(m, "thumb", None)),
            duration=int(getattr(m, "duration", 0) / 1000)
            if getattr(m, "duration", None)
            else None,
        )
        for m in movies
    ]
    parent = _make_item(
        section_key,
        "Recently Added",
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(all_recent), page, limit),
    )


def _browse_shows(
    server, plex, section_key: str, sort: str, page: int, limit: int
) -> BrowseResults:
    section: ShowSection = plex.library.sectionByID(int(section_key))
    start = (page - 1) * limit
    shows = section.search(
        libtype="show",
        sort=sort,
        container_start=start,
        container_size=limit,
        maxresults=limit,
    )
    total = len(section.search(libtype="show"))

    children = [
        _make_item(
            str(s.ratingKey),
            s.title,
            media_class=MediaClass.TV_SHOW,
            media_type="show",
            can_browse=True,
            thumbnail=_thumb_url(server, getattr(s, "thumb", None)),
        )
        for s in shows
    ]
    parent = _make_item(
        section_key,
        section.title,
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), total, page, limit),
    )


def _browse_shows_recent(
    server, plex, section_key: str, page: int, limit: int
) -> BrowseResults:
    section: ShowSection = plex.library.sectionByID(int(section_key))
    all_recent = section.recentlyAdded(maxresults=100, libtype="episode")
    # Group by show to avoid duplicates
    seen: set[str] = set()
    unique_shows: list = []
    for ep in all_recent:
        show_key = str(getattr(ep, "grandparentRatingKey", ""))
        if show_key and show_key not in seen:
            seen.add(show_key)
            unique_shows.append(ep)

    start = (page - 1) * limit
    page_shows = unique_shows[start : start + limit]

    children = [
        _make_item(
            str(getattr(ep, "grandparentRatingKey", ep.ratingKey)),
            getattr(ep, "grandparentTitle", ep.title),
            media_class=MediaClass.TV_SHOW,
            media_type="show",
            can_browse=True,
            thumbnail=_thumb_url(server, getattr(ep, "grandparentThumb", None)),
        )
        for ep in page_shows
    ]
    parent = _make_item(
        section_key,
        "Recently Added",
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(unique_shows), page, limit),
    )


def _browse_seasons(server, plex, show_rating_key: str) -> BrowseResults:
    show = plex.fetchItem(int(show_rating_key))
    seasons = show.seasons()

    children = [
        _make_item(
            str(s.ratingKey),
            s.title,
            media_class=MediaClass.SEASON,
            media_type=MediaContentType.SEASON,
            can_browse=True,
            thumbnail=_thumb_url(server, getattr(s, "thumb", None)),
        )
        for s in seasons
    ]
    parent = _make_item(
        show_rating_key,
        show.title,
        media_class=MediaClass.TV_SHOW,
        can_browse=True,
        thumbnail=_thumb_url(server, getattr(show, "thumb", None)),
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(children), 1, len(children) or 1),
    )


def _browse_episodes(server, plex, season_rating_key: str) -> BrowseResults:
    season = plex.fetchItem(int(season_rating_key))
    episodes = season.episodes()

    children = [
        _make_item(
            str(ep.ratingKey),
            f"{ep.index}. {ep.title}" if ep.index else ep.title,
            subtitle=getattr(ep, "grandparentTitle", None),
            media_class=MediaClass.EPISODE,
            media_type=MediaContentType.EPISODE,
            can_play=True,
            thumbnail=_episode_thumb(server, ep),
            duration=int(getattr(ep, "duration", 0) / 1000)
            if getattr(ep, "duration", None)
            else None,
        )
        for ep in episodes
    ]
    parent = _make_item(
        season_rating_key,
        season.title,
        media_class=MediaClass.SEASON,
        can_browse=True,
        thumbnail=_thumb_url(server, getattr(season, "thumb", None)),
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(children), 1, len(children) or 1),
    )


def _browse_artists(
    server, plex, section_key: str, sort: str, page: int, limit: int
) -> BrowseResults:
    section: MusicSection = plex.library.sectionByID(int(section_key))
    start = (page - 1) * limit
    artists = section.search(
        libtype="artist",
        sort=sort,
        container_start=start,
        container_size=limit,
        maxresults=limit,
    )
    total = len(section.search(libtype="artist"))

    children = [
        _make_item(
            str(a.ratingKey),
            a.title,
            media_class=MediaClass.ARTIST,
            media_type=MediaContentType.ARTIST,
            can_browse=True,
            thumbnail=_thumb_url(server, getattr(a, "thumb", None)),
        )
        for a in artists
    ]
    parent = _make_item(
        section_key,
        section.title,
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), total, page, limit),
    )


def _browse_music_recent(
    server, plex, section_key: str, page: int, limit: int
) -> BrowseResults:
    section: MusicSection = plex.library.sectionByID(int(section_key))
    all_recent = section.recentlyAdded(maxresults=100, libtype="album")
    start = (page - 1) * limit
    albums = all_recent[start : start + limit]

    children = [
        _make_item(
            str(a.ratingKey),
            a.title,
            media_class=MediaClass.ALBUM,
            media_type=MediaContentType.ALBUM,
            artist=getattr(a, "parentTitle", None),
            can_browse=True,
            thumbnail=_thumb_url(server, getattr(a, "thumb", None)),
        )
        for a in albums
    ]
    parent = _make_item(
        section_key,
        "Recently Added",
        media_class=MediaClass.DIRECTORY,
        can_browse=True,
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(all_recent), page, limit),
    )


def _browse_albums(server, plex, artist_rating_key: str) -> BrowseResults:
    artist = plex.fetchItem(int(artist_rating_key))
    albums = artist.albums()

    children = [
        _make_item(
            str(a.ratingKey),
            a.title,
            media_class=MediaClass.ALBUM,
            media_type=MediaContentType.ALBUM,
            can_browse=True,
            thumbnail=_thumb_url(server, getattr(a, "thumb", None)),
        )
        for a in albums
    ]
    parent = _make_item(
        artist_rating_key,
        artist.title,
        media_class=MediaClass.ARTIST,
        can_browse=True,
        thumbnail=_thumb_url(server, getattr(artist, "thumb", None)),
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(children), 1, len(children) or 1),
    )


def _browse_tracks(server, plex, album_rating_key: str) -> BrowseResults:
    album = plex.fetchItem(int(album_rating_key))
    tracks = album.tracks()

    children = [
        _make_item(
            str(t.ratingKey),
            f"{t.index}. {t.title}" if t.index else t.title,
            subtitle=getattr(t, "parentTitle", None),
            media_class=MediaClass.TRACK,
            media_type=MediaContentType.TRACK,
            can_play=True,
            artist=getattr(t, "grandparentTitle", None),
            album=getattr(t, "parentTitle", None),
            duration=int(getattr(t, "duration", 0) / 1000)
            if getattr(t, "duration", None)
            else None,
            thumbnail=_thumb_url(
                server, getattr(t, "thumb", None) or getattr(album, "thumb", None)
            ),
        )
        for t in tracks
    ]
    parent = _make_item(
        album_rating_key,
        album.title,
        media_class=MediaClass.ALBUM,
        can_browse=True,
        thumbnail=_thumb_url(server, getattr(album, "thumb", None)),
        children=children,
    )
    return BrowseResults(
        media=parent,
        pagination=_pagination(len(children), len(children), 1, len(children) or 1),
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _search_sync(
    server,
    plex,
    query: str,
    media_id: str | None,
    _media_type: str | None,
    page: int,
    limit: int,
) -> SearchResults:
    """Synchronous search logic (runs in thread pool)."""
    results: list[BrowseMediaItem] = []

    # If media_id points to a specific section, scope the search there
    if media_id and media_id != "root":
        try:
            section: LibrarySection = plex.library.sectionByID(int(media_id))
            raw = section.search(title=query, maxresults=200)
        except Exception:  # pylint: disable=broad-exception-caught
            raw = plex.library.search(title=query, maxresults=200)
    else:
        raw = plex.library.search(title=query, maxresults=200)

    for item in raw:
        media_class, item_media_type, can_play, can_browse = _classify_item(item)
        thumb = _thumb_url(server, getattr(item, "thumb", None))
        subtitle = _search_subtitle(item, item_media_type)
        results.append(
            _make_item(
                str(item.ratingKey),
                item.title,
                subtitle=subtitle,
                media_class=media_class,
                media_type=item_media_type,
                can_play=can_play,
                can_browse=can_browse,
                thumbnail=thumb,
                artist=getattr(item, "grandparentTitle", None)
                or getattr(item, "parentTitle", None),
                album=getattr(item, "parentTitle", None)
                if item_media_type == "track"
                else None,
                duration=int(getattr(item, "duration", 0) / 1000)
                if getattr(item, "duration", None)
                else None,
            )
        )

    total = len(results)
    start = (page - 1) * limit
    page_results = results[start : start + limit]

    return SearchResults(
        media=page_results,
        pagination=_pagination(len(page_results), total, page, limit),
    )


def _classify_item(item) -> tuple[str, str, bool, bool]:
    """Return (media_class, media_type, can_play, can_browse) for a plexapi item."""
    t = getattr(item, "type", "")
    match t:
        case "movie":
            return "movie", "movie", True, False
        case "show":
            return "tv_show", "show", False, True
        case "season":
            return "season", "season", False, True
        case "episode":
            return "episode", "episode", True, False
        case "artist":
            return "artist", "artist", False, True
        case "album":
            return "album", "album", False, True
        case "track":
            return "track", "track", True, False
        case _:
            return "directory", t or "directory", False, True


def _search_subtitle(item, media_type: str) -> str | None:
    """Return a contextual subtitle string for a search result item."""
    match media_type:
        case "movie":
            year = getattr(item, "year", None)
            return str(year) if year else None
        case "episode":
            return getattr(item, "grandparentTitle", None)
        case "track":
            return getattr(item, "parentTitle", None)
        case "album":
            year = getattr(item, "year", None)
            return str(year) if year else None
        case _:
            return None
