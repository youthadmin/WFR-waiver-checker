#!/usr/bin/env python3
"""
test_writer_offline.py — drive the real Playwright write path against a
local replica of a two-attendee registration page.

Touches nothing in PCO: it loads fixtures/multi_attendee_registration.html
from disk, so it needs neither credentials nor auth_state.json, and it is
safe to run in LIVE mode — the only checkbox it can move is in the local
file.

What it proves, which the pure-logic checks in test_target_selection.py
cannot: the in-page probe JS parses the real markup, the label-overlay
click lands with force=True, post-click verification reads the right
element back, and checking one sibling leaves the other alone.

Usage:
    python test_writer_offline.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

import pco_writer
from pco_matcher import Attendee, Match, WaiverPerson

FIXTURE = Path(__file__).parent / "fixtures" / "multi_attendee_registration.html"

DESTINY_WFR = "388414-159570963-1"
DANIEL_WFR = "388414-159570964-1"
DANIEL_MEDICAL = "390751-159570964-0"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def _match(attendee_id: str, first: str, last: str) -> Match:
    attendee = Attendee(
        attendee_id=attendee_id,
        registration_id="83840005",
        person_id="p" + attendee_id,
        first_name=first,
        last_name=last,
        email=None,
        signup_id="3526683",
    )
    waiver = WaiverPerson(first, last, f"{first} {last}", None)
    return Match(waiver, attendee, 100, "offline_test")


def _state(page) -> dict[str, bool]:
    return page.evaluate(
        """() => Object.fromEntries(
             [...document.querySelectorAll('input[type=checkbox]')].map(b => [b.id, b.checked]))"""
    )


def main() -> int:
    if not FIXTURE.exists():
        print(f"✗ fixture missing: {FIXTURE}", file=sys.stderr)
        return 2

    # Keep audit screenshots out of the repo's audit/ dir for this test.
    tmp = Path(tempfile.mkdtemp(prefix="waiver-sync-offline-"))
    pco_writer.AUDIT_BEFORE = tmp / "before"
    pco_writer.AUDIT_AFTER = tmp / "after"
    pco_writer.AUDIT_BEFORE.mkdir(parents=True, exist_ok=True)
    pco_writer.AUDIT_AFTER.mkdir(parents=True, exist_ok=True)

    daniel = _match("159570964", "Daniel", "Chukwunyekwam")
    destiny = _match("159570963", "Destiny", "Chukwunyekwam")
    stranger = _match("999999999", "Grace", "Limbu")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")

        print("probe — reading the page")
        targets, count = pco_writer._probe_wfr_targets(page, "Washington Family Ranch Form")
        check("both attendees seen", count == 2, f"got {count}")
        check("exactly 2 WFR checkboxes (Medical items excluded)", len(targets) == 2,
              f"got {len(targets)}")
        check("attendee ids parsed off the DOM ids",
              [t.attendee_id for t in targets] == ["159570963", "159570964"])
        check("card headings read as the attendees' names",
              [t.card_name for t in targets] == ["Destiny Chukwunyekwam", "Daniel Chukwunyekwam"])

        print("dry run — no state may change")
        before = _state(page)
        r = pco_writer._toggle_for_attendee(page, daniel, dry_run=True)
        check("dry run reports would_check", r.action == "would_check", r.error or "")
        check("dry run resolved via attendee_id", r.target_method == "attendee_id")
        check("dry run reports a 2-person registration", r.attendees_on_page == 2)
        check("dry run changed nothing", _state(page) == before)

        print("live click — Daniel only")
        r = pco_writer._toggle_for_attendee(page, daniel, dry_run=False)
        after = _state(page)
        check("Daniel reported checked", r.action == "checked", r.error or "")
        check("Daniel's WFR box is now checked", after[DANIEL_WFR] is True)
        check("Destiny's WFR box untouched (the reported bug)", after[DESTINY_WFR] is False)
        check("Daniel's Medical form untouched", after[DANIEL_MEDICAL] == before[DANIEL_MEDICAL])

        print("second sibling — same page, own checkbox")
        r = pco_writer._toggle_for_attendee(page, destiny, dry_run=False)
        after = _state(page)
        check("Destiny reported checked", r.action == "checked", r.error or "")
        check("Destiny's WFR box is now checked", after[DESTINY_WFR] is True)

        print("re-run — already-complete is per person")
        r = pco_writer._toggle_for_attendee(page, daniel, dry_run=False)
        check("Daniel now reads already_complete", r.action == "already_complete", r.error or "")

        print("unknown attendee — must refuse, not guess")
        before = _state(page)
        r = pco_writer._toggle_for_attendee(page, stranger, dry_run=False)
        check("stranger reported failed", r.action == "failed", r.action)
        check("failure explains itself", bool(r.error) and "Grace Limbu" in (r.error or ""),
              r.error or "")
        check("stranger changed nothing", _state(page) == before)

        browser.close()

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
