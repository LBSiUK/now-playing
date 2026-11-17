#!/usr/bin/env python3
"""Now Playing server for the iPad Air 2 wall display.

Serves web/ and pulls every piece of song data from Spotify:
  GET /api/song?track=<spotify track id>
    -> title, artists, album, duration, album art, artist banner, canvas URL,
       lyrics (Spotify's own, with their sync type) and Spotify's lyric colours.

Track details and the artist banner work with an anonymous web-player token.
Canvas and lyrics need the sp_dc cookie of a logged-in Spotify web session,
read from ~/.config/now-playing/sp_dc (or NP_SPDC_FILE). Standard library only.
"""

import base64
import email.utils
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import signin

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
PORT = int(os.environ.get("NP_PORT", "8092"))
SPDC_FILE = os.path.expanduser(os.environ.get("NP_SPDC_FILE", "~/.config/now-playing/sp_dc"))
PROFILE_DIR = os.path.expanduser(os.environ.get("NP_PROFILE_DIR", os.path.join(os.path.dirname(SPDC_FILE), "browser")))
DEFAULT_TRACK = "1TK2iF5KlY8OEm5k5AeyBk"  # She Knows Too Much - Thundercat, Mac Miller

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")

# TOTP secret the web player uses for /api/token (version 61, current since Jan 2026).
SECRETS = {61: [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76, 94, 102, 43, 69,
                49, 120, 118, 80, 64, 78]}
SECRETS_URL = "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/main/secrets/secretDict.json"

# Persisted-query hashes from web-player.c078a389.js (2026-11-15). Refreshed from the
# live bundle when Spotify retires one.
HASHES = {
    "getTrack": "e7b1bf4e95cb3d733d48b442f93006e9712fd504e8b88d7a82e6e3bac9306a6b",
    "canvas": "575138ab27cd5c1b3e54da54d0a7cc8d85485402de26340c2145f0f6bb5e7a9f",
    "queryArtistOverview": "eafb1620d783ece118e72fbfede2b0064483e029d3c1aafafa1c2de0e25a89e7",
}

lock = threading.Lock()
tokens = {}          # "user" / "anon" -> (token, expires_epoch)
client_token = {}    # "value", "expires"
song_cache = {}      # track id -> (json, fetched_epoch)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def http(url, method="GET", headers=None, data=None, timeout=20):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers, r.read()


def read_cookie():
    try:
        with open(SPDC_FILE) as f:
            v = f.read().strip()
        if v.startswith("sp_dc="):
            v = v[6:]
        return v or None
    except OSError:
        return None


auth = {"needs_signin": False, "last_refresh": 0}


def save_cookie(value):
    os.makedirs(os.path.dirname(SPDC_FILE), exist_ok=True)
    fd = os.open(SPDC_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    with lock:
        tokens.pop("user", None)
        song_cache.clear()
    auth["needs_signin"] = False


def forget_cookie():
    try:
        os.remove(SPDC_FILE)
    except OSError:
        pass
    with lock:
        tokens.pop("user", None)
        song_cache.clear()


# ---------- What is playing: your own Spotify developer app ----------
# The web player's token gets HTTP 429 on every public Web API call, so playback state
# comes from your own developer app (Authorization Code with PKCE, no client secret). The
# server's signed-in Chrome approves the app and catches the redirect (see signin.py).

CLIENT_ID = os.environ.get("NP_CLIENT_ID", "")
REDIRECT_URI = signin.REDIRECT_PREFIX
SCOPES = "user-read-playback-state"
CONFIG_DIR = os.path.dirname(SPDC_FILE)
PLAYER_FILE = os.path.join(CONFIG_DIR, "player.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")

player = {"status": "none", "pending": {}, "last_try": 0}
now = {"state": "unknown", "track": None, "kind": None, "progress": 0, "duration": 0,
       "fetched": 0, "song": None, "lastAt": None}
viewers = {"last": 0}
wake = threading.Event()


def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_json(path, data, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def player_authorize_url():
    """Where Spotify asks to let the display see what is playing; None once connected."""
    if not CLIENT_ID or read_json(PLAYER_FILE).get("refresh_token"):
        return None
    verifier = b64url(os.urandom(48))
    state = b64url(os.urandom(12))
    pending = player["pending"]
    pending[state] = verifier
    while len(pending) > 4:
        pending.pop(next(iter(pending)))
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
        "scope": SCOPES, "state": state})


