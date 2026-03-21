# Plex Integration for Unfolded Circle Remote — Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## Unreleased

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
