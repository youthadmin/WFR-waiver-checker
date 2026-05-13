# waiver-sync

Reads the weekly "WFR Waiver Form" email from Young Life's Washington Family Ranch (an XLSX of completed guest consent forms), matches each name against attendees in two PCO Registrations signups (Youth Camp + Dream Team), and toggles the "Washington Family Ranch Form" checkbox via Playwright. Designed for GitHub Actions; local-runnable.

## Architecture (hybrid, by necessity)

- **Reads** go through the PCO Registrations API. Faster, paginated, no browser.
- **Writes** go through Playwright with saved auth state. The Registrations Attendee vertex is API-read-only (see `API_NOTES.md`), so the only way to flip the waiver checkbox is the browser.
- Two Gmail OAuth tokens: a readonly token for fetching the waiver email, a send-scoped token for emailing the run summary.

## Requirements

- Python 3.11+
- A PCO Personal Access Token with read access to Registrations
- A Gmail OAuth client (`credentials.json`, reused from existing auto-responder agent)
- A captured PCO Playwright auth state (`auth_state.json`, from `capture_auth.py`)
- A captured Gmail send token (`token_send.json`, from `capture_send_auth.py`)

## Setup

1. Clone and install:
   ```bash
   git clone https://github.com/youthadmin/WFR-waiver-checker.git
   cd WFR-waiver-checker
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

2. Configure local secrets:
   ```bash
   cp .env.example .env
   # fill in PCO_CLIENT_ID and PCO_SECRET from ~/Documents/Agents/pco-automation/.env
   ```

3. Copy Gmail OAuth client credentials from the auto-responder agent:
   ```bash
   cp /path/to/auto-responder/credentials.json .
   ```

4. One-time auth captures (each writes its own JSON; all three are gitignored):
   ```bash
   python capture_auth.py        # → auth_state.json     (PCO Playwright)
   python capture_gmail_auth.py  # → token.json          (Gmail readonly)
   python capture_send_auth.py   # → token_send.json     (Gmail send)
   ```

## Running

- **List all PCO attendees** (read-only, no auth state required):
  ```bash
  python test_one_attendee.py
  ```
- **Single-attendee Playwright dry-run** (headed, no click):
  ```bash
  python test_one_attendee.py "Mayuki Corrigan"
  ```
- **Single-attendee LIVE click** (one attendee, real write):
  ```bash
  python test_one_attendee.py "Mayuki Corrigan" --live
  ```
- **Full pipeline, dry-run:**
  ```bash
  DRY_RUN=true python main.py
  ```
- **Full pipeline, LIVE:**
  ```bash
  DRY_RUN=false python main.py
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

## GitHub Actions setup

### Required repository secrets

Set these once in **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Source |
|--------|--------|
| `PCO_CLIENT_ID` | PCO Personal Access Token client ID |
| `PCO_SECRET` | PCO Personal Access Token secret |
| `AUTH_STATE_B64` | `base64 -i auth_state.json` after running `capture_auth.py` |
| `CREDENTIALS_B64` | `base64 -i credentials.json` |
| `TOKEN_READONLY_B64` | `base64 -i token.json` after `capture_gmail_auth.py` |
| `TOKEN_SEND_B64` | `base64 -i token_send.json` after `capture_send_auth.py` |

On macOS pipe directly to clipboard:
```bash
base64 -i auth_state.json | pbcopy
```

### Repository variable

Set in **Settings → Secrets and variables → Actions → Variables**:

| Variable | Purpose |
|----------|---------|
| `DRY_RUN` | `"true"` (default) or `"false"`. Manual `workflow_dispatch` runs can override per-run. |

Leave `DRY_RUN=true` for at least the first two scheduled runs. Inspect the summary emails and the run artifacts (logs + before/after screenshots) before flipping to `"false"`.

### Cron schedule

Defined in `.github/workflows/waiver-sync.yml`:
- Thursdays 08:00–22:00 America/Los_Angeles, every 30 min (29 fires)
- Friday 07:00 America/Los_Angeles catch-up (1 fire)

Cron lines are in UTC. They're correct for PDT; see `HOSTING.md` for the manual swap needed during PST months (mid-Nov to mid-Mar).

### Enable / disable the cron

- **Disable:** *Actions* tab → *WFR Waiver Sync* → ⋯ menu → *Disable workflow*. Or comment out the `schedule:` block in the workflow file and push.
- **Re-enable:** same place, *Enable workflow*.
- **Pause one week:** disable Wednesday, re-enable Friday after that week's Thursday window.

### Manual run

*Actions* tab → *WFR Waiver Sync* → *Run workflow*. The `dry_run` input lets you do an on-demand dry-run even when the repo variable is set to `false`, or vice versa.

## Refreshing expired auth

PCO sessions usually last about a month; Gmail OAuth refresh tokens persist longer. Expect a monthly chore.

**PCO Playwright session (`auth_state.json`):**
```bash
python capture_auth.py                       # interactive, headed
base64 -i auth_state.json | pbcopy           # paste into AUTH_STATE_B64 secret
```

**Gmail readonly token (`token.json`):**
```bash
python capture_gmail_auth.py
base64 -i token.json | pbcopy                # paste into TOKEN_READONLY_B64 secret
```

**Gmail send token (`token_send.json`):**
```bash
python capture_send_auth.py
base64 -i token_send.json | pbcopy           # paste into TOKEN_SEND_B64 secret
```

If a scheduled run finds the PCO session dead it emails `Waiver sync paused: re-auth needed` and exits cleanly without retrying. That email is your signal to run `capture_auth.py` and update the secret.