def account_token(fields):
    data = urllib.parse.urlencode(dict(fields, client_id=CLIENT_ID)).encode()
    _, _, b = http("https://accounts.spotify.com/api/token", method="POST", data=data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    d = json.loads(b)
    rec = {"access_token": d["access_token"],
           "refresh_token": d.get("refresh_token") or read_json(PLAYER_FILE).get("refresh_token"),
           "expires": time.time() + int(d.get("expires_in", 3600)) - 60}
    write_json(PLAYER_FILE, rec, private=True)
    return rec


def on_redirect(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    state, code = (q.get("state") or [""])[0], (q.get("code") or [""])[0]
    verifier = player["pending"].pop(state, None)
    if q.get("error") or not code or not verifier:
        log("player connect refused:", (q.get("error") or ["no code"])[0])
        return False
    account_token({"grant_type": "authorization_code", "code": code,
                   "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    player["status"] = "connected"
    log("player connected")
    wake.set()
    return True


def player_token():
    rec = read_json(PLAYER_FILE)
    if not rec.get("refresh_token"):
        return None
    if rec.get("access_token") and rec.get("expires", 0) > time.time():
        return rec["access_token"]
    try:
        return account_token({"grant_type": "refresh_token", "refresh_token": rec["refresh_token"]})["access_token"]
    except urllib.error.HTTPError as e:
        if e.code == 400:  # invalid_grant: access was removed in Spotify's settings
            try:
                os.remove(PLAYER_FILE)
            except OSError:
                pass
            player["status"] = "none"
            log("player access was revoked")
        raise


def player_status():
    if read_json(PLAYER_FILE).get("refresh_token"):
        return "connected"
    if not read_cookie():
        return "signed_out"
    if not CLIENT_ID or not signin.AVAILABLE:
        return "unavailable"
    return "needs_approval" if player["status"] == "needs_approval" else "connecting"


def auto_connect():
    """Signed in but the player is not connected yet: let the server's browser ask
    Spotify. Spotify answers at once when the app was allowed before."""
    if time.time() - player["last_try"] < 600 or browser.active:
        return
    player["last_try"] = time.time()
    url = player_authorize_url()
    if not url:
        return
    ok = False
    try:
        ok = browser.connect(url)
    except Exception as e:
        log("player auto-connect failed:", repr(e)[:200])
    player["status"] = "connected" if ok else "needs_approval"
    log("player auto-connect:", player["status"])


def episode_song(item):
    show = item.get("show") or {}
    images = item.get("images") or show.get("images") or []
    art = images[0]["url"] if images else None
    return {"id": item.get("id"), "title": item.get("name"), "artists": [show.get("name") or "Podcast"],
            "album": show.get("name"), "duration": (item.get("duration_ms") or 0) / 1000, "art": art,
            "banner": art, "canvas": None, "lyrics": None, "colors": None, "loggedIn": True, "episode": True}


last_seen = read_json(STATE_FILE).get("last")  # {"track", "kind", "at", "song"}


def poll_once():
    """Ask Spotify what is playing. Returns seconds until the next check."""
    global last_seen
    tok = player_token()
    if not tok:
        return 15
    try:
        _, _, b = http("https://api.spotify.com/v1/me/player?additional_types=track,episode",
                       headers={"Authorization": "Bearer " + tok}, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return int(e.headers.get("Retry-After") or 10) + 1
        if e.code == 401:
            rec = read_json(PLAYER_FILE)
            rec["expires"] = 0
            write_json(PLAYER_FILE, rec, private=True)
            return 1
        raise
    t = int(time.time() * 1000)
    d = json.loads(b) if b else None
    item = (d or {}).get("item")
    if d and item and item.get("id"):
        kind = item.get("type", "track")
        state = "playing" if d.get("is_playing") else "paused"
        song = episode_song(item) if kind == "episode" else None
        now.update(state=state, track=item["id"], kind=kind, progress=(d.get("progress_ms") or 0) / 1000,
                   duration=(item.get("duration_ms") or 0) / 1000, fetched=t, song=song, lastAt=None)
        changed = not last_seen or last_seen.get("track") != item["id"]
        last_seen = {"track": item["id"], "kind": kind, "at": t, "song": song}
        if changed:
            write_json(STATE_FILE, {"last": last_seen})
        return 2 if state == "playing" else 5
    # Nothing is playing on any device: show the last song the server saw.
    if now["state"] in ("playing", "paused") and last_seen:
        write_json(STATE_FILE, {"last": last_seen})
    now.update(state="idle", fetched=t, progress=0)
    if last_seen:
        now.update(track=last_seen["track"], kind=last_seen["kind"], song=last_seen.get("song"),
                   lastAt=last_seen["at"], duration=0)
    return 15


def poller():
    while True:
        wait = 15
        try:
            # Only ask Spotify while someone has the display open (or nothing is known yet).
            if time.time() - viewers["last"] < 120 or not now["fetched"]:
                if read_cookie() and player_status() in ("connecting", "needs_approval") and signin.AVAILABLE:
                    auto_connect()
                wait = poll_once()
        except Exception as e:
            log("poll failed:", repr(e)[:200])
            wait = 10
        wake.wait(wait)
        wake.clear()


browser = signin.SignIn(PROFILE_DIR, save_cookie, on_redirect, player_authorize_url, log)


# ---------- web-player token ----------

def totp(secret, t):
    key = "".join(str(b ^ ((i % 33) + 9)) for i, b in enumerate(secret)).encode()
    mac = hmac.new(key, struct.pack(">Q", int(t // 30)), hashlib.sha1).digest()
    o = mac[-1] & 15
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 1000000).zfill(6)


def server_time():
    try:
        _, h, _ = http("https://open.spotify.com/", method="HEAD", timeout=10)
        return email.utils.parsedate_to_datetime(h.get("Date")).timestamp()
    except Exception:
        return time.time()


def refresh_secrets():
    try:
        _, _, b = http(SECRETS_URL, timeout=10)
        d = json.loads(b)
        for k, v in d.items():
            SECRETS[int(k)] = v
        log("secrets refreshed, newest version", max(SECRETS))
    except Exception as e:
        log("secret refresh failed:", e)


def fetch_token(cookie):
    for attempt in range(2):
        ver = max(SECRETS)
        code = totp(SECRETS[ver], server_time())
        headers = {"Referer": "https://open.spotify.com/", "Accept": "application/json", "App-Platform": "WebPlayer"}
        if cookie:
            headers["Cookie"] = "sp_dc=" + cookie
        for reason in ("transport", "init"):
            q = urllib.parse.urlencode({"reason": reason, "productType": "web-player",
                                        "totp": code, "totpServer": code, "totpVer": ver})
            try:
                _, _, b = http("https://open.spotify.com/api/token?" + q, headers=headers)
            except urllib.error.HTTPError as e:
                body = e.read()[:200].decode("utf8", "replace")
                log("token", reason, e.code, body)
                if "totpVer" in body:
                    break
                continue
            d = json.loads(b)
            if d.get("accessToken") and (not cookie or not d.get("isAnonymous")):
                return d["accessToken"], d.get("accessTokenExpirationTimestampMs", 0) / 1000, d.get("clientId")
        if attempt == 0:
            refresh_secrets()
    raise RuntimeError("could not get a web-player token" + (" (is the sp_dc cookie still valid?)" if cookie else ""))


def get_token(kind):
    """kind = 'user' (needs cookie) or 'anon'."""
    with lock:
        tok = tokens.get(kind)
        if tok and tok[1] - 60 > time.time():
            return tok[0]
    cookie = read_cookie() if kind == "user" else None
    if kind == "user" and not cookie:
        auth["needs_signin"] = True
        return None
    try:
        value, exp, cid = fetch_token(cookie)
    except RuntimeError:
        if kind != "user":
            raise
        # The cookie stopped working. Let the server's own browser renew it from its
        # saved profile (at most every 30 minutes); otherwise ask for a sign-in.
        fresh = None
        if signin.AVAILABLE and time.time() - auth["last_refresh"] > 1800:
            auth["last_refresh"] = time.time()
            try:
                fresh = browser.refresh_cookie()
            except Exception as e:
                log("cookie refresh failed:", repr(e)[:200])
        if fresh and fresh != cookie:
            save_cookie(fresh)
            log("cookie renewed from the browser profile")
            value, exp, cid = fetch_token(fresh)
        else:
            auth["needs_signin"] = True
            log("signed out of Spotify: sign in from the settings pane")
            return None
    auth["needs_signin"] = False
    with lock:
        tokens[kind] = (value, exp or time.time() + 1800)
        tokens["client_id"] = (cid, 0)
    log("got", kind, "token")
    return value


def get_client_token():
    with lock:
        if client_token.get("value") and client_token.get("expires", 0) > time.time():
            return client_token["value"]
    try:
        _, _, html = http("https://open.spotify.com/", timeout=15)
        m = re.search(rb'<script id="appServerConfig" type="text/plain">([^<]+)</script>', html)
        version = json.loads(base64.b64decode(m.group(1)))["clientVersion"] if m else "1.2.70.0"
        cid = (tokens.get("client_id") or ("d8a5ed958d274c2e8ee717e6a4b0971d", 0))[0]
        body = json.dumps({"client_data": {"client_version": version, "client_id": cid, "js_sdk_data": {
            "device_brand": "unknown", "device_model": "unknown", "os": "windows", "os_version": "NT 10.0",
            "device_id": hashlib.md5(cid.encode()).hexdigest(), "device_type": "computer"}}}).encode()
        _, _, b = http("https://clienttoken.spotify.com/v1/clienttoken", method="POST", data=body,
                       headers={"Accept": "application/json", "Content-Type": "application/json"})
        g = json.loads(b)["granted_token"]
        with lock:
            client_token["value"] = g["token"]
            client_token["expires"] = time.time() + int(g.get("expires_after_seconds", 3600)) - 60
        return g["token"]
    except Exception as e:
        log("client-token failed (continuing without):", e)
        return None


# ---------- Pathfinder ----------

def refresh_hashes():
    try:
        _, _, html = http("https://open.spotify.com/", timeout=15)
        m = re.search(rb'https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[0-9a-f]+\.js', html)
        _, _, js = http(m.group(0).decode(), timeout=30)
        for name, h in re.findall(rb'"([A-Za-z0-9_]+)",\s*"(?:query|mutation)",\s*"([0-9a-f]{64})"', js):
            if name.decode() in HASHES:
                HASHES[name.decode()] = h.decode()
        log("query hashes refreshed")
    except Exception as e:
        log("hash refresh failed:", e)


def pathfinder(op, variables, token):
    for attempt in range(2):
        body = json.dumps({"operationName": op, "variables": variables,
                           "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASHES[op]}}}).encode()
        headers = {"Authorization": "Bearer " + token, "App-Platform": "WebPlayer",
                   "Content-Type": "application/json;charset=UTF-8"}
        ct = get_client_token()
        if ct:
            headers["client-token"] = ct
        _, _, b = http("https://api-partner.spotify.com/pathfinder/v2/query", method="POST", data=body, headers=headers)
        d = json.loads(b)
        errs = json.dumps(d.get("errors") or "")
        if "PersistedQueryNotFound" in errs or "persisted query" in errs.lower():
            if attempt == 0:
                refresh_hashes()
                continue
        return d
    return {}


def argb(n):
    n = int(n) & 0xFFFFFFFF
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255]


def biggest(sources, key="width"):
    s = sorted(sources or [], key=lambda x: x.get(key) or x.get("maxWidth") or 0)
    return s[-1]["url"] if s else None


def build_song(track_id):
    anon = get_token("anon")
    user = get_token("user")
    uri = "spotify:track:" + track_id
    t = pathfinder("getTrack", {"uri": uri}, user or anon).get("data", {}).get("trackUnion", {})
    if not t.get("name"):
        raise RuntimeError("Spotify returned no track for " + track_id)
    album = t.get("albumOfTrack", {})
    artists = t.get("firstArtist", {}).get("items", []) + t.get("otherArtists", {}).get("items", [])
    song = {
        "id": track_id,
        "title": t["name"],
        "artists": [a.get("profile", {}).get("name") for a in artists],
        "album": album.get("name"),
        "duration": (t.get("duration", {}).get("totalMilliseconds") or 0) / 1000,
        "art": biggest(album.get("coverArt", {}).get("sources")),
        "banner": None,
        "canvas": None,
        "lyrics": None,
        "colors": None,
        "loggedIn": bool(user),
    }
    if artists:
        a = pathfinder("queryArtistOverview", {"uri": artists[0].get("uri"), "locale": "", "preReleaseV2": False,
                                               "includeMusicVideoPreviews": False}, anon)
        au = a.get("data", {}).get("artistUnion", {})
        song["banner"] = (biggest((au.get("headerImage") or {}).get("data", {}).get("sources"), "maxWidth")
                          or biggest((au.get("visuals") or {}).get("avatarImage", {}).get("sources")))
    if user:
        c = pathfinder("canvas", {"trackUri": uri}, user)
        song["canvas"] = ((c.get("data", {}).get("trackUnion") or {}).get("canvas") or {}).get("url")
        try:
            _, _, b = http("https://spclient.wg.spotify.com/color-lyrics/v2/track/%s?format=json&vocalRemoval=false&market=from_token" % track_id,
                           headers={"Authorization": "Bearer " + user, "App-Platform": "WebPlayer", "Accept": "application/json"})
            ly = json.loads(b)
            lines = ly.get("lyrics", {}).get("lines", [])
            synced = ly.get("lyrics", {}).get("syncType") == "LINE_SYNCED"
            song["lyrics"] = {
                "mode": "synced" if synced else "plain",
                "lines": [[int(l.get("startTimeMs") or 0) / 1000, (l.get("words") or "").strip()]
                          for l in lines],
            }
            col = ly.get("colors") or {}
            if "background" in col:
                song["colors"] = {"background": argb(col["background"]), "text": argb(col.get("text", -16777216)),
                                  "highlight": argb(col.get("highlightText", -1))}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log("lyrics", e.code)
    return song


# ---------- HTTP ----------

PAGE_HEAD = (b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
             b'<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">'
             # Full screen from the home screen, like the dashboard: iOS reads these only in <head>.
             b'<title>Now Playing</title>'
             b'<meta name="apple-mobile-web-app-capable" content="yes">'
             b'<meta name="mobile-web-app-capable" content="yes">'
             b'<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
             b'<meta name="apple-mobile-web-app-title" content="Now Playing">'
             b'<meta name="theme-color" content="#000000">'
             b'<link rel="apple-touch-icon" href="icon-180.png">'
             b'<link rel="apple-touch-icon" sizes="152x152" href="icon-152.png">'
             b'<link rel="apple-touch-icon" sizes="180x180" href="icon-180.png">'
             b'<style>:root{padding-top:env(safe-area-inset-top,0px);'
             b'padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px/1.5 system-ui,sans-serif}'
             b'img{max-width:100%}[hidden]{display:none!important}</style></head><body>')

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]) and "/api/signin/" not in str(args[0]):
            log("http", fmt % args)

    def end_headers(self):
        if self.path.endswith(".html") or self.path in ("/", ""):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def status(self):
        return {"signedIn": bool(read_cookie()) and not auth["needs_signin"],
                "needsSignIn": auth["needs_signin"] or not read_cookie(),
                "signInAvailable": signin.AVAILABLE,
                "signingIn": browser.active,
                "player": player_status()}

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send_json(400, {"error": "bad request"})
        try:
            if u.path == "/api/signin/start":
                browser.start(player_only=bool(body.get("player")) and bool(read_cookie()))
            elif u.path == "/api/signin/input":
                t = body.get("type")
                if t == "click":
                    browser.click(body["x"], body["y"])
                elif t == "text":
                    browser.text(body.get("text", ""))
                elif t == "key":
                    browser.key(body.get("key", ""))
                elif t == "scroll":
                    browser.scroll(body.get("dy", 0))
                elif t == "back":
                    browser.back()
                else:
                    return self.send_json(400, {"error": "unknown input"})
            elif u.path == "/api/signin/cancel":
                browser.cancel()
            elif u.path == "/api/signout":
                forget_cookie()
                browser.forget()
                for f in (PLAYER_FILE, STATE_FILE):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                player["status"] = "none"
                auth["needs_signin"] = True
            else:
                return self.send_json(404, {"error": "not found"})
        except Exception as e:
            return self.send_json(503, {"error": str(e)[:200]})
        return self.send_json(200, self.status())

    def send_page(self):
        # web/index.html is written as a page body (the artifact host adds the document
        # around it), so add the same document shell here.
        with open(os.path.join(WEB, "index.html"), "rb") as f:
            body = f.read()
        b = PAGE_HEAD + body + b"</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send_page()
        if u.path == "/api/now":
            viewers["last"] = time.time()
            stale = time.time() * 1000 - now["fetched"] > 8000
            if stale:
                wake.set()
            out = dict(now)
            out.update(server=int(time.time() * 1000), player=player_status())
            return self.send_json(200, out)
        if u.path == "/api/status":
            if not read_cookie():
                auth["needs_signin"] = True
            return self.send_json(200, self.status())
        if u.path == "/api/signin/frame":
            try:
                jpg, done = browser.frame()
            except Exception as e:
                return self.send_json(503, {"error": str(e)[:200]})
            if not jpg:
                return self.send_json(409, {"error": "no sign-in open"})
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Signed-In", "1" if done else "0")
            self.send_header("Access-Control-Expose-Headers", "X-Signed-In")
            self.end_headers()
            self.wfile.write(jpg)
            return
        if u.path == "/api/song":
            q = urllib.parse.parse_qs(u.query)
            tid = (q.get("track") or [DEFAULT_TRACK])[0]
            if not re.fullmatch(r"[A-Za-z0-9]{22}", tid):
                return self.send_json(400, {"error": "track must be a Spotify track id"})
            cached = song_cache.get(tid)
            if cached and (cached[0]["loggedIn"] or not read_cookie()) and time.time() - cached[1] < 6 * 3600:
                return self.send_json(200, cached[0])
            try:
                song = build_song(tid)
                song_cache[tid] = (song, time.time())
                return self.send_json(200, song)
            except Exception as e:
                log("song failed:", repr(e))
                return self.send_json(502, {"error": str(e)})
        return super().do_GET()


if __name__ == "__main__":
    log("serving", WEB, "on :%d" % PORT, "| cookie:", "found" if read_cookie() else "missing (" + SPDC_FILE + ")",
        "| player:", player_status())
    threading.Thread(target=poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
