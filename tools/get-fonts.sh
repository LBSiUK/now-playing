#!/bin/sh
# Download the Spotify Mix fonts the page uses into web/fonts/, from Spotify's own web
# player stylesheet. They are Spotify's fonts, so they are not in this repository; without
# them the page falls back to Helvetica Neue.
set -eu
cd "$(dirname "$0")/../web"
mkdir -p fonts
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
css=$(curl -fsSL -A "$UA" https://open.spotify.com/ | grep -o 'https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[0-9a-f]*\.css' | head -1)
[ -n "$css" ] || { echo "could not find the web player stylesheet" >&2; exit 1; }
urls=$(curl -fsSL -A "$UA" "$css" | grep -o 'https://encore\.scdn\.co/fonts/[A-Za-z-]*-[0-9a-f]\{32\}\.woff2' | sort -u)
for name in SpotifyMixUI-Regular SpotifyMixUI-Bold SpotifyMixUITitle-Bold SpotifyMixUITitle-Extrabold; do
  url=$(printf '%s\n' "$urls" | grep "/$name-[0-9a-f]\{32\}\.woff2" | head -1 || true)
  [ -n "$url" ] || { echo "not found: $name" >&2; exit 1; }
  curl -fsSL "$url" -o "fonts/$name.woff2"
  echo "web/fonts/$name.woff2"
done
