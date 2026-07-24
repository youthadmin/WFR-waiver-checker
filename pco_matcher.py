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
import re
import sys
import time
import unicodedata
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


# ─── name normalization ──────────────────────────────────────────────
#
# Three failure modes surfaced in the 2026-07-24 run leaked real
# registrants into the manual-review queue purely on spelling:
#   • accents      — "Ivan Álvarez-Tarter" vs PCO "Ivan Alvarez Tarter"
#   • hyphens      — "Juniper Wardrop" vs PCO "Juniper Wardrop-Long"
#   • parentheses  — "Abbie Tice" vs PCO "Abigail (Abbie) Tice"
#   • nicknames    — "Ben Jones"/"Benjamin", "Amelia"/"Mia" Callahan
# _normalize handles the first three; NICKNAME_CANON the last. The 90
# floor is unchanged — these only canonicalize the strings compared.


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def _normalize(s: str) -> str:
    """Accent/punctuation-insensitive lowercase form for name comparison."""
    s = _strip_accents(s or "").lower()
    s = re.sub(r"[-.,'`]", " ", s)  # hyphens/periods/commas/apostrophes → space
    return " ".join(s.split())


# Conservative nickname → formal-name map. Deliberately tight: only
# well-established diminutives, so genuinely distinct given names are
# never collapsed (e.g. "Liam" is NOT mapped to William — Liam Madsen is
# a real separate person, not a nickname). Only the given-name (first
# token) is canonicalized.
NICKNAME_CANON = {
    "ben": "benjamin", "benji": "benjamin", "benjie": "benjamin", "benny": "benjamin",
    "abby": "abigail", "abbie": "abigail", "abi": "abigail",
    "mia": "amelia",
    "will": "william", "bill": "william", "billy": "william", "willie": "william",
    "tom": "thomas", "tommy": "thomas",
    "mike": "michael", "mikey": "michael",
    "matt": "matthew", "matty": "matthew",
    "chris": "christopher",
    "nick": "nicholas",
    "alex": "alexander",
    "sam": "samuel", "sammy": "samuel",
    "dan": "daniel", "danny": "daniel",
    "dave": "david",
    "jim": "james", "jimmy": "james",
    "joe": "joseph", "joey": "joseph",
    "rob": "robert", "robbie": "robert", "bob": "robert", "bobby": "robert",
    "rick": "richard", "ricky": "richard", "rich": "richard",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "cathy": "catherine",
    "liz": "elizabeth", "beth": "elizabeth", "betsy": "elizabeth", "eliza": "elizabeth",
    "meg": "margaret", "maggie": "margaret",
    "jen": "jennifer", "jenny": "jennifer",
    "becca": "rebecca", "becky": "rebecca",
    "tony": "anthony",
    "ed": "edward", "eddie": "edward",
    "greg": "gregory",
    "jon": "jonathan",
    "josh": "joshua",
    "zach": "zachary", "zack": "zachary",
    "andy": "andrew", "drew": "andrew",
    "charlie": "charles", "chuck": "charles",
    "gabe": "gabriel",
    "nate": "nathaniel",
    "cam": "cameron",
    "tim": "timothy", "timmy": "timothy",
}


def _canonicalize(normalized: str) -> str:
    """Swap the first token for its formal name if it's a known nickname."""
    toks = normalized.split()
    if not toks:
        return normalized
    toks[0] = NICKNAME_CANON.get(toks[0], toks[0])
    return " ".join(toks)


def _paren_expand(text: str) -> list[str]:
    """'Abigail (Abbie)' → ['Abigail', 'Abbie']; plain text → [text]."""
    m = re.search(r"\(([^)]*)\)", text or "")
    if not m:
        return [text or ""]
    inside = m.group(1).strip()
    outside = " ".join(re.sub(r"\([^)]*\)", " ", text).split())
    forms = [f for f in (outside, inside) if f]
    return forms or [text or ""]


def _attendee_forms(a: "Attendee") -> tuple[set[str], set[str]]:
    """(exact_forms, fuzzy_forms) for an attendee.

    exact_forms are accent/punct-normalized (with parenthetical aliases
    expanded); fuzzy_forms add the nickname-canonicalized variants.
    """
    exact: set[str] = set()
    for f in _paren_expand(a.first_name):
        for l in _paren_expand(a.last_name):
            base = _normalize(f"{f} {l}")
            if base:
                exact.add(base)
    fuzzy = set(exact)
    for base in exact:
        fuzzy.add(_canonicalize(base))
    return exact, fuzzy


def _waiver_forms(wp: "WaiverPerson") -> tuple[set[str], set[str]]:
    """(exact_forms, fuzzy_forms) for a waiver person, mirroring _attendee_forms."""
    exact: set[str] = set()
    for src in (wp.full_name, wp.raw_name):
        base = _normalize(src or "")
        if base:
            exact.add(base)
    fuzzy = set(exact)
    for base in exact:
        fuzzy.add(_canonicalize(base))
    return exact, fuzzy


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

    # Precompute normalized match forms once per attendee. exact_forms are
    # accent/hyphen/parenthesis-insensitive; fuzzy_forms also carry the
    # nickname-canonicalized variants. Both feed the ladder below.
    att_forms: dict[str, tuple[set[str], set[str]]] = {
        a.attendee_id: _attendee_forms(a) for a in attendees
    }

    # First-wins exact index over the normalized forms (includes
    # parenthetical aliases, e.g. "Abigail (Abbie) Tice" → both spellings).
    by_name: dict[str, Attendee] = {}
    for a in attendees:
        for form in att_forms[a.attendee_id][0]:
            by_name.setdefault(form, a)

    for wp in waiver_list:
        wp_exact, wp_fuzzy = _waiver_forms(wp)

        # 1. Exact email
        if wp.email:
            hit = by_email.get(wp.email.strip().lower())
            if hit and hit.attendee_id not in used:
                matches.append(Match(wp, hit, 100, "email_exact"))
                used.add(hit.attendee_id)
                continue

        # 2. Exact name (accent/hyphen/parenthesis-insensitive)
        hit = next(
            (by_name[f] for f in wp_exact
             if f in by_name and by_name[f].attendee_id not in used),
            None,
        )
        if hit is not None:
            matches.append(Match(wp, hit, 100, "name_exact"))
            used.add(hit.attendee_id)
            continue

        # 3. Fuzzy — score every waiver form against every attendee form
        # (normalized + nickname-canonicalized) and keep the max. This
        # covers three patterns without loosening the 90 floor:
        #   • middle name stored in PCO first_name ("Nicholas Caeden"/"King")
        #     — the raw waiver name "Nicholas Caeden King" lines up at 97+
        #   • hyphenated surnames ("Wardrop" vs "Wardrop-Long")
        #   • nicknames ("Ben"↔"Benjamin", "Mia"↔"Amelia") via the canonical
        #     forms, which score 100 against each other
        best_aid: Optional[str] = None
        best_score = 0
        for a in attendees:
            if a.attendee_id in used:
                continue
            att_fuzzy = att_forms[a.attendee_id][1]
            score = 0
            for wf in wp_fuzzy:
                for af in att_fuzzy:
                    s = int(fuzz.WRatio(wf, af))
                    if s > score:
                        score = s
                if score == 100:
                    break
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
