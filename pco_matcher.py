#!/usr/bin/env python3
"""
pco_matcher.py — API-only reads + name/email matching.

Fetches all attendees from both PCO Registrations signups (Youth Camp +
Dream Team), enriches each with their primary email via the People API,
and matches an incoming waiver list against them using:

    1. Exact email match            → confidence 100, method "email_exact"
    2. Exact full-name match        → confidence 100, method "name_exact"
    3. Fuzzy name match (rapidfuzz) → confidence = WRatio score, "name_fuzzy"
    4. Below MATCH_THRESHOLD        → unmatched (manual-review queue)

The matcher does no writes. pco_writer.py consumes its output and performs
the Playwright clicks.

Run standalone for a smoke test:
    python pco_matcher.py
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz

load_dotenv()

PCO_BASE = "https://api.planningcenteronline.com"
RATE_LIMIT_SLEEP = 0.25  # ~4 req/s; PCO allows 100/20s
HTTP_TIMEOUT = 30


@dataclass
class Attendee:
    attendee_id: str
    registration_id: str
    person_id: str
    first_name: str
    last_name: str
    email: Optional[str]
    signup_id: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class WaiverPerson:
    first_name: str
    last_name: str
    raw_name: str
    email: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class Match:
    waiver_person: WaiverPerson
    attendee: Attendee
    confidence: int
    method: str  # "email_exact" | "name_exact" | "name_fuzzy"


def _auth() -> tuple[str, str]:
    cid = os.environ.get("PCO_CLIENT_ID")
    sec = os.environ.get("PCO_SECRET")
    if not cid or not sec:
        raise RuntimeError("PCO_CLIENT_ID and PCO_SECRET must be set in environment / .env")
    return cid, sec


def _get(url: str, params: Optional[dict] = None) -> dict:
    r = requests.get(url, auth=_auth(), params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fetch_attendees_for_signup(signup_id: str) -> list[Attendee]:
    """One signup → list of attendees (no emails yet)."""
    attendees: list[Attendee] = []
    url = f"{PCO_BASE}/registrations/v2/signups/{signup_id}/attendees"
    params: Optional[dict] = {"include": "person,registration", "per_page": 100}

    while url:
        resp = _get(url, params=params)
        included = {(it["type"], it["id"]): it for it in resp.get("included", [])}

        for att in resp.get("data", []):
            rels = att.get("relationships", {})
            person_id = (rels.get("person", {}).get("data") or {}).get("id")
            registration_id = (rels.get("registration", {}).get("data") or {}).get("id")
            if not person_id or not registration_id:
                continue
            person = included.get(("Person", person_id))
            if not person:
                continue
            pa = person.get("attributes", {})
            attendees.append(Attendee(
                attendee_id=att["id"],
                registration_id=registration_id,
                person_id=person_id,
                first_name=(pa.get("first_name") or "").strip(),
                last_name=(pa.get("last_name") or "").strip(),
                email=None,
                signup_id=signup_id,
            ))

        url = resp.get("links", {}).get("next")
        params = None  # next link carries its own query string
        if url:
            time.sleep(RATE_LIMIT_SLEEP)

    return attendees


def _enrich_email(attendee: Attendee) -> None:
    """Mutate the Attendee in place with their primary email if available."""
    try:
        data = _get(f"{PCO_BASE}/people/v2/people/{attendee.person_id}/emails")
    except requests.HTTPError:
        return
    emails = data.get("data", [])
    if not emails:
        return
    primary = next(
        (e for e in emails if (e.get("attributes") or {}).get("primary")),
        None,
    )
    chosen = primary or emails[0]
    addr = (chosen.get("attributes") or {}).get("address")
    if addr:
        attendee.email = addr.strip().lower() or None


def fetch_all_attendees(signup_ids: list[str]) -> list[Attendee]:
    """Fetch attendees from each signup and enrich each with primary email."""
    all_attendees: list[Attendee] = []
    for sid in signup_ids:
        all_attendees.extend(_fetch_attendees_for_signup(sid))

    for a in all_attendees:
        _enrich_email(a)
        time.sleep(RATE_LIMIT_SLEEP)

    return all_attendees


def match(
    waiver_list: list[WaiverPerson],
    attendees: list[Attendee],
    threshold: int = 90,
) -> tuple[list[Match], list[WaiverPerson]]:
    """Return (matches, unmatched). First-come-first-served on duplicate names."""
    matches: list[Match] = []
    unmatched: list[WaiverPerson] = []
    used: set[str] = set()

    # Family registrations often share a parent's email across multiple
    # student attendees (e.g. the Corrigan siblings all carry hiromicorrigan@…).
    # Only index emails that uniquely identify a single attendee — shared
    # emails fall through to name matching where the names disambiguate.
    email_groups: dict[str, list[Attendee]] = {}
    for a in attendees:
        if a.email:
            email_groups.setdefault(a.email, []).append(a)
    by_email: dict[str, Attendee] = {
        addr: lst[0] for addr, lst in email_groups.items() if len(lst) == 1
    }

    by_name: dict[str, Attendee] = {}
    for a in attendees:
        by_name.setdefault(a.full_name.lower(), a)

    for wp in waiver_list:
        # 1. Exact email
        if wp.email:
            hit = by_email.get(wp.email.strip().lower())
            if hit and hit.attendee_id not in used:
                matches.append(Match(wp, hit, 100, "email_exact"))
                used.add(hit.attendee_id)
                continue

        # 2. Exact full-name (case-insensitive)
        hit = by_name.get(wp.full_name.lower())
        if hit and hit.attendee_id not in used:
            matches.append(Match(wp, hit, 100, "name_exact"))
            used.add(hit.attendee_id)
            continue

        # 3. Fuzzy — score each candidate against both the normalized waiver
        # name AND the raw cell content (whitespace-collapsed, lowercased),
        # take the higher. PCO sometimes stores a person's middle name as
        # part of first_name (e.g. "Nicholas Caeden" / "King"); the waiver's
        # normalized "Nicholas King" then scores below threshold (~85) but
        # the raw "Nicholas Caeden King" lines up at 97+. Considering both
        # forms catches that pattern without loosening the 90 floor.
        wp_normalized = wp.full_name.lower()
        wp_raw = " ".join((wp.raw_name or "").lower().split())
        best_aid: Optional[str] = None
        best_score = 0
        for a in attendees:
            if a.attendee_id in used:
                continue
            pco = a.full_name.lower()
            score = int(max(
                fuzz.WRatio(wp_normalized, pco),
                fuzz.WRatio(wp_raw, pco) if wp_raw else 0,
            ))
            if score > best_score:
                best_score = score
                best_aid = a.attendee_id

        if best_aid is not None and best_score >= threshold:
            hit = next(a for a in attendees if a.attendee_id == best_aid)
            matches.append(Match(wp, hit, best_score, "name_fuzzy"))
            used.add(best_aid)
            continue

        unmatched.append(wp)

    return matches, unmatched


def _smoke_test() -> int:
    youth = os.environ.get("SIGNUP_YOUTH_CAMP", "3526683")
    dream = os.environ.get("SIGNUP_DREAM_TEAM", "3527418")
    print(f"Fetching attendees from signups {youth} (Youth Camp) and {dream} (Dream Team)…")
    print("(This includes email enrichment — expect ~1 second per attendee at the rate limit.)")
    print()

    attendees = fetch_all_attendees([youth, dream])

    by_signup: dict[str, list[Attendee]] = {}
    for a in attendees:
        by_signup.setdefault(a.signup_id, []).append(a)

    for sid, lst in by_signup.items():
        label = "Youth Camp" if sid == youth else "Dream Team" if sid == dream else sid
        with_email = sum(1 for a in lst if a.email)
        print(f"  {label} ({sid}): {len(lst)} attendees, {with_email} with email on file")

    print()
    print(f"Total: {len(attendees)} attendees")
    if attendees:
        print()
        print("Sample (first 3):")
        for a in attendees[:3]:
            email = a.email or "—"
            print(f"  • {a.full_name:30s}  email={email:35s}  reg_id={a.registration_id}  signup={a.signup_id}")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
