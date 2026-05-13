#!/usr/bin/env python3
"""
test_one_attendee.py — manual single-attendee Playwright sanity check.

Run this against a real attendee BEFORE enabling the scheduled cron.
Exercises the full pco_writer flow (navigate → find the right card →
locate the WFR checkbox → read state → optionally click → verify) for
exactly one attendee, so you can confirm the selectors work against the
live UI before anything automated touches more than one record.

Default behavior:
  • DRY-RUN — reads the page and reports what it would click, never clicks.
  • HEADED  — opens a visible Chromium so you can watch the navigation.

Usage:
    python test_one_attendee.py                      # list all attendees + IDs
    python test_one_attendee.py "Mayuki Corrigan"    # match by name substring
    python test_one_attendee.py 150240986            # match by exact attendee_id
    python test_one_attendee.py mayuki --live        # actually click the box
    python test_one_attendee.py mayuki --headless    # run browser headless
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import pco_matcher  # noqa: E402
import pco_writer   # noqa: E402
from pco_matcher import Attendee, Match, WaiverPerson  # noqa: E402


def _signups() -> tuple[str, str]:
    return (
        os.environ.get("SIGNUP_YOUTH_CAMP", "3526683"),
        os.environ.get("SIGNUP_DREAM_TEAM", "3527418"),
    )


def _list_all() -> int:
    youth, dream = _signups()
    print(f"Fetching attendees from {youth} (Youth Camp) and {dream} (Dream Team)…")
    attendees = pco_matcher.fetch_all_attendees([youth, dream])

    print()
    print(f"{'attendee_id':<13} {'signup':<11} {'reg_id':<10} name")
    print("-" * 70)
    for a in sorted(attendees, key=lambda x: (x.signup_id, x.last_name.lower(), x.first_name.lower())):
        sig = "Youth Camp" if a.signup_id == youth else "Dream Team"
        print(f"{a.attendee_id:<13} {sig:<11} {a.registration_id:<10} {a.full_name}")
    print()
    print(f"Total: {len(attendees)}")
    return 0


def _find_attendee(query: str) -> Attendee:
    youth, dream = _signups()
    attendees = pco_matcher.fetch_all_attendees([youth, dream])

    # Exact attendee_id wins
    for a in attendees:
        if a.attendee_id == query:
            return a

    q = query.strip().lower()
    matches = [a for a in attendees if q in a.full_name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"✗ Multiple attendees match {query!r}:", file=sys.stderr)
        for a in matches:
            sig = "Youth Camp" if a.signup_id == youth else "Dream Team"
            print(f"    [{sig}] {a.full_name}  attendee_id={a.attendee_id}", file=sys.stderr)
        print("  Re-run with the exact attendee_id to disambiguate.", file=sys.stderr)
        sys.exit(2)
    print(f"✗ No attendee matches {query!r}.", file=sys.stderr)
    print("  Run with no argument to see the full list.", file=sys.stderr)
    sys.exit(3)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Attendee ID or name substring. Omit to list all attendees.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually click the checkbox. Default is dry-run.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless. Default is headed (visible).",
    )
    args = parser.parse_args()

    if not args.query:
        return _list_all()

    print("Fetching attendee from PCO…")
    attendee = _find_attendee(args.query)
    reg_url = f"https://registrations.planningcenteronline.com/registrations/{attendee.registration_id}"
    print()
    print(f"Target attendee: {attendee.full_name}")
    print(f"  attendee_id:     {attendee.attendee_id}")
    print(f"  registration_id: {attendee.registration_id}")
    print(f"  signup_id:       {attendee.signup_id}")
    print(f"  url:             {reg_url}")
    print()

    waiver = WaiverPerson(
        first_name=attendee.first_name,
        last_name=attendee.last_name,
        raw_name=attendee.full_name,
        email=attendee.email,
    )
    synthetic_match = Match(
        waiver_person=waiver,
        attendee=attendee,
        confidence=100,
        method="manual_test",
    )

    dry_run = not args.live
    print(f"Mode:    {'DRY-RUN (will NOT click)' if dry_run else 'LIVE (will click if unchecked)'}")
    print(f"Browser: {'headless' if args.headless else 'headed'}")
    print()

    try:
        report = pco_writer.apply_matches(
            [synthetic_match],
            dry_run=dry_run,
            headless=args.headless,
            record_video=False,
            max_modifications=1,
            sleep_between=0.0,
        )
    except pco_writer.ReAuthNeededError as e:
        print(f"✗ Re-auth needed: {e}", file=sys.stderr)
        print("  Run capture_auth.py to refresh auth_state.json, then retry.", file=sys.stderr)
        return 4
    except pco_writer.PostClickMismatchError as e:
        print(f"✗ Post-click state mismatch: {e}", file=sys.stderr)
        return 5
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 6

    print("=" * 60)
    print("RESULT")
    print("=" * 60)
    for r in report.results:
        print(f"  action:    {r.action}")
        print(f"  attendee:  {r.attendee.full_name}")
        if r.error:
            print(f"  error:     {r.error}")
        if r.before_screenshot:
            print(f"  before:    {r.before_screenshot}")
        if r.after_screenshot:
            print(f"  after:     {r.after_screenshot}")
    if report.halted:
        print(f"  HALTED:    {report.halt_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
