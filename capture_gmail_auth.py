#!/usr/bin/env python3
"""
One-time Gmail readonly OAuth capture.

Reads credentials.json (Gmail OAuth client, reused from the existing
auto-responder agent — copy it into this directory before running),
runs the browser-based OAuth flow with gmail.readonly scope only, and
writes token.json.

Re-run whenever token.json's refresh token gets revoked. Refresh tokens
usually persist long-term; expect monthly or less.

Usage:
    python capture_gmail_auth.py
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent / "token.json"


def main() -> int:
    if not CREDENTIALS_FILE.exists():
        print(f"✗ {CREDENTIALS_FILE} not found.", file=sys.stderr)
        print("  Copy credentials.json from your existing auto-responder agent:", file=sys.stderr)
        print("      cp /path/to/auto-responder/credentials.json .", file=sys.stderr)
        return 1

    print("Opening browser for Google sign-in.")
    print(f"Scope requested: {SCOPES[0]} (read-only — cannot send or modify mail).")
    print()
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"✓ Saved {TOKEN_FILE}")
    print()
    print("Next steps:")
    print("  • Local runs are ready to go.")
    print("  • For GitHub Actions, base64 each file and paste into repo secrets:")
    print("      base64 -i credentials.json | pbcopy   # → CREDENTIALS_B64")
    print("      base64 -i token.json       | pbcopy   # → TOKEN_READONLY_B64")
    return 0


if __name__ == "__main__":
    sys.exit(main())
