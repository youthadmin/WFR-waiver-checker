#!/usr/bin/env python3
"""
main.py — orchestrator for the WFR waiver-sync agent.

Wiring:
  1. gmail_watcher → fetch unprocessed WFR emails (skips message_ids in
     processed.json)
  2. waiver_parser → extract attendee names from each XLSX attachment
  3. pco_matcher   → API read of both signups, match waiver names against
     attendees with the email/name/fuzzy ladder
  4. pco_writer    → Playwright toggle of the "Washington Family Ranch
     Form" checkbox per matched attendee (DRY_RUN reads but never clicks)
  5. Gmail send    → email the run summary to NOTIFY_EMAIL

Guardrails (all enforced here):
  • DRY_RUN — inverts pco_writer's click behavior
  • MAX_ATTENDEES_PER_RUN hard cap — halts before exceeding
  • RUN_TIMEOUT_MINUTES deadline — halts before exceeding
  • Per-run log file at logs/YYYY-MM-DD-HHMM.log (never overwritten)
  • Re-auth needed → alert email, exit non-zero, do NOT mark messages
    processed (so the next run can retry)
  • Post-click state mismatch → halt with partial summary
  • Top-level exception handler emails the traceback as a last resort
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials   # noqa: E402
from googleapiclient.discovery import build         # noqa: E402

import gmail_watcher                                # noqa: E402
import pco_matcher                                  # noqa: E402
import pco_writer                                   # noqa: E402
import waiver_parser                                # noqa: E402

PROJECT_ROOT = Path(__file__).parent
LOGS_DIR = PROJECT_ROOT / "logs"
TOKEN_SEND_FILE = PROJECT_ROOT / "token_send.json"
SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "youthadmin@mannahouse.church")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
RECORD_VIDEO = os.environ.get("RECORD_VIDEO", "false").lower() == "true"
MATCH_THRESHOLD = int(os.environ.get("MATCH_THRESHOLD", "90"))
MAX_ATTENDEES_PER_RUN = int(os.environ.get("MAX_ATTENDEES_PER_RUN", "50"))
RUN_TIMEOUT_MINUTES = int(os.environ.get("RUN_TIMEOUT_MINUTES", "20"))

SIGNUP_YOUTH_CAMP = os.environ.get("SIGNUP_YOUTH_CAMP", "3526683")
SIGNUP_DREAM_TEAM = os.environ.get("SIGNUP_DREAM_TEAM", "3527418")

# ─── logging ─────────────────────────────────────────────────────────


class _Logger:
    """Tiny tee logger — appends to a run-specific file AND mirrors to stdout.
    Avoids pulling in the full logging module for the small surface we need.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = open(path, "a", encoding="utf-8")

    def __call__(self, msg: str = "") -> None:
        line = f"{datetime.now(timezone.utc).isoformat()}  {msg}"
        print(msg)  # stdout for GH Actions log
        self._fp.write(line + "\n")
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()


# ─── Gmail send (summary email + alerts) ─────────────────────────────


def _build_send_service():
    if not TOKEN_SEND_FILE.exists():
        raise RuntimeError(
            f"{TOKEN_SEND_FILE} not found. Run `python capture_send_auth.py` first."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_SEND_FILE), SEND_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_SEND_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError(
                "Gmail send credentials invalid and cannot be refreshed. "
                "Re-run capture_send_auth.py."
            )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(to: str, subject: str, body: str) -> None:
    service = _build_send_service()
    msg = MIMEText(body, _charset="utf-8")
    msg["to"] = to
    msg["from"] = "me"
    msg["subject"] = subject
    raw = urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ─── run accounting ──────────────────────────────────────────────────


@dataclass
class RunSummary:
    started_at: datetime
    log_path: Path
    dry_run: bool
    messages_processed: int = 0
    waiver_names_total: int = 0
    matched_total: int = 0
    auto_checked: list[pco_writer.WriteResult] = field(default_factory=list)
    already_complete: list[pco_writer.WriteResult] = field(default_factory=list)
    would_check: list[pco_writer.WriteResult] = field(default_factory=list)
    needs_review: list[tuple[str, str]] = field(default_factory=list)
    write_failures: list[pco_writer.WriteResult] = field(default_factory=list)
    halted: bool = False
    halt_reason: Optional[str] = None
    fatal_error: Optional[str] = None

    def finished_at(self) -> datetime:
        return datetime.now(timezone.utc)

    def duration_seconds(self) -> int:
        return int((self.finished_at() - self.started_at).total_seconds())


