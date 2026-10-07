"""
Artwork selection with fallbacks.

The configured artwork choice (series/season/episode poster or background art for TV,
poster or art for movies) is tried first. If the item has no image in that slot, the
next best one is used rather than showing nothing. Live TV is the common case: Plex
stores an XMLTV guide's programme image as the *series* poster and leaves the episode
image, season poster and background art empty, so "Episode Poster" would otherwise
always be blank on live channels.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

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
