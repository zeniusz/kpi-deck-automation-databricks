"""Auto-saving Databricks login helper (launched by the app, not run by hand).

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
                if not on_login and urlparse(WORKSPACE_URL).netloc in url:
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