def _format_summary(s: RunSummary) -> tuple[str, str]:
    """Return (subject, body) for the summary email."""
    when = s.started_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    mode = "DRY-RUN" if s.dry_run else "LIVE"

    if s.fatal_error:
        head = "FAILED"
    elif s.halted:
        head = "HALTED"
    elif s.write_failures:
        head = "WARN"
    else:
        head = "OK"

    checked_count = len(s.would_check) if s.dry_run else len(s.auto_checked)
    subject = (
        f"[Waiver sync] {head} — {checked_count} "
        f"{'would-check' if s.dry_run else 'updated'}, "
        f"{len(s.needs_review)} review, {len(s.write_failures)} error  ({when})"
    )

    lines: list[str] = []
    lines.append(f"Run:      {when}")
    lines.append(f"Mode:     {mode}")
    lines.append(f"Duration: {s.duration_seconds()}s")
    lines.append(f"Log:      {s.log_path.name}")
    if s.fatal_error:
        lines.append("")
        lines.append("─── FATAL ERROR ───────────────────────────────────")
        lines.append(s.fatal_error)
    if s.halted:
        lines.append("")
        lines.append(f"⚠️  Run halted: {s.halt_reason}")
    lines.append("")
    lines.append("─── Summary ───────────────────────────────────────")
    lines.append(f"Messages processed:  {s.messages_processed}")
    lines.append(f"Waiver names total:  {s.waiver_names_total}")
    lines.append(f"Matched (any tier):  {s.matched_total}")
    lines.append(
        f"Auto-checked:        "
        f"{len(s.would_check) if s.dry_run else len(s.auto_checked)}"
        f"{'  (dry-run — no actual writes)' if s.dry_run else ''}"
    )
    lines.append(f"Already complete:    {len(s.already_complete)}")
    lines.append(f"Needs manual review: {len(s.needs_review)}")
    lines.append(f"Write errors:        {len(s.write_failures)}")

    def _section(title: str, items: list, fmt) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"─── {title} ({len(items)}) " + "─" * max(0, 35 - len(title)))
        for item in items:
            lines.append(fmt(item))

    def _wr_line(r: pco_writer.WriteResult) -> str:
        sig = "YC" if r.attendee.signup_id == SIGNUP_YOUTH_CAMP else "DT"
        return (
            f"  • {r.attendee.full_name:<28} [{sig}]  "
            f"conf={r.confidence:>3} {r.method}"
        )

    _section(
        "Auto-checked" if not s.dry_run else "Would auto-check (dry-run)",
        s.auto_checked if not s.dry_run else s.would_check,
        _wr_line,
    )
    _section("Already complete — skipped", s.already_complete, _wr_line)
    _section(
        "Needs manual review",
        s.needs_review,
        lambda t: f"  • {t[0]:<30}  reason: {t[1]}",
    )
    _section(
        "Write errors",
        s.write_failures,
        lambda r: f"  • {r.attendee.full_name:<28}  error: {r.error}",
    )

    return subject, "\n".join(lines)


# ─── core run ────────────────────────────────────────────────────────


def _attachment_to_parse(msg: gmail_watcher.FetchedMessage) -> Optional[bytes]:
    """Pick the first XLSX/PDF attachment, or fall back to body bytes."""
    for att in msg.attachments:
        if att.mime_type in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/pdf",
        ):
            return att.data
    # Fall back to body if present
    if msg.body_text:
        return msg.body_text.encode("utf-8")
    if msg.body_html:
        return msg.body_html.encode("utf-8")
    return None


