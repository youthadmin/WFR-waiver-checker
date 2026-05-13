# Hosting & notification plan

## Recommendation: GitHub Actions

Confirms your default. The Mac-on-desk and Pi/Mac-mini paths trade a quarterly cookie refresh for an uptime risk, and a missed Thursday hurts more than a 10-minute monthly auth rotation.

## The three options, head to head

| | (a) GitHub Actions cron | (b) Local Mac launchd | (c) Always-on Pi / Mac mini |
|---|---|---|---|
| **Uptime** | ✅ 99.99% — GitHub's problem | ⚠️ Mac must be awake at every trigger; sleep/lid kills it | ✅ Always on |
| **Auth state storage** | Repo secret (base64) | Local disk | Local disk |
| **Auth state rotation** | Re-capture locally, base64, paste into secret. ~10 min/month | Re-capture in place. ~5 min/month | Re-capture, scp to box. ~10 min/month |
| **Cost** | Free under GitHub's plan | Free (existing Mac) | Hardware + electricity |
| **Ops surface** | None — same plane as your other PCO agents | Mac admin (sleep/wake, launchd, log inspection) | New box to patch, monitor, harden |
| **Debug ergonomics** | Workflow logs in browser; artifacts for screenshots | `tail -f` locally | SSH in |
| **Secrets exposure** | Encrypted at rest, decrypted at runtime in a sandboxed VM | Filesystem perms; user-readable | Filesystem perms |

**Picking (a)** because it matches your existing fleet (`pco-automation`, `pco-incomplete-form-reminder`, `camp-update` are all GH Actions), so cognitive load stays low — one CI surface, one place to read logs, one secret store.

## Auth state on GitHub Actions

The trick is treating `auth_state.json` and `token_send.json` as ordinary base64 secrets that the workflow materializes at job start and deletes at job end.

```yaml
# rough sketch — actual workflow file lands in step 9
- name: Restore PCO auth state
  run: |
    echo "$AUTH_STATE_B64" | base64 -d > auth_state.json
  env:
    AUTH_STATE_B64: ${{ secrets.AUTH_STATE_B64 }}

- name: Restore Gmail tokens
  run: |
    echo "$CREDENTIALS_B64" | base64 -d > credentials.json
    echo "$TOKEN_READONLY_B64" | base64 -d > token.json
    echo "$TOKEN_SEND_B64" | base64 -d > token_send.json
  env:
    CREDENTIALS_B64: ${{ secrets.CREDENTIALS_B64 }}
    TOKEN_READONLY_B64: ${{ secrets.TOKEN_READONLY_B64 }}
    TOKEN_SEND_B64: ${{ secrets.TOKEN_SEND_B64 }}

- name: Run
  run: python main.py

- name: Scrub
  if: always()
  run: rm -f auth_state.json credentials.json token.json token_send.json
```

Local `auth_state.json` capture path:

```bash
python capture_auth.py                              # produces auth_state.json
base64 -i auth_state.json | pbcopy                  # macOS: copy to clipboard
# paste into Settings → Secrets → AUTH_STATE_B64
```

Same pattern for `token.json` and `token_send.json`.

## Expected cookie lifetime

PCO web sessions hold for around 30 days against a stable IP and User-Agent. Google SSO cookies inside the chain are shorter (≈14 days for active accounts) but the relevant cookies for `planningcenteronline.com` write actions don't depend on Google's session staying live — only the initial auth handshake does. Expect to re-capture roughly **monthly**. Acceptable per your note.

The bigger expiry risk isn't time, it's PCO server-side session invalidation (password change, logout from another device, "Sign out everywhere" in security settings). The agent must detect this and not retry blindly.

## Re-auth-needed detection

`pco_writer.py` checks every page load:

1. After `page.goto(attendee_url)`, read the final URL.
2. If it contains `/login` or `id.planningcenteronline.com` or the public Church Center domain, treat the session as dead.
3. Halt the entire run. **Never** attempt headless login.
4. Send the alert email with subject `Waiver sync paused: re-auth needed`. Body: which run, what time, link to the repo's Settings → Secrets, and the `capture_auth.py` rerun command.
5. Exit non-zero so the GH Actions run shows red and you get GitHub's failure notification too.

## Cron schedule

Single weekly run, Friday 10:00 AM Pacific:

```yaml
on:
  schedule:
    - cron: "0 17 * * 5"   # Fri 17:00 UTC = Fri 10:00 PDT
  workflow_dispatch:        # manual on-demand trigger from the Actions UI
```

PST/PDT caveat: when Pacific shifts to PST (first Sunday in November) the cron fires at 09:00 AM local instead of 10:00 AM. Manual swap to `0 18 * * 5` keeps the run at 10:00 AM PT year-round; swap back to `0 17 * * 5` when PDT resumes (second Sunday in March). Same manual-twice-a-year pattern as your other PCO agent.

## Run-summary notification — recommended path

Three options on the table:

| Option | Setup | Maintenance | Latency to you | Verdict |
|---|---|---|---|---|
| **Gmail API, separate send-scoped token** | One-time `capture_send_auth.py` + `token_send.json` secret | Same monthly rotation cycle as auth state | Immediate inbox arrival | ✅ Recommend |
| SMTP via `smtplib` + app password | Generate Google app password, store as secret | App passwords can be revoked silently if Workspace policy tightens | Immediate | OK fallback |
| Gmail draft only (no send) | One-time `gmail.compose`-scoped token | Same as Gmail send | Manual — you must check Drafts every Thursday | ❌ Defeats automation |

**Recommendation: Gmail API send with a separate send-scoped token (`token_send.json`).**

Reasons:
1. Same OAuth ecosystem you've already accepted for the readonly fetch — no new tech (SMTP, app passwords).
2. Scope isolation is clean: readonly token can only read; send token can only send. A compromised secret can't do both jobs.
3. The capture script mirrors `capture_auth.py`'s shape — same one-time pattern, easy to remember.
4. Workspace admins can revoke OAuth app access via the admin console centrally; SMTP app passwords are per-user and harder to govern.

Implementation: a tiny `capture_send_auth.py` runs the OAuth dance with scope `https://www.googleapis.com/auth/gmail.send` and writes `token_send.json`. `main.py` uses it to POST a `messages.send` to `youthadmin@mannahouse.church` with the run summary. Total added code: ~30 lines.

## Failure notification path (separate from re-auth)

If `main.py` crashes for any other reason, the workflow's `on: failure:` step still emails you via Gmail send (same token). If the Gmail send itself is what failed, GH Actions' built-in workflow-failure email kicks in. Two layers, neither depends on the other.

## Total monthly maintenance budget

- ~10 min: re-capture `auth_state.json`, base64, paste into `AUTH_STATE_B64`
- ~5 min: re-capture Gmail tokens if expired (less frequent — refresh tokens usually persist longer than session cookies)
- 0 min if nothing expired

If maintenance creeps above 30 min/month, escalate to (b) or (c) — the equation flips.
