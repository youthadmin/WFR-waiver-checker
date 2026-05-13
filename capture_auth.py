#!/usr/bin/env python3
"""
One-time interactive capture of PCO Playwright auth state.

Opens a headed Chromium window, lets you log in to Planning Center (Google
SSO is fine), and saves the authenticated browser storage to auth_state.json.
Verifies the saved state works against a real signup URL before declaring
success.

Usage:
    python capture_auth.py

Re-run whenever auth_state.json expires (typically monthly).
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

AUTH_STATE_PATH = Path(__file__).parent / "auth_state.json"
LOGIN_URL = "https://registrations.planningcenteronline.com/"
VERIFY_URL = "https://registrations.planningcenteronline.com/signups/3526683/attendees"


def main() -> int:
    print("=" * 70)
    print("PCO Playwright Auth Capture")
    print("=" * 70)
    print()
    print("This will open a Chromium window. In that window:")
    print("  1. Log in to Planning Center (Google SSO is fine).")
    print("  2. If you see 'Choose an Organization', click Mannahouse.")
    print("     (NOT Alive — it appears first but is the wrong org.)")
    print("  3. Confirm you can see the Registrations dashboard or a signup.")
    print()
    print("When you're done, come back to this terminal and press Enter.")
    print()
    input("Press Enter to launch the browser… ")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print()
        print("Browser is open. Complete login in the Chromium window now.")
        print("Do NOT close the window. Press Enter HERE when login is done.")
        input("→ ")

        context.storage_state(path=str(AUTH_STATE_PATH))
        print(f"✓ Saved auth state to {AUTH_STATE_PATH}")
        browser.close()

    print()
    print("Verifying saved state against a real signup URL…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            storage_state=str(AUTH_STATE_PATH),
        )
        page = context.new_page()
        page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=30_000)
        final_url = page.url
        browser.close()

    redirect_markers = ("/login", "churchcenter.com", "id.planningcenteronline.com")
    if any(marker in final_url for marker in redirect_markers):
        print(f"✗ Verification failed — final URL was {final_url}")
        print("  Login was not completed or the wrong org was selected.")
        print("  Delete auth_state.json and re-run this script.")
        return 1

    print(f"✓ Verified — final URL: {final_url}")
    print()
    print("Auth state captured successfully.")
    print()
    print("Next steps:")
    print("  • Local runs are ready to go.")
    print("  • For GitHub Actions, base64 encode and paste as the")
    print("    AUTH_STATE_B64 repository secret:")
    print("        base64 -i auth_state.json | pbcopy")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
