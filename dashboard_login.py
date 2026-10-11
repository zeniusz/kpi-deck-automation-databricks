"""One-time interactive Databricks login for dashboard screenshots.

Company SSO + MFA cannot be automated, so you sign in ONCE by hand in a real browser
window and this script saves the resulting session (cookies + local storage). The capture
step (dashboard_capture.py) then reuses that saved session headlessly each month.

Run it from the project folder with a GUI available (WSLg provides this in WSL):

    .venv/bin/python dashboard_login.py

A Chromium window opens on the Databricks workspace. Complete the normal SSO + MFA login
until you can see a dashboard, then come back to the terminal and press Enter. The session
is written to .databricks_session.json (git-ignored).

Re-run this whenever capture starts failing with an authentication / login error — SSO
sessions expire after a while, and this refreshes them.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

from settings import WORKSPACE_URL as WORKSPACE_BASE_URL

# Any authenticated page in the workspace works; the SQL/dashboard host is reused here.
WORKSPACE_URL = WORKSPACE_BASE_URL + "/"
SESSION_PATH = Path(__file__).parent / ".databricks_session.json"


# Microsoft SSO (login.microsoftonline.com) sometimes renders blank in the plain bundled
# Chromium because it detects automation. These make the window look like a normal browser.
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--start-maximized",
    # WSLg's virtual GPU makes Chromium paint blank/white pages. Force software rendering.
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
    "--use-gl=swiftshader",
    "--in-process-gpu",
    "--disable-dev-shm-usage",
]
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _launch(p):
    """Prefer a real installed Chrome/Edge (best for Microsoft SSO); fall back to bundled."""
    for channel in ("chrome", "msedge"):
        try:
            return p.chromium.launch(headless=False, channel=channel, args=STEALTH_ARGS)
        except Exception:
            continue
    return p.chromium.launch(headless=False, args=STEALTH_ARGS)


def main() -> None:
    print("Opening a browser window for Databricks login...")
    print("Complete SSO + MFA until you can see the Databricks workspace / a dashboard.")
    print("If the Microsoft page looks blank, click reload (the circular arrow) once.")
    with sync_playwright() as p:
        browser = _launch(p)
        context = browser.new_context(
            user_agent=REAL_UA,
            viewport=None,  # use the real window size
            # Corporate firewall does TLS interception with its own root CA, which the
            # bundled browser doesn't trust (pages render blank / ERR_CERT_AUTHORITY_INVALID).
            # Accept it so SSO can load.
            ignore_https_errors=True,
        )
        # Hide the navigator.webdriver flag Microsoft checks for.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.goto(WORKSPACE_URL, wait_until="domcontentloaded")

        input(
            "\n>>> Finish logging in in the browser window, then press Enter here to save "
            "the session... "
        )

        context.storage_state(path=str(SESSION_PATH))
        browser.close()

    print(f"\nSession saved to {SESSION_PATH}")
    print("You can now turn on 'Dashboard screenshots' in the app.")


if __name__ == "__main__":
    main()
