# Phase 1 — PCO Registrations API Investigation

Date: 2026-05-12
Verified against: live Mannahouse PCO org (id 23287) using existing `PCO_CLIENT_ID` / `PCO_SECRET` Personal Access Token from `~/Documents/Agents/pco-automation/.env`.

## TL;DR

**The PCO Registrations API is read-only.** Every PATCH against the Attendee or Registration vertex returns `403 Forbidden — "User with id 40557725 cannot update AppGraph::V2025_05_01::Vertices::AttendeeVertex"`. The wall is at the vertex level, not the field level. There is no API path that toggles the waiver-complete checkbox on an attendee record.

This contradicts a memory note from 2026-05-05 that said Registrations is not in the public API at all. **Status today: Registrations GET endpoints _are_ exposed**, but writes to Attendee/Registration are blocked.

Two viable paths forward, in order of preference:
1. **Playwright** (extend the existing `pco-incomplete-form-reminder` agent's logged-in browser pattern to click the checkbox). Highest fidelity — flips the exact UI checkbox staff use.
2. **Person Note** as a proxy via the People API (writeable). Reliable, but doesn't actually toggle the Registrations checkbox — only useful if the downstream consumer of "waiver complete" is human-readable rather than a PCO automation.

Need a decision from you on path before Phase 2 starts. Recommendation: Playwright, since the manual workflow you described is "click a checkbox in PCO" and a Note doesn't satisfy that.

---

## The four Phase 1 questions

### Q1. Can I write to a custom form field on an attendee record via the API?

**No.** Two independent failures:

- The Attendee resource exposes only 6 attributes: `active`, `canceled`, `created_at`, `updated_at`, `waitlisted`, `waitlisted_at`. No form-field responses, no custom-field bag.
- The vertex blocks writes entirely. PATCH with any payload (valid field, invalid field, or empty `attributes: {}`) returns:
  ```
  HTTP 403
  {"errors":[{"status":"403","title":"Forbidden",
    "detail":"You do not have access to this resource",
    "meta":{"description":"User with id 40557725 cannot update
      AppGraph::V2025_05_01::Vertices::AttendeeVertex with id 150240986."}}]}
  ```
- Same 403 on Registration: `cannot update ...::RegistrationVertex`.

The error mentions "User with id 40557725" (your personal account) — this could in theory be a scope/permission issue on the PAT rather than an API-wide read-only ceiling. But Registrations form fields are not part of the documented v2 schema in any case, so even with elevated scope it's unlikely we'd get a writable form-response endpoint.

### Q2. Is there a built-in "waiver received" / "form complete" status flag I can PATCH?

**No.** The Attendee schema has no such attribute. The Registration parent record exposes only `created_at` / `updated_at`. The Signup itself has the high-level toggles (`open`, `closed`, `archived`) but nothing per-attendee.

Includable relationships from Attendee: `emergency_contact`, `person`, `registration`, `selection_type`. Notice the absence of any `form_response`, `waiver`, or `external_form` link.

Both target signups behave the same way:
- Youth Camp 2026 — signup `3526683` — 26 attendees as of probe
- Dream Team 2026 — signup `3527418` — populated and accessible

### Q3. Can I attach a note or tag as a proxy?

**Notes: yes. Tags: no.**

- `GET /people/v2/people/{person_id}/notes` → 200, returns 0 notes for our test person. The endpoint exists and is in scope.
- `GET /people/v2/note_categories` → 200, returns categories. "General" (id `47269`, locked) is always present. Could create a dedicated "Waiver Complete" category for clean filtering.
- POST shape (per PCO People API docs and our existing `watchdog.py`/`api_post` pattern): a note is created with `type: "Note"`, `attributes: { note: "Waiver received 2026-05-14" }`, and a relationship to the note category.
- `GET /people/v2/tags` → 404 with this PAT. Either the org doesn't have the Tags feature provisioned, or the token lacks scope. Not pursuing.

**Caveat on the Note path:** a note lives on the People record, not on the Attendee record. It does *not* flip the waiver checkbox visible inside the Registrations signup. Whether that matters depends entirely on what reads the checkbox downstream — if it's a human glancing at the attendee list, a note is invisible to them.

### Q4. Browser automation path (Playwright + saved auth state)?

**Yes — and most of the infrastructure already exists in `~/Documents/Agents/pco-incomplete-form-reminder/`.** That project solved:
- Login with `zayla5596@gmail.com` (gmail, not yahoo)
- The two-org "Choose an Organization" interstitial → must click Mannahouse explicitly (not `.first`, because Alive is listed first)
- The redirect from `registrations.planningcenteronline.com/signups/{id}` to public Church Center when not logged in — login must happen before navigating
- Diagnostic screenshots in `artifacts/` after every step

What's new for this project:
- Navigate to an individual attendee inside a signup (the existing agent operates at the bulk-table level, not per-attendee)
- Locate and click the waiver checkbox on the attendee detail panel/drawer
- Confirm the toggle persisted (re-read state)

Risk: PCO's UI shifts. The existing project documents that the bulk-row Actions button is `nth(1)` of two "Actions" buttons on the page. Similar selector fragility will apply here. Mitigation = the screenshot-after-every-step pattern that's already in place.

---

## Other findings worth knowing

- **PCO API version header:** all v2 responses report `AppGraph::V2025_05_01::Vertices::...`. So we're on the `2025-05-01` schema today.
- **Mannahouse org id:** `23287`. PAT user id: `40557725`.
- **`registrations/v2` confirmed live:** root returns Organization Mannahouse with links to campuses, categories, signups. The 2026-05-05 memory note saying "PCO does not expose Registrations in its public API" is stale — verified outdated.
- **Pagination:** standard `?per_page=N&offset=N` with `meta.total_count` and `meta.next.offset`. Same as Services/People APIs.
- **One existing API auth pattern to reuse:** `requests.get(url, auth=(PCO_CLIENT_ID, PCO_SECRET))` from `watchdog.py:204`. HTTP Basic. No OAuth dance required.

---

## Recommended Phase 2 path

**Playwright.** Reasons:

1. It's what you actually want — the checkbox you've been clicking manually is in the Registrations UI, and Playwright is the only path that flips that exact widget.
2. Reuse leverage: the `pco-incomplete-form-reminder` repo already owns login, org-selection, screenshot diagnostics, and the secret layout. New code is mostly: navigate to attendee → click checkbox → assert.
3. The Note-as-proxy path is strictly worse unless you confirm the checkbox has no consumer beyond your eyes.

If you want both belt and suspenders, write a Note too (cheap, idempotent, gives an audit trail in the Person's record) — but treat Playwright as the source of truth for waiver state.

---

## Questions still pending from your brief (Phase 2 inputs)

These don't block Phase 1 but I'll need them before writing the pipeline:

1. **Washington Square Ranch sender email** — the `from:` filter for Gmail. Your brief had `[fill in]`.
2. **Subject pattern** — `[fill in]` in your brief. Just the recurring substring is fine ("Waiver List", "Camp Waivers", etc.).
3. **Notification email for the run summary** — assuming `youthadmin@mannahouse.church` from auto-memory; confirm.
4. **Where exactly is the waiver checkbox in the PCO UI?** — under each attendee's detail drawer? Or is it the "External Form" status from the Registrations signup overview? This affects how Playwright locates the toggle.
5. **Confirm: are Youth Camp `3526683` and Dream Team `3527418` the only two signups in scope?** (These match the IDs cached in auto-memory.)
