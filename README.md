[![Discord](https://badgen.net/discord/online-members/zGVYf58)](https://discord.gg/zGVYf58)
![GitHub Release](https://img.shields.io/github/v/release/jackjpowell/uc-intg-plex)
![GitHub Downloads (all assets, all releases)](https://img.shields.io/github/downloads/jackjpowell/uc-intg-plex/total)
<a href="#"><img src="https://img.shields.io/maintenance/yes/2025.svg"></a>
[![Buy Me A Coffee](https://img.shields.io/badge/Buy_Me_A_Coffee&nbsp;☕-FFDD00?logo=buy-me-a-coffee&logoColor=white&labelColor=grey)](https://buymeacoffee.com/jackpowell)

# Plex integration for Unfolded Circle Remotes

Using [uc-integration-api](https://github.com/aitatoi/integration-python-library)

The driver lets you control your Plex clients with the Unfolded Circle Remote Two. The capabilities are limited due to API support of the clients but works well for controlling media playback and makes great use of the media widget. 

This integration pairs great with Android devices like the Nvidia Shield as they do not provide a way to populate the media widget with poster, title, or position information. This has been tested with iOS and Android clients.

The initial release supports movie and tv show playback and control but may not extend to music. If there is a demand for this I can add in support. 

The setup flow should guide you through the process but the only thing I'll point out is that the plex client must be actively playing for it to be seen during setup. 

## Media Player
Supported attributes:
 - For the initial release, only standard media controls are supported:
   - Play
   - Pause
   - Stop
   - Seeking
   - Fast Forward and Rewind
   - Next and Previous

## Usage
The simpliest way to get started is by uploading this integration to your unfolded circle remote. You'll find the option on the integration tab in the web configurator. Simply upload the .tar.gz file attached to the release. This option is nice and doesn't require a separate docker instance to host the package. However, upgrading is a fully manual process. To help with this, a docker image is also provided that allows you to run it externally from the remote and easily upgrade when new versions are released. 

### Docker
```
docker run -d --name=uc-intg-plex --network host -v </local/path>:/config --restart unless-stopped ghcr.io/jackjpowell/uc-intg-plex:latest
```

### Docker Compose
```
services:
  uc-intg-plex:
    image: ghcr.io/jackjpowell/uc-intg-plex:latest
    container_name: uc-intg-plex
    network_mode: host
    volumes:
      - </local/path>:/config
    environment:
      - UC_INTEGRATION_HTTP_PORT=9090
    restart: unless-stopped
```
