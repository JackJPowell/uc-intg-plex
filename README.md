# Plex integration for Unfolded Circle Remotes

Using [uc-integration-api](https://github.com/aitatoi/integration-python-library)

The driver lets you control your Plex clients with the Unfolded Circle Remote Two.

## Media Player
Supported attributes:
 - For the initial release, only standard media controls are supported:
   - Play
   - Pause
   - Stop
   - Seeking
   - Fast Forward and Rewind
   - Next and Previous

Supported commands:
- Client navigation and playback shortcuts (audio track selection, subtitle selection, etc.) will be coming in a future release

## Remote
- See Media Player

## Usage
The simpliest way to get started is by uploading this integration to your unfolded circle remote. You'll find the option on the integration tab in the web configurator. Simply upload the .tar.gz file attached to the release. This option is nice and doesn't require a separate docker instance to host the package. However, upgrading is a fully manual process. To help with this, a docker image is also provided that allows you to run it externally from the remote and easily upgrade when new versions are released. 

### Docker
```docker run -d --name=uc-intg-plex -p 9090:9090 --restart unless-stopped ghcr.io/jackjpowell/uc-intg-plex:latest```

### Docker Compose
```
services:
  uc-intg-plex:
    image: ghcr.io/jackjpowell/uc-intg-plex:latest
    container_name: uc-intg-plex
    ports:
      - 9090:9090
    restart: unless-stopped
```
