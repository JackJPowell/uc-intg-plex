"""
What the remote shows for the item that's playing: artwork, content type, and the
artist/album lines.

Artwork: the configured artwork choice (series/season/episode poster or background art for TV,
poster or art for movies) is tried first. If the item has no image in that slot, the
next best one is used rather than showing nothing. Live TV is the common case: Plex
stores an XMLTV guide's programme image as the *series* poster and leaves the episode
image, season poster and background art empty, so "Episode Poster" would otherwise
always be blank on live channels.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from ucapi.media_player import MediaContentType

from placeholders import PLACEHOLDER_MOVIE, PLACEHOLDER_MUSIC, PLACEHOLDER_TV

# Plex metadata attributes to try, in order, for each artwork setting.
TV_ARTWORK_ORDER = {
    "tv-poster-series": ("grandparentThumb", "parentThumb", "thumb", "grandparentArt", "art"),
    "tv-poster-season": ("parentThumb", "grandparentThumb", "thumb", "grandparentArt", "art"),
    "tv-poster-episode": ("thumb", "grandparentThumb", "parentThumb", "grandparentArt", "art"),
    "tv-poster-art": ("art", "grandparentArt", "grandparentThumb", "parentThumb", "thumb"),
}
MOVIE_ARTWORK_ORDER = {
    "movie-poster": ("thumb", "art"),
    "movie-art": ("art", "thumb"),
}
DEFAULT_TV_SELECTION = "tv-poster-series"
DEFAULT_MOVIE_SELECTION = "movie-poster"


def episode_artwork_path(item, tv_selection: str | None) -> str | None:
    """Return the first available artwork path for an episode, honouring tv_selection."""
    order = TV_ARTWORK_ORDER.get(tv_selection or "", TV_ARTWORK_ORDER[DEFAULT_TV_SELECTION])
    return _first(item, order)


def movie_artwork_path(item, movie_selection: str | None) -> str | None:
    """Return the first available artwork path for a movie, honouring movie_selection."""
    order = MOVIE_ARTWORK_ORDER.get(movie_selection or "", MOVIE_ARTWORK_ORDER[DEFAULT_MOVIE_SELECTION])
    return _first(item, order)


def _first(item, attributes: tuple[str, ...]) -> str | None:
    for name in attributes:
        value = getattr(item, name, None)
        if value:
            return value
    return None


def season_episode_label(item) -> str:
    """
    Return "S01E02" for an episode with season and episode numbers ("E188" with only an
    episode number), or "" when there's nothing to show.

    Built from parentIndex/index directly: plexapi's ``seasonEpisode`` can make an extra
    HTTP request to look up the season, and renders missing numbers as "None".
    """
    if getattr(item, "type", None) != "episode":
        return ""
    season = getattr(item, "parentIndex", None)
    episode = getattr(item, "index", None)
    if isinstance(episode, int):
        return f"S{season:02d}E{episode:02d}" if isinstance(season, int) else f"E{episode:02d}"
    return ""


# plexapi item types (``item.type``) to the remote's content types.
MEDIA_CONTENT_TYPES = {
    "track": MediaContentType.MUSIC,
    "episode": MediaContentType.TV_SHOW,
    "movie": MediaContentType.MOVIE,
    "clip": MediaContentType.VIDEO,
}


def media_content_type(item) -> MediaContentType | str:
    """Return the remote content type for a Plex item, or "" if unknown."""
    return MEDIA_CONTENT_TYPES.get(getattr(item, "type", None), "")


def artist_album_labels(item) -> tuple[str, str]:
    """
    Return the (artist, album) lines for the remote.

    Music: the track artist (falling back to the album artist, so compilations show who
    actually performs the track) and the album title. Episodes: the season/episode label
    in the artist line. Anything else gets empty strings, so labels from the previous
    item never linger.
    """
    match getattr(item, "type", None):
        case "track":
            artist = getattr(item, "originalTitle", None) or getattr(item, "grandparentTitle", None)
            return artist or "", getattr(item, "parentTitle", None) or ""
        case "episode":
            return season_episode_label(item), ""
        case _:
            return "", ""


def pick_session(sessions: list, payload: dict | None = None):
    """
    Choose the session to show from those on this player.

    Matches the websocket notification's sessionKey, then its ratingKey; without a match
    (or a notification) a session whose player is playing wins over a stale one, and
    the first session is the last resort.
    """
    if not sessions:
        return None
    if payload:
        for field in ("sessionKey", "ratingKey"):
            wanted = payload.get(field)
            if wanted in (None, ""):
                continue
            for session in sessions:
                if str(getattr(session, field, "")) == str(wanted):
                    return session
    for session in sessions:
        if any(getattr(p, "state", None) == "playing" for p in getattr(session, "players", [])):
            return session
    return sessions[0]


def placeholder_image(item) -> str:
    """Return the placeholder artwork (a data URI) for the item's media type, or ""."""
    return {
        "episode": PLACEHOLDER_TV,
        "movie": PLACEHOLDER_MOVIE,
        "clip": PLACEHOLDER_MOVIE,
        "track": PLACEHOLDER_MUSIC,
    }.get(getattr(item, "type", None), "")
