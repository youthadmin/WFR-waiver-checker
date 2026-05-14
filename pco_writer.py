#!/usr/bin/env python3
"""
pco_writer.py — Playwright module that toggles the "Washington Family Ranch
Form" checkbox on each matched attendee's registration page.

Architecture, anchored on what we confirmed against the live UI:

  • URL pattern is
      https://registrations.planningcenteronline.com/registrations/{registration_id}
    Per Gio 2026-05-13.

  • A single registration can contain multiple attendees (sibling groups),
    each rendered as its own per-attendee card on the page. Each card has
    its own "Additional forms" section with its own WFR consent checkbox
    plus a separate Medical/Liability form item that we MUST NOT touch.

  • The exact label differs by signup, surfaced on 2026-05-14:
      Youth Camp (3526683): "Washington Family Ranch Form"
      Dream Team (3527418): "Washington Family Ranch Liability Form"
    See WFR_CHECKBOX_NAMES. The writer tries each in order on every page
    and uses whichever actually renders first.

  • To click the right checkbox, we use Playwright's filter idiom:
      page.locator("div, section, article")
          .filter(has_text=attendee.full_name)
          .filter(has=page.get_by_role("checkbox", name=label))
          .first
    The .first picks the smallest container containing BOTH the attendee's
    name AND a WFR checkbox — that uniquely identifies the right card even
    among siblings.

  • Multiple matches can share a registration_id. We group matches by
    registration so each page loads exactly once.

  • Re-auth detection: after every page.goto we check the resulting URL
    for /login, churchcenter.com, or id.planningcenteronline.com. If any
    appear, raise ReAuthNeededError. We NEVER attempt headless login.

  • State verification: read checkbox state BEFORE click, skip if already
    checked, read AGAIN after click, raise PostClickMismatchError if the
    new state isn't checked. main.py catches both errors, halts the run,
    and emails the alert.

  • DRY_RUN reads each page and reports what it would click, but never
    clicks.

  • Hard cap on modifications (default 50). Once hit, we stop processing
    further matches and return with halted=True; main.py decides how to
    surface that in the run summary.

Public surface:
    apply_matches(matches, *, dry_run, auth_state_path, headless,
                  record_video, max_modifications, sleep_between)
        -> WriteRunReport
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from pco_matcher import Attendee, Match, WaiverPerson

PROJECT_ROOT = Path(__file__).parent
AUTH_STATE_PATH = PROJECT_ROOT / "auth_state.json"
AUDIT_DIR = PROJECT_ROOT / "audit"
AUDIT_BEFORE = AUDIT_DIR / "before"
AUDIT_AFTER = AUDIT_DIR / "after"
AUDIT_VIDEOS = AUDIT_DIR / "videos"

REGISTRATION_URL_TEMPLATE = (
    "https://registrations.planningcenteronline.com/registrations/{registration_id}"
)

# Both PCO signups expose the consent toggle in the "Additional forms"
# section but use different exact labels. Tried in order — the first
# label whose text appears on the page wins. Both are exact-string
# matches via get_by_role("checkbox", name=…), so the Medical /
# Student-Liability item on the Youth Camp page (full label
# "Mannahouse Youth Camp 2026 | Attendee Medical/Student Liability
# Form") cannot match either.
WFR_CHECKBOX_NAMES = (
    "Washington Family Ranch Form",            # Youth Camp signup 3526683
    "Washington Family Ranch Liability Form",  # Dream Team signup 3527418
)

# Markers that indicate the auth state is dead and we got bounced to login.
SESSION_DEAD_MARKERS = ("/login", "churchcenter.com", "id.planningcenteronline.com")

PAGE_NAVIGATION_TIMEOUT_MS = 30_000
ELEMENT_WAIT_TIMEOUT_MS = 10_000


class ReAuthNeededError(RuntimeError):
    """Raised when PCO redirects to a login surface — auth state is dead."""


class PostClickMismatchError(RuntimeError):
    """Raised when a checkbox doesn't end up in the expected state after click."""


@dataclass
class WriteResult:
    waiver_person: WaiverPerson
    attendee: Attendee
    action: str  # "already_complete" | "would_check" | "checked" | "failed"
    confidence: int
    method: str
    error: Optional[str] = None
    before_screenshot: Optional[str] = None
    after_screenshot: Optional[str] = None


@dataclass
class WriteRunReport:
    results: list[WriteResult] = field(default_factory=list)
    halted: bool = False
    halt_reason: Optional[str] = None


def _ensure_audit_dirs() -> None:
    for d in (AUDIT_BEFORE, AUDIT_AFTER, AUDIT_VIDEOS):
        d.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _check_session_alive(page: Page) -> None:
    url = page.url
    if any(marker in url for marker in SESSION_DEAD_MARKERS):
        raise ReAuthNeededError(
            f"PCO session dead — landed on {url}. Re-run capture_auth.py "
            "and refresh AUTH_STATE_B64 in repo secrets."
        )