def _run(log) -> RunSummary:
    started = datetime.now(timezone.utc)
    log_path = LOGS_DIR / f"{started.astimezone().strftime('%Y-%m-%d-%H%M')}.log"
    summary = RunSummary(started_at=started, log_path=log_path, dry_run=DRY_RUN)

    log(f"=== waiver-sync run started ===")
    log(f"mode: {'DRY-RUN' if DRY_RUN else 'LIVE'}")
    log(f"signups: youth={SIGNUP_YOUTH_CAMP} dream={SIGNUP_DREAM_TEAM}")
    log(f"thresholds: match={MATCH_THRESHOLD} cap={MAX_ATTENDEES_PER_RUN} timeout={RUN_TIMEOUT_MINUTES}m")

    deadline = time.time() + RUN_TIMEOUT_MINUTES * 60

    log("")
    log(f"Gmail query: {gmail_watcher.build_query()}")
    messages = gmail_watcher.fetch_new_waiver_messages()
    log(f"Found {len(messages)} unprocessed message(s)")
    if not messages:
        log("Nothing to do — exiting clean.")
        return summary

    log("Fetching PCO attendees from both signups…")
    attendees = pco_matcher.fetch_all_attendees([SIGNUP_YOUTH_CAMP, SIGNUP_DREAM_TEAM])
    log(f"Fetched {len(attendees)} attendees")

    for msg in messages:
        if time.time() > deadline:
            summary.halted = True
            summary.halt_reason = f"Run timeout ({RUN_TIMEOUT_MINUTES}m) exceeded"
            log(f"⚠️  {summary.halt_reason}")
            break

        log("")
        log(f"── message {msg.message_id} — subject={msg.subject!r} ──")

        payload = _attachment_to_parse(msg)
        if payload is None:
            log("✗ no usable attachment or body found")
            summary.needs_review.append((msg.subject, "no attachment or body"))
            gmail_watcher.mark_processed(msg.message_id, name_count=0)
            continue

        try:
            waivers = waiver_parser.parse(payload)
        except Exception as e:
            log(f"✗ parse failed: {e}")
            summary.needs_review.append((msg.subject, f"parse failed: {e}"))
            gmail_watcher.mark_processed(msg.message_id, name_count=0)
            continue

        log(f"  parsed {len(waivers)} name(s)")
        summary.waiver_names_total += len(waivers)
        summary.messages_processed += 1

        matches, unmatched = pco_matcher.match(waivers, attendees, threshold=MATCH_THRESHOLD)
        log(f"  matched {len(matches)}, unmatched {len(unmatched)}")
        summary.matched_total += len(matches)
        for u in unmatched:
            summary.needs_review.append((u.full_name, "no PCO match ≥ threshold"))

        if not matches:
            gmail_watcher.mark_processed(msg.message_id, name_count=len(waivers))
            continue

        if len(matches) > MAX_ATTENDEES_PER_RUN:
            reason = (
                f"{len(matches)} matched attendees exceeds hard cap of "
                f"{MAX_ATTENDEES_PER_RUN} — halting before any writes"
            )
            log(f"⚠️  {reason}")
            summary.halted = True
            summary.halt_reason = reason
            for m in matches:
                summary.needs_review.append(
                    (m.attendee.full_name, f"skipped: hard cap of {MAX_ATTENDEES_PER_RUN}")
                )
            break

        log(f"  applying {len(matches)} match(es) via pco_writer "
            f"({'dry-run' if DRY_RUN else 'live'}, headless={HEADLESS})")
        report = pco_writer.apply_matches(
            matches,
            dry_run=DRY_RUN,
            headless=HEADLESS,
            record_video=RECORD_VIDEO,
            max_modifications=MAX_ATTENDEES_PER_RUN,
            sleep_between=2.0,
        )

        for r in report.results:
            if r.action == "checked":
                summary.auto_checked.append(r)
            elif r.action == "would_check":
                summary.would_check.append(r)
            elif r.action == "already_complete":
                summary.already_complete.append(r)
            elif r.action == "failed":
                summary.write_failures.append(r)

        if report.halted:
            summary.halted = True
            summary.halt_reason = report.halt_reason
            log(f"⚠️  writer halted: {report.halt_reason}")
            break

        log(
            f"  ✓ checked={len(summary.auto_checked)} would_check={len(summary.would_check)} "
            f"already={len(summary.already_complete)} failed={len(summary.write_failures)}"
        )
        gmail_watcher.mark_processed(msg.message_id, name_count=len(waivers))

    log("")
    log(f"=== run complete (duration {summary.duration_seconds()}s) ===")
    return summary


def main() -> int:
    started = datetime.now(timezone.utc)
    log_path = LOGS_DIR / f"{started.astimezone().strftime('%Y-%m-%d-%H%M')}.log"
    log = _Logger(log_path)

    try:
        summary = _run(log)
    except pco_writer.ReAuthNeededError as e:
        log(f"✗ re-auth needed: {e}")
        try:
            send_email(
                NOTIFY_EMAIL,
                "Waiver sync paused: re-auth needed",
                f"PCO session is dead. Re-run capture_auth.py locally, base64 the new "
                f"auth_state.json, and update the AUTH_STATE_B64 repo secret.\n\n"
                f"Detail: {e}\n\nLog: {log_path.name}\n",
            )
        except Exception as send_err:
            log(f"✗ also failed to send re-auth alert: {send_err}")
        log.close()
        return 4
    except pco_writer.PostClickMismatchError as e:
        log(f"✗ post-click mismatch: {e}")
        try:
            send_email(
                NOTIFY_EMAIL,
                "Waiver sync HALTED — post-click state mismatch",
                f"A click did not produce the expected state. The run was halted to "
                f"prevent further writes.\n\nDetail: {e}\n\nLog: {log_path.name}\n",
            )
        except Exception as send_err:
            log(f"✗ also failed to send halt alert: {send_err}")
        log.close()
        return 5
    except Exception:
        tb = traceback.format_exc()
        log(f"✗ uncaught exception\n{tb}")
        try:
            send_email(
                NOTIFY_EMAIL,
                "Waiver sync FAILED — uncaught exception",
                f"The run crashed.\n\n{tb}\n\nLog: {log_path.name}\n",
            )
        except Exception as send_err:
            log(f"✗ also failed to send failure alert: {send_err}")
        log.close()
        return 6

    subject, body = _format_summary(summary)
    try:
        send_email(NOTIFY_EMAIL, subject, body)
        log(f"✓ summary emailed to {NOTIFY_EMAIL}")
    except Exception as send_err:
        log(f"✗ summary email failed: {send_err}")

    log.close()
    return 0 if not (summary.halted or summary.fatal_error or summary.write_failures) else 7


if __name__ == "__main__":
    sys.exit(main())
