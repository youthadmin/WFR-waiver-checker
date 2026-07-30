#!/usr/bin/env python3
"""
test_target_selection.py — offline checks for pco_writer._select_target.

No network, no browser, no PCO credentials: it feeds _select_target the
exact checkbox layouts read off the live registration pages on 2026-07-30
and asserts each attendee resolves to their OWN checkbox.

The regression this locks down: before 2026-07-30 the writer took the
first WFR checkbox in DOM order for every attendee, so on a registration
holding two or more people (siblings, families, a leader plus their kids)
it checked the first person's box over and over — marking the wrong
person complete and leaving the person who actually signed the form
unchecked.

Usage:
    python test_target_selection.py
"""

from __future__ import annotations

import sys

from pco_matcher import Attendee
from pco_writer import WfrTarget, _select_target


def _attendee(attendee_id: str, first: str, last: str) -> Attendee:
    return Attendee(
        attendee_id=attendee_id,
        registration_id="83840005",
        person_id="p" + attendee_id,
        first_name=first,
        last_name=last,
        email=None,
        signup_id="3526683",
    )


def _target(index, dom_id, attendee_id, card_name, checked=False, card_text=None):
    return WfrTarget(
        index=index,
        dom_id=dom_id,
        attendee_id=attendee_id,
        checked=checked,
        card_name=card_name,
        card_text=card_text if card_text is not None else f"{card_name}\nMiddle School Student\n($410)\nIncomplete",
    )


# Real layout, Youth Camp registration 83840005 (two siblings).
# Note Destiny's checkbox is FIRST in the DOM even though Daniel is listed
# first in the signup's attendee table — page position tells you nothing.
YC_SIBLINGS = [
    _target(0, "388414-159570963-1", "159570963", "Destiny Chukwunyekwam"),
    _target(1, "388414-159570964-1", "159570964", "Daniel Chukwunyekwam"),
]

# Real layout, Dream Team registration 83874195 (two adults).
DT_SIBLINGS = [
    _target(0, "388510-159641176-1", "159641176", "Jake Madsen"),
    _target(1, "388510-159641194-1", "159641194", "Aryn Madsen"),
]

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    print("_select_target — multi-attendee registrations")

    # 1. Each sibling resolves to their own checkbox, not the first one.
    destiny = _attendee("159570963", "Destiny", "Chukwunyekwam")
    daniel = _attendee("159570964", "Daniel", "Chukwunyekwam")
    t_d, m_d, e_d = _select_target(YC_SIBLINGS, 2, destiny)
    t_n, m_n, e_n = _select_target(YC_SIBLINGS, 2, daniel)
    check("Destiny → her own box", t_d is not None and t_d.dom_id == "388414-159570963-1", str(e_d))
    check("Daniel → his own box", t_n is not None and t_n.dom_id == "388414-159570964-1", str(e_n))
    check("Daniel does NOT get the first-in-DOM box (the old bug)",
          t_n is not None and t_n.index == 1)
    check("both anchored on attendee_id", (m_d, m_n) == ("attendee_id", "attendee_id"))

    # 2. Same for the Dream Team signup, whose form id and label differ.
    jake = _attendee("159641176", "Jake", "Madsen")
    aryn = _attendee("159641194", "Aryn", "Madsen")
    t_j, _, _ = _select_target(DT_SIBLINGS, 2, jake)
    t_a, _, _ = _select_target(DT_SIBLINGS, 2, aryn)
    check("Jake → his own box", t_j is not None and t_j.dom_id == "388510-159641176-1")
    check("Aryn → her own box", t_a is not None and t_a.dom_id == "388510-159641194-1")

    # 3. Per-attendee checked state is read per person, not off the first box.
    partly_done = [
        _target(0, "388414-159570963-1", "159570963", "Destiny Chukwunyekwam", checked=True),
        _target(1, "388414-159570964-1", "159570964", "Daniel Chukwunyekwam", checked=False),
    ]
    t, _, _ = _select_target(partly_done, 2, daniel)
    check("Daniel still reads unchecked while Destiny is checked",
          t is not None and t.checked is False)

    print("_select_target — fallbacks when the id scheme is unavailable")

    # 4. No parseable ids → fall back to the card heading.
    no_ids = [
        _target(0, None, None, "Destiny Chukwunyekwam"),
        _target(1, None, None, "Daniel Chukwunyekwam"),
    ]
    t, m, e = _select_target(no_ids, 2, daniel)
    check("card heading identifies Daniel", t is not None and t.index == 1 and m == "card_name", str(e))

    # 5. Nickname/accent spellings still line up (matcher's normalization).
    nicknames = [
        _target(0, None, None, "Abigail (Abbie) Tice"),
        _target(1, None, None, "Ivan Álvarez-Tarter"),
    ]
    t, m, e = _select_target(nicknames, 2, _attendee("1", "Abbie", "Tice"))
    check("'Abbie Tice' matches the card 'Abigail (Abbie) Tice'",
          t is not None and t.index == 0, str(e))
    t, m, e = _select_target(nicknames, 2, _attendee("2", "Ivan", "Alvarez Tarter"))
    check("'Ivan Alvarez Tarter' matches the accented/hyphenated card",
          t is not None and t.index == 1, str(e))

    # 6. Lone checkbox on a one-attendee registration is safe to take even
    #    when the card name is a preferred name we can't reconcile.
    solo = [_target(0, None, None, "A.Z. Suarez")]
    t, m, e = _select_target(solo, 1, _attendee("3", "Alexander", "Suarez"))
    check("single-attendee registration resolves via sole_checkbox",
          t is not None and m == "sole_checkbox", str(e))

    print("_select_target — fails closed rather than clicking the wrong person")

    # 7. Two attendees, ours identifiable by neither id nor name → refuse.
    t, m, e = _select_target(no_ids, 2, _attendee("9", "Grace", "Limbu"))
    check("unidentifiable attendee on a 2-person reg → no click",
          t is None and e is not None, f"got {m}")

    # 8. A lone checkbox is NOT taken when the page holds several attendees
    #    (e.g. only one of the siblings was assigned the WFR form).
    one_box_two_people = [_target(0, None, None, "Destiny Chukwunyekwam")]
    t, m, e = _select_target(one_box_two_people, 2, daniel)
    check("lone checkbox on a 2-person reg → no click", t is None and e is not None, f"got {m}")

    # 9. If the id anchor and the name anchor disagree, trust neither.
    crossed = [
        _target(0, "388414-159570963-1", "159570963", "Daniel Chukwunyekwam"),
        _target(1, "388414-159570964-1", "159570964", "Destiny Chukwunyekwam"),
    ]
    t, m, e = _select_target(crossed, 2, daniel)
    check("id/name disagreement → no click", t is None and e is not None, f"got {m}")

    # 10. Nothing rendered at all → reported, not clicked.
    t, m, e = _select_target([], 1, daniel)
    check("no checkboxes on page → no click", t is None and e is not None)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
