"""A headless Chromium inside the server, used to sign in to Spotify and to keep the
sp_dc cookie fresh.

The settings pane on the display shows this browser as a live picture and sends it
taps and typing. Once Spotify sets sp_dc, the server saves it and closes the browser.
The profile is kept, so when the cookie stops working later the server can reopen the
profile, let open.spotify.com renew the session, and read the new cookie without asking.

Playwright is not thread-safe, so every browser call runs on one worker thread.
"""

import os
import queue
import threading
import time

try:
    from playwright.sync_api import sync_playwright
    AVAILABLE = True
except ImportError:  # local runs without Playwright: sign-in is simply unavailable
    sync_playwright = None
    AVAILABLE = False

LOGIN_URL = "https://accounts.spotify.com/en/login?continue=https%3A%2F%2Fopen.spotify.com%2F"
HOME_URL = "https://open.spotify.com/"
# Spotify sends the player-access code here (register this redirect URI on your
# developer app). Nothing listens on it: the browser's request is caught and answered here.
REDIRECT_PREFIX = "http://127.0.0.1:8899/callback"
VIEW_W, VIEW_H = 400, 720
IDLE_CLOSE = 300  # close the browser after five minutes without use


class SignIn:
    def __init__(self, profile_dir, on_cookie, on_redirect, next_url, log):
        self.profile_dir = profile_dir
        self.on_cookie = on_cookie      # called with the sp_dc value when one appears
        self.on_redirect = on_redirect  # called with the redirect URL; True once the player is connected
        self.next_url = next_url        # the player-access address, or None when already connected
        self.log = log
        self.done = False               # signed in AND the player is connected
        self.q = queue.Queue()
        self.pw = None
        self.ctx = None
        self.page = None
        self.last_use = 0
        self.signed_in = False          # sp_dc saved during the current sign-in
        self.active = False             # a sign-in view is open
        if AVAILABLE:
            threading.Thread(target=self._loop, daemon=True).start()

    # ---------- worker thread ----------

    def _loop(self):
        while True:
            try:
                fn, done, box = self.q.get(timeout=1)
            except queue.Empty:
                if self.ctx and not self.active and time.time() - self.last_use > 20:
                    self._close()
                elif self.ctx and time.time() - self.last_use > IDLE_CLOSE:
                    self.active = False
                    self._close()
                continue
            try:
                box["value"] = fn()
            except Exception as e:  # report to the caller, keep the worker alive
                box["error"] = e
                self.log("browser:", repr(e)[:200])
            finally:
                self.last_use = time.time()
                done.set()

    def _call(self, fn, timeout=45):
        if not AVAILABLE:
            raise RuntimeError("Playwright is not installed here")
        done, box = threading.Event(), {}
        self.q.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError("browser did not answer")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _open(self):
        if self.ctx:
            return
        os.makedirs(self.profile_dir, exist_ok=True)
        if not self.pw:
            self.pw = sync_playwright().start()
        # Real Google Chrome, drawn on the container's virtual screen (xvfb-run), with the
        # automation markers removed. Playwright's stripped-down headless Chromium is easy
        # to spot, and reCAPTCHA then served endless puzzles and Spotify refused the login.
        # Chrome keeps its own user agent so it matches its client hints.
        self.ctx = self.pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chrome",
            headless=False,
            viewport={"width": VIEW_W, "height": VIEW_H},
            device_scale_factor=2,
            locale="en-GB",
            timezone_id="Europe/London",
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                  "--window-size=%d,%d" % (VIEW_W, VIEW_H + 120), "--no-first-run", "--no-default-browser-check"],
        )
        # Follow pop-ups (Apple or Google sign-in windows) so they show in the pane.
        self.ctx.on("page", self._on_page)
        self.ctx.route(REDIRECT_PREFIX + "**", self._on_route)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.log("browser opened")

    def _on_route(self, route):
        ok = False
        try:
            ok = bool(self.on_redirect(route.request.url))
        except Exception as e:
            self.log("player connect failed:", repr(e)[:200])
        self.done = self.done or ok
        body = ("<body style='background:#121212;color:#fff;font:600 18px sans-serif;display:grid;place-items:center;height:90vh'>"
                + ("Connected. The display now follows Spotify." if ok else "Spotify did not connect. Try again from the pane.") + "</body>")
        route.fulfill(status=200, content_type="text/html", body=body)

    def _on_page(self, page):
        self.page = page
        self.log("browser switched to a new window")

    def _current(self):
        """The page to show: the newest window still open."""
        if self.page and not self.page.is_closed():
            return self.page
        open_pages = [p for p in self.ctx.pages if not p.is_closed()] if self.ctx else []
        self.page = open_pages[-1] if open_pages else None
        return self.page

    def _close(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        self.log("browser closed")

    def _cookie(self):
        for c in self.ctx.cookies([HOME_URL, "https://accounts.spotify.com/"]):
            if c.get("name") == "sp_dc" and c.get("value"):
                return c["value"]
        return None

    def _check(self):
        """Once Spotify has set sp_dc, save it and move on to connecting the player."""
        value = self._cookie()
        if value and not self.signed_in:
            self.signed_in = True
            self.on_cookie(value)
            self.log("sign-in finished, sp_dc saved")
            url = self.next_url()
            if url:
                self._current().goto(url, wait_until="domcontentloaded")
            else:
                self.done = True
        return value

    # ---------- what the server calls ----------

    def start(self, player_only=False):
        """Open the sign-in view: Spotify's login page, or (already signed in) straight
        to the page where Spotify asks to let the display see what is playing."""
        def go():
            self._open()
            self.active = True
            self.done = False
            url = self.next_url() if player_only else None
            self.signed_in = bool(url)
            self._current().goto(url or LOGIN_URL, wait_until="domcontentloaded")
            return True
        return self._call(go)

    def connect(self, url, wait=8):
        """Try to connect the player without anyone watching. Spotify skips its question
        when the app was allowed before; returns True if the code arrived in time."""
        def go():
            self._open()
            self.done = False
            self.signed_in = True
            self._current().goto(url, wait_until="domcontentloaded")
            end = time.time() + wait
            while not self.done and time.time() < end:
                self._current().wait_for_timeout(250)
            return self.done
        return self._call(go, timeout=wait + 40)

    def frame(self):
        def shot():
            page = self._current()
            if not page:
                return None, False
            self._check()
            return page.screenshot(type="jpeg", quality=70, caret="initial", timeout=10000), self.done
        return self._call(shot, timeout=20)

    def click(self, x, y):
        return self._call(lambda: self._current() and self._current().mouse.click(float(x), float(y)))

    def text(self, t):
        return self._call(lambda: self._current() and self._current().keyboard.insert_text(str(t)[:500]))

    KEYS = {"Enter", "Backspace", "Tab", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Delete"}

    def key(self, k):
        if k not in self.KEYS:
            raise ValueError("key not allowed")
        return self._call(lambda: self._current() and self._current().keyboard.press(k))

    def scroll(self, dy):
        return self._call(lambda: self._current() and self._current().mouse.wheel(0, max(-2000, min(2000, float(dy)))))

    def back(self):
        return self._call(lambda: self._current() and self._current().go_back(wait_until="domcontentloaded"))

    def cancel(self):
        def stop():
            self.active = False
            self._close()
        return self._call(stop)

    def refresh_cookie(self):
        """Reopen the saved profile and read a fresh sp_dc. Returns it, or None if the
        profile is signed out too."""
        def go():
            self._open()
            self.page.goto(HOME_URL, wait_until="domcontentloaded")
            self.page.wait_for_timeout(5000)
            value = self._cookie()
            if not self.active:
                self._close()
            return value
        return self._call(go, timeout=60)

    def forget(self):
        """Sign out: close the browser and delete its profile."""
        def wipe():
            self.active = False
            self._close()
            import shutil
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        return self._call(wipe)
