# waiver-sync

Reads the weekly Washington Square Ranch waiver email from Gmail, matches each name against attendees in two PCO Registrations signups (Youth Camp + Dream Team), and toggles the "Washington Family Ranch Form" checkbox via Playwright. Designed for GitHub Actions; local-runnable.

## Architecture (hybrid, by necessity)

- **Reads** go through the PCO Registrations API. Faster, paginated, no browser.
- **Writes** go through Playwright with saved auth state. The Registrations Attendee vertex is API-read-only (see `API_NOTES.md`), so the only way to flip the waiver checkbox is the browser.
- Two Gmail OAuth tokens: a readonly token for fetching the waiver email, a send-scoped token for emailing the run summary.

## Status

🚧 In active build. See commit log for current module.

## Requirements

- Python 3.11+
- A PCO Personal Access Token with read access to Registrations
- A Gmail OAuth client (`credentials.json`, reused from existing auto-responder agent)
- A captured PCO Playwright auth state (`auth_state.json`, from `capture_auth.py`)
- A captured Gmail send token (`token_send.json`, from `capture_send_auth.py`)

## Setup

> Filled in as modules land. Sections marked TODO are not yet implemented.

1. Clone and install:
   ```bash
   git clone https://github.com/youthadmin/WFR-waiver-checker.git
   cd WFR-waiver-checker
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Configure secrets:
   ```bash
   cp .env.example .env
   # fill in PCO_CLIENT_ID and PCO_SECRET from ~/Documents/Agents/pco-automation/.env
   ```

3. Copy Gmail OAuth client credentials:
   ```bash
   cp /path/to/auto-responder/credentials.json .
   ```

4. Capture PCO Playwright auth (one-time, headed):
   ```bash
   python capture_auth.py
   # → opens Chromium, you log in via Google SSO, picks Mannahouse, dumps auth_state.json
   ```

5. Capture Gmail send-scope token (one-time):
   ```bash
   # TODO: capture_send_auth.py — written in step 8
   ```

## Running

- **Dry run, full pipeline:**
  ```bash
  DRY_RUN=true python main.py
  ```
- **Single attendee, manual test:**
  ```bash
  # TODO: test_one_attendee.py — written in step 7
  ```

## Environment variables

See `.env.example` for the full list with comments. Highlights:

| Var | Purpose |
|-----|---------|
| `PCO_CLIENT_ID` / `PCO_SECRET` | PCO API auth (Basic) |
| `WAIVER_SENDER` / `WAIVER_SUBJECT` | Gmail filter |
| `NOTIFY_EMAIL` | Where the run summary goes |
| `DRY_RUN` | `true` = read but don't click |
| `HEADLESS` | `false` for local Playwright debugging |
| `MATCH_THRESHOLD` | rapidfuzz min score (default 90) |
| `MAX_ATTENDEES_PER_RUN` | Hard cap; halts and alerts if exceeded |

## Hosting and notifications

See `HOSTING.md` for the GitHub Actions deployment plan, auth state rotation strategy, and the run-summary notification path.

## API and write-path notes

See `API_NOTES.md` for the Phase 1 investigation: what's read-only in the PCO API, the 403 wall on Attendee PATCH, and why Playwright is the write path.

## Enable / disable the cron

> TODO once `.github/workflows/waiver-sync.yml` exists (step 9).

## Refreshing expired auth

> TODO. Re-run `capture_auth.py` for PCO, `capture_send_auth.py` for Gmail send, then base64-update the matching repo secret.
