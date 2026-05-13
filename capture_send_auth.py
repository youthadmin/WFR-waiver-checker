#!/usr/bin/env python3
"""
One-time Gmail send-scope OAuth capture.

Reads the same credentials.json the readonly script uses, runs a separate
OAuth dance with the gmail.send scope only, and writes token_send.json.
main.py uses this token to email the run summary (and re-auth alerts) to
NOTIFY_EMAIL.

The send scope is intentionally isolated from the readonly scope — a
compromised token can't do both jobs.

Re-run whenever token_send.json's refresh token gets revoked.

Usage:
    python capture_send_auth.py
"""

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent / "token_send.json"


def main() -> int:
    if not CREDENTIALS_FILE.exists():
        print(f"✗ {CREDENTIALS_FILE} not found.", file=sys.stderr)
        print("  Copy credentials.json from your existing auto-responder agent:", file=sys.stderr)
        print("      cp /path/to/auto-responder/credentials.json .", file=sys.stderr)
        return 1

    print("Opening browser for Google sign-in.")
    print(f"Scope requested: {SCOPES[0]} (send-only — cannot read mail).")
    print()
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"✓ Saved {TOKEN_FILE}")
    print()
    print("Next steps:")
    print("  • Local runs are ready to go.")
    print("  • For GitHub Actions, base64 and paste as TOKEN_SEND_B64 secret:")
    print("      base64 -i token_send.json | pbcopy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
