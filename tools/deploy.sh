#!/bin/sh
# Build and (re)start the Now Playing container on the server, over ssh.
# Usage: HOST=user@server tools/deploy.sh      (from the project folder)
# The server needs Podman and ~/.config/now-playing/env with NP_CLIENT_ID=... (see README).
set -eu
: "${HOST:?set HOST=user@server}"
cd "$(dirname "$0")/.."
# tar over ssh: macOS's openrsync silently copies nothing with some flags.
COPYFILE_DISABLE=1 tar -czf - server.py signin.py Containerfile web deploy | ssh "$HOST" 'rm -rf ~/now-playing.new && mkdir -p ~/now-playing.new && tar -xzf - -C ~/now-playing.new && rm -rf ~/now-playing && mv ~/now-playing.new ~/now-playing'
ssh "$HOST" 'set -e
  mkdir -p ~/.config/now-playing && chmod 700 ~/.config/now-playing
  [ -f ~/.config/now-playing/env ] || { echo "missing ~/.config/now-playing/env (NP_CLIENT_ID=...)" >&2; exit 1; }
  podman build -q -t now-playing ~/now-playing >/dev/null
  mkdir -p ~/.config/containers/systemd
  cp ~/now-playing/deploy/now-playing.container ~/.config/containers/systemd/
  systemctl --user daemon-reload
  systemctl --user restart now-playing.service
  sleep 6
  systemctl --user is-active now-playing.service
  curl -s -o /dev/null -w "page %{http_code}\n" http://127.0.0.1:8092/'
echo "Open http://${HOST#*@}:8092/"
