# Now Playing server for the iPad Air 2. Runs under rootless Podman on a home server
# (see deploy/now-playing.container).
# Debian 12 base: Playwright's --with-deps knows its packages.
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    NP_PORT=8092 NP_SPDC_FILE=/config/sp_dc NP_PROFILE_DIR=/config/browser \
    HOME=/tmp
# Google Chrome on a virtual screen (Xvfb) for signing in to Spotify and renewing the
# sp_dc cookie. The container runs as the host user (UserNS=keep-id).
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth \
 && pip install playwright==1.64.1 \
 && playwright install --with-deps chrome \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py signin.py ./
COPY web ./web
EXPOSE 8092
# xvfb-run hangs as a container's first process (it waits on a signal that never comes),
# so start Xvfb directly. RunInit=true in the Quadlet unit reaps Chrome's processes.
ENV DISPLAY=:99
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 & exec python server.py"]
