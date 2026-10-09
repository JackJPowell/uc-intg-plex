# Plex Integration for Unfolded Circle Remote — Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## v1.4.0 - 2026-10-08

### Added
- Media-specific placeholder artwork for TV, movies, and music when no image is available, plus a "Nothing playing on Plex" image when idle.
- A **Show Placeholder Artwork** setup option to enable or disable missing-artwork placeholders and the idle image. Enabled by default for new and existing configurations.
- Track artist and album information in now playing, with album artist used when the track artist is unavailable.
- Small (60 pixel), medium (100 pixel), and large (420 pixel) now-playing artwork variants for the remote UI.

### Changed
- Artwork now falls back to the next available image when the selected TV or movie artwork is missing, including programme images from Live TV guides.
- TV and movie artwork preferences now apply consistently to now playing, media browsing, and search results. Music uses track or album cover artwork.
- Now-playing default images and browse thumbnails are resized to a 480 pixel bounding box, preserving aspect ratio. Plex artwork uses the server's image transcoder; embedded placeholders are resized locally.
- Idle artwork appears after a short delay when playback stops, avoiding flashes between playlist items, and clears the previous item's details.

### Fixed
- Correctly track the active item when Plex lists multiple sessions for the same player, ignoring late pause/stop events from previous sessions and outdated metadata responses.
- Handle buffering notifications as active playback and tolerate playback notifications without a position.
- Clear stale artist, album, and episode labels when switching media, and report the correct content types for music, TV episodes, movies, and clips.
- Display season/episode labels without extra Plex requests or missing-number text.
- Preserve saved TV and movie artwork choices when reopening setup.
- Handle absolute artwork URLs, including external Live TV guide images, without prefixing the Plex server address or attaching its authentication token.

## v1.2.3 - 2026-04-10

### Changed
- Updated ucapi to 0.6.0, ucapi-framework to 1.9.1, and plexapi to 4.18.1.

---

## v1.2.2 - 2026-03-21

### Changed
- Applied a temporary workaround that results in the browse media icon displaying properly. Will be addressed properly with next firmware release.

---

## v1.2.1 - 2026-03-20

### Added
- Media browsing support with a structured library hierarchy (Movies, TV Shows, Music).
- On Deck section at the top of the browse tree, showing in-progress content across all libraries.
- Media search support across all library sections.
- Play media command, allowing items selected from the browser to be queued and played on the active client.

### Changed
- Playback now prefers a direct client connection over the server-proxied session, improving compatibility and reducing 404 errors on devices such as the Nvidia Shield.
- Plex Web clients are filtered out as a playback target, as they do not support the required playback API.

---

## v1.1.3 - 2026-03-09

These were all under the hood changes to ease future development.

### Changed
- Updated internal framework dependency to improve stability.

---

## v0.1.0 - 2025-01-22

### Added
- First release. Control Plex clients on your local network from your Unfolded Circle Remote.