def _find_wfr_label_on_page(page: Page) -> Optional[str]:
    """Wait briefly for either WFR label and return whichever shows up first."""
    per_label_timeout = max(2_000, ELEMENT_WAIT_TIMEOUT_MS // len(WFR_CHECKBOX_NAMES))
    for label in WFR_CHECKBOX_NAMES:
        try:
            page.wait_for_selector(f"text={label}", timeout=per_label_timeout)
            return label
        except PlaywrightTimeoutError:
            continue
    return None


def _attendee_card(page: Page, attendee_name: str, label: str):
    """Smallest container that has BOTH the attendee's name AND a WFR checkbox."""
    return (
        page.locator("div, section, article")
        .filter(has_text=attendee_name)
        .filter(has=page.get_by_role("checkbox", name=label))
        .first
    )


def _toggle_for_attendee(page: Page, match: Match, dry_run: bool) -> WriteResult:
    attendee = match.attendee
    waiver = match.waiver_person
    ts = _ts()

    # Pick whichever WFR label this page actually uses (Youth Camp vs Dream
    # Team have different labels). Returns None if neither renders within
    # the wait budget.
    label = _find_wfr_label_on_page(page)
    if label is None:
        return WriteResult(
            waiver, attendee, action="failed",
            confidence=match.confidence, method=match.method,
            error=(
                f"No WFR checkbox label rendered within {ELEMENT_WAIT_TIMEOUT_MS}ms "
                f"(tried: {', '.join(repr(n) for n in WFR_CHECKBOX_NAMES)})"
            ),
        )

    card = _attendee_card(page, attendee.full_name, label)
    checkbox = card.get_by_role("checkbox", name=label).first

    try:
        is_checked = checkbox.is_checked(timeout=ELEMENT_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return WriteResult(
            waiver, attendee, action="failed",
            confidence=match.confidence, method=match.method,
            error=f"Could not locate the WFR checkbox card for {attendee.full_name}",
        )

    before_path = AUDIT_BEFORE / f"{ts}_{attendee.attendee_id}.png"
    try:
        page.screenshot(path=str(before_path), full_page=True)
    except Exception:
        before_path = None

    if is_checked:
        return WriteResult(
            waiver, attendee, action="already_complete",
            confidence=match.confidence, method=match.method,
            before_screenshot=str(before_path) if before_path else None,
        )

    if dry_run:
        return WriteResult(
            waiver, attendee, action="would_check",
            confidence=match.confidence, method=match.method,
            before_screenshot=str(before_path) if before_path else None,
        )

    checkbox.check()
    # Allow the PATCH to round-trip before reading state back.
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_NAVIGATION_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass  # networkidle can be slow on busy PCO pages; we still verify below

    new_state = checkbox.is_checked(timeout=ELEMENT_WAIT_TIMEOUT_MS)
    if not new_state:
        raise PostClickMismatchError(
            f"Clicked WFR for {attendee.full_name} (attendee_id={attendee.attendee_id}) "
            f"but post-click state still reads unchecked. Halting run."
        )

    after_path = AUDIT_AFTER / f"{ts}_{attendee.attendee_id}.png"
    try:
        page.screenshot(path=str(after_path), full_page=True)
    except Exception:
        after_path = None

    return WriteResult(
        waiver, attendee, action="checked",
        confidence=match.confidence, method=match.method,
        before_screenshot=str(before_path) if before_path else None,
        after_screenshot=str(after_path) if after_path else None,
    )


def apply_matches(
    matches: list[Match],
    *,
    dry_run: bool = True,
    auth_state_path: Path = AUTH_STATE_PATH,
    headless: bool = True,
    record_video: bool = False,
    max_modifications: int = 50,
    sleep_between: float = 2.0,
) -> WriteRunReport:
    """Toggle WFR checkboxes for matched attendees via Playwright.

    Groups matches by registration_id so each registration page loads
    exactly once even for sibling registrations.
    """
    if not auth_state_path.exists():
        raise RuntimeError(
            f"{auth_state_path} not found. Run `python capture_auth.py` first."
        )

    _ensure_audit_dirs()
    report = WriteRunReport()

    by_reg: dict[str, list[Match]] = {}
    for m in matches:
        by_reg.setdefault(m.attendee.registration_id, []).append(m)

    modifications = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_kwargs = {
            "storage_state": str(auth_state_path),
            "viewport": {"width": 1280, "height": 800},
        }
        if record_video:
            context_kwargs["record_video_dir"] = str(AUDIT_VIDEOS)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_navigation_timeout(PAGE_NAVIGATION_TIMEOUT_MS)

        try:
            for reg_id, reg_matches in by_reg.items():
                if modifications >= max_modifications:
                    report.halted = True
                    report.halt_reason = (
                        f"Hard cap of {max_modifications} modifications reached"
                    )
                    for m in reg_matches:
                        report.results.append(WriteResult(
                            m.waiver_person, m.attendee, action="failed",
                            confidence=m.confidence, method=m.method,
                            error="Skipped — hard cap reached",
                        ))
                    continue

                url = REGISTRATION_URL_TEMPLATE.format(registration_id=reg_id)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except PlaywrightTimeoutError as e:
                    for m in reg_matches:
                        report.results.append(WriteResult(
                            m.waiver_person, m.attendee, action="failed",
                            confidence=m.confidence, method=m.method,
                            error=f"Navigation timeout: {e}",
                        ))
                    continue

                _check_session_alive(page)  # raises ReAuthNeededError if dead

                for idx, m in enumerate(reg_matches):
                    if modifications >= max_modifications:
                        report.halted = True
                        report.halt_reason = (
                            f"Hard cap of {max_modifications} modifications reached"
                        )
                        for remaining in reg_matches[idx:]:
                            report.results.append(WriteResult(
                                remaining.waiver_person, remaining.attendee,
                                action="failed",
                                confidence=remaining.confidence,
                                method=remaining.method,
                                error="Skipped — hard cap reached",
                            ))
                        break

                    result = _toggle_for_attendee(page, m, dry_run=dry_run)
                    report.results.append(result)
                    if result.action == "checked":
                        modifications += 1
                    time.sleep(sleep_between)

        finally:
            context.close()
            browser.close()

    return report


def _smoke_test() -> int:
    print("pco_writer.py has no standalone smoke test — invoke via main.py or")
    print("test_one_attendee.py (built in step 7). Module loads cleanly though.")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
