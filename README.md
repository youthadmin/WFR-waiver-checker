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
- **Fridays at 10:00 AM America/Los_Angeles** — one weekly run, the morning after Noah's Thursday email.

Cron line is `0 17 * * 5` (Fri 17:00 UTC = Fri 10:00 PDT). DST will shift the local fire time by an hour twice a year; see `HOSTING.md` for the manual swap recipe.

### Enable / disable the cron

- **Disable:** *Actions* tab → *WFR Waiver Sync* → ⋯ menu → *Disable workflow*. Or comment out the `schedule:` block in the workflow file and push.
- **Re-enable:** same place, *Enable workflow*.
- **Pause one week:** disable Wednesday, re-enable Friday after that week's Thursday window.

### Manual run

*Actions* tab → *WFR Waiver Sync* → *Run workflow*. The `dry_run` input lets you do an on-demand dry-run even when the repo variable is set to `false`, or vice versa.

## When the agent breaks

The agent emails you on every outcome. The subject line tells you the severity at a glance:

| Subject prefix | Exit | Meaning |
|---|---|---|
| `[Waiver sync] OK — …` | 0 | Clean run |
| `[Waiver sync] WARN — …` | 7 | Completed with write failures or a hard-cap halt |
| `Waiver sync paused: re-auth needed` | 4 | PCO session is dead — see *Refresh PCO auth* below |
| `Waiver sync HALTED — post-click state mismatch` | 5 | A click didn't take; possible PCO UI change — see *Investigate halts* below |
| `Waiver sync FAILED — uncaught exception` | 6 | Crash; the email body has the full traceback |

### Where to read the details

- **Run summary email** — first place to look. Lists every matched attendee, every skip, and every needs-review case with reasons.
- **Workflow run log** — GitHub → *Actions* → *WFR Waiver Sync* → click the failing run → expand the *Run waiver sync* step.
- **Run artifacts** — same run page, bottom of the summary, *Artifacts → run-<id>*. Download it; inside is `logs/YYYY-MM-DD-HHMM.log` (full per-run trace) and `audit/before/` + `audit/after/` (full-page screenshots taken around each click). Retention: 30 days.
- **Local runs** — the same `logs/` and `audit/` paths get populated when you run `main.py` or `test_one_attendee.py` on your laptop.

### Refresh PCO auth (most common failure)

When you see *Waiver sync paused: re-auth needed*:

```bash
cd ~/Documents/Agents/waiver-sync
python capture_auth.py                       # interactive, headed Chromium
base64 -i auth_state.json | pbcopy
```

Paste into the `AUTH_STATE_B64` secret in repo settings, then trigger a manual `workflow_dispatch` run (with `dry_run: true`) to confirm the new session works. Once that's green, the next scheduled run will pick up the fresh auth.

Same pattern for Gmail tokens if they ever expire (much rarer — Gmail refresh tokens are long-lived):

```bash
python capture_gmail_auth.py    # → TOKEN_READONLY_B64
python capture_send_auth.py     # → TOKEN_SEND_B64
```

### Investigate halts (post-click mismatch)

When you see *Waiver sync HALTED — post-click state mismatch*, PCO accepted the click but the state read-back didn't confirm. Two likely causes:

1. **PCO UI changed** — selector is now hitting the wrong control or missing the right one. This is the higher-stakes case.
2. **Network blip** — the PATCH dropped between click and re-read. Re-run will succeed.

Download the run artifact and compare `audit/before/<timestamp>_<attendee_id>.png` and `audit/after/...` for the attendee that triggered the halt. If the *after* shot shows the box visibly checked, it was a verification race — retry. If *after* looks identical to *before*, the selector logic in `pco_writer.py` needs review (`.filter(has_text=…).filter(has=…).first` is the chain to inspect).

### Order of operations for common failures

| Symptom | First action |
|---|---|
| Re-auth needed email | Refresh PCO auth (above), then manual `workflow_dispatch` |
| Halt email | Download artifact, compare before/after screenshots, re-run if verification race |
| Crash email | Read the traceback in the email body; if it's transient, manual rerun; if it points to a module bug, fix locally and push |
| Summary shows lots of "needs manual review" | The waiver names in the XLSX don't line up with PCO attendees — look for typos, name changes, or registrations that haven't synced yet |
| No summary email at all on a Friday | Check the workflow run page; if Gmail send failed, GitHub Actions' built-in workflow-failure email kicks in as the second layer |
