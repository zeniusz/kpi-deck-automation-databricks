"""Databricks browser-session status + in-app login, so the app is fully self-service.

Dashboard screenshots reuse a saved SSO session (`.databricks_session.json`). SSO+MFA itself cannot
be automated, but the user should not have to know that a terminal script exists — so:

  * `session_state()`  — is the saved session present / stale / actually still authenticated?
  * `launch_login()`   — opens the real login browser FROM the app (a detached process, because a
                         Streamlit run must not block on `input()`), then the user signs in and the
                         session saves itself.

`_login_auto.py`-style saving: we launch a helper that closes and saves on its own once the user is
authenticated, instead of waiting for an Enter keypress in a terminal nobody is watching.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
SESSION_PATH = BASE / ".databricks_session.json"
HELPER_PATH = BASE / "_login_helper.py"
FLAG_PATH = BASE / ".login_result.json"


@dataclass
class SessionState:
    exists: bool
    saved_at: str | None = None
    age_minutes: float | None = None
    verified: bool | None = None      # None = not checked (needs a browser round-trip)
    message: str = ""

    @property
    def looks_stale(self) -> bool:
        """SSO sessions die within hours; anything older than a day is almost certainly dead."""
        return self.age_minutes is not None and self.age_minutes > 60 * 12


def session_state() -> SessionState:
    if not SESSION_PATH.exists():
        return SessionState(False, message="No saved Databricks session yet.")
    try:
        mt = datetime.fromtimestamp(SESSION_PATH.stat().st_mtime)
        age = (datetime.now() - mt).total_seconds() / 60
        n = len(json.loads(SESSION_PATH.read_text()).get("cookies", []))
        st = SessionState(True, mt.strftime("%Y-%m-%d %H:%M"), age, None,
                          f"Session saved {mt:%Y-%m-%d %H:%M} ({n} cookies).")
        if st.looks_stale:
            st.message += " This is more than 12h old — SSO sessions expire, so it may be dead."
        return st
    except Exception as exc:  # noqa: BLE001
        return SessionState(False, message=f"Session file unreadable ({exc}).")


def verify_session(timeout_ms: int = 45_000) -> tuple[bool, str]:
    """Actually load a dashboard headlessly and see whether Databricks redirects us to login.

    This is the only reliable check — a present file with unexpired cookies can still be dead.
    """
    if not SESSION_PATH.exists():
        return False, "No saved session."
    try:
        from playwright.sync_api import sync_playwright

        from dashboard_capture import HIDE_OVERLAYS_INIT_JS, LAUNCH_ARGS
        from kpis import DATABRICKS_DASHBOARDS

        url = next(iter(DATABRICKS_DASHBOARDS.values()))
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            try:
                ctx = b.new_context(
                    storage_state=str(SESSION_PATH),
                    viewport={"width": 1280, "height": 800},
                    ignore_https_errors=True,
                )
                ctx.add_init_script(HIDE_OVERLAYS_INIT_JS)
                pg = ctx.new_page()
                pg.set_default_timeout(timeout_ms)
                pg.goto(url, wait_until="domcontentloaded")
                pg.wait_for_timeout(9_000)
                landed = pg.url.lower()
            finally:
                b.close()
        if any(k in landed for k in ("login", "aad", "microsoftonline")):
            return False, "Session expired — Databricks redirected to the login page."
        return True, "Session is valid; dashboard screenshots will work."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not verify the session ({str(exc)[:110]})."


HELPER_SOURCE = '''"""Auto-saving Databricks login helper (launched by the app, not run by hand).

Opens a real browser window, waits until the workspace is actually authenticated, then saves the
session and closes itself. No terminal Enter needed — that is the whole point: the app can start
this and poll for the result.
"""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from dashboard_login import REAL_UA, STEALTH_ARGS, WORKSPACE_URL  # noqa: E402

SESSION_PATH = BASE / ".databricks_session.json"
FLAG_PATH = BASE / ".login_result.json"
TIMEOUT_S = 600  # 10 minutes to complete SSO + MFA


def _result(ok, msg: str) -> None:
    """ok=True signed in, False failed, None cancelled (window closed — not an error)."""
    FLAG_PATH.write_text(json.dumps({"ok": ok, "message": msg}))


def main() -> None:
    try:
        with sync_playwright() as p:
            browser = None
            for channel in ("chrome", "msedge"):
                try:
                    browser = p.chromium.launch(headless=False, channel=channel, args=STEALTH_ARGS)
                    break
                except Exception:
                    continue
            if browser is None:
                browser = p.chromium.launch(headless=False, args=STEALTH_ARGS)
            ctx = browser.new_context(user_agent=REAL_UA, viewport=None, ignore_https_errors=True)
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = ctx.new_page()
            page.goto(WORKSPACE_URL, wait_until="domcontentloaded")

            # Poll until we are clearly inside the workspace (not on a login/SSO page), then save.
            waited = 0
            while waited < TIMEOUT_S:
                # Closing the window is a NORMAL way to abandon a sign-in, and it makes every
                # Playwright call raise "Target page, context or browser has been closed". That was
                # surfacing as a raw "Login failed: Page.wait_for_timeout: ..." stack message. Treat a
                # closed window as a cancellation, not a failure.
                try:
                    page.wait_for_timeout(2_000)
                    url = page.url.lower()
                except Exception:
                    _result(None, "Sign-in window closed — nothing was changed. "
                                  "The previous session (if any) is untouched.")
                    return
                waited += 2
                on_login = any(k in url for k in ("login", "aad", "microsoftonline", "signin"))
                if not on_login and _WORKSPACE_HOST in url:
                    page.wait_for_timeout(4_000)  # let cookies settle
                    ctx.storage_state(path=str(SESSION_PATH))
                    browser.close()
                    _result(True, "Signed in — session saved.")
                    return
            _result(False, "Timed out waiting for sign-in (10 minutes).")
            browser.close()
    except Exception as exc:
        # Keep the raw text out of the UI: a Playwright traceback tells the user nothing about what to
        # do. A closed window is a cancellation; anything else gets a short, actionable line.
        text = str(exc)
        if "has been closed" in text or "Target page" in text:
            _result(None, "Sign-in window closed — nothing was changed.")
        elif "Missing X server" in text or "DISPLAY" in text:
            _result(False, "No display available to open a browser window. Run "
                           "`.venv/bin/python dashboard_login.py` in a terminal instead.")
        elif "Executable doesn't exist" in text or "playwright install" in text:
            _result(False, "The browser used for sign-in is not installed. Run "
                           "`.venv/bin/playwright install chromium`.")
        else:
            _result(False, f"Sign-in could not start: {text.splitlines()[0][:130]}")


if __name__ == "__main__":
    main()
'''


def ensure_helper() -> Path:
    """Write the helper script if missing/outdated so the app can launch it."""
    if not HELPER_PATH.exists() or HELPER_PATH.read_text() != HELPER_SOURCE:
        HELPER_PATH.write_text(HELPER_SOURCE)
    return HELPER_PATH


def _gui_env() -> dict[str, str] | None:
    """An environment with a usable DISPLAY, or None if this machine has no GUI.

    The SSO login needs a REAL browser window — MFA cannot be automated — so it cannot run headless.
    But when the app runs as a systemd service, the service environment has no `DISPLAY` at all
    (`systemctl --user show-environment` is empty), and the child process inherits that: Playwright
    then fails with "Missing X server or $DISPLAY … The platform failed to initialize."

    On WSL, WSLg provides the display and the socket is at /tmp/.X11-unix/X0 even when the variable is
    unset, so DISPLAY is reconstructed from the socket rather than assumed.
    """
    env = dict(os.environ)
    if env.get("DISPLAY") and Path("/tmp/.X11-unix").exists():
        return env
    socks = sorted(Path("/tmp/.X11-unix").glob("X*")) if Path("/tmp/.X11-unix").is_dir() else []
    if socks:
        env["DISPLAY"] = f":{socks[0].name[1:]}"
        # WSLg also needs these for hardware-accelerated/Wayland paths; harmless elsewhere.
        if Path("/mnt/wslg").exists():
            env.setdefault("WAYLAND_DISPLAY", "wayland-0")
            env.setdefault("XDG_RUNTIME_DIR", "/mnt/wslg/runtime-dir")
        return env
    return None


def launch_login() -> tuple[bool, str]:
    """Start the login browser as a DETACHED process and return immediately.

    Streamlit reruns the script top-to-bottom, so we must not block. The helper saves the session
    on its own; the page polls `login_result()`.
    """
    env = _gui_env()
    if env is None:
        return False, (
            "No graphical display available, so a sign-in window cannot be opened. SSO + MFA needs a "
            "real browser. Run this in a terminal instead:\n\n"
            "    cd ~/powerpoint_data_refresher_v2 && .venv/bin/python dashboard_login.py\n\n"
            "The saved session is then picked up here — press “Check session”."
        )
    try:
        ensure_helper()
        FLAG_PATH.unlink(missing_ok=True)
        subprocess.Popen(
            [sys.executable, str(HELPER_PATH)],
            cwd=str(BASE),
            env=env,               # the service has no DISPLAY; pass a working one explicitly
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, (f"A browser window is opening on display {env['DISPLAY']} — sign in with "
                      "SSO + MFA, then press “Check session”.")
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not start the login browser ({exc})."


def login_result() -> dict | None:
    """The helper's outcome, once it has finished. None while still in progress."""
    if not FLAG_PATH.exists():
        return None
    try:
        return json.loads(FLAG_PATH.read_text())
    except Exception:  # noqa: BLE001
        return None
