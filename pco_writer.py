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

  • Picking the RIGHT person's checkbox. Every consent checkbox carries a
    DOM id of the form
        {form_id}-{attendee_id}-{ordinal}
    e.g. 388414-159570963-1 (Youth Camp WFR form 388414, attendee
    159570963) and 388510-159641176-1 (Dream Team WFR form 388510).
    The middle segment is the same attendee id the Registrations API
    returns — confirmed against the live DOM on 2026-07-30, where the
    per-attendee "Edit" control on the same card posts to
    /attendees/{that id}/edit. So we anchor on attendee_id and never on
    page position.

    This replaces the pre-2026-07-30 approach, which filtered
    "div, section, article" by has_text=name and took .first. Playwright
    orders locator matches by DOM position, not by size, so .first
    resolved to the OUTERMOST wrapper — a div containing every attendee
    on the page — and the subsequent .get_by_role("checkbox").first then
    always hit the first attendee's checkbox. On any registration with
    two or more attendees that checked the wrong person's box (and left
    the intended person unchecked), which is exactly the failure Gio
    reported on 2026-07-30.

  • Resolution ladder in _select_target, strictly fail-closed — if we
    cannot prove which checkbox belongs to this attendee we click
    NOTHING and report the result as "failed":
        1. attendee_id   — DOM id middle segment == attendee.attendee_id
        2. card_name     — the attendee card's heading matches the name
        3. card_text     — the name appears in exactly one card's text
        4. sole_checkbox — a single WFR checkbox on a single-attendee
                           registration, so there is nothing to confuse
    Tier 1 is cross-checked against the name: if some OTHER card's
    heading matches this attendee's name, the id lookup is treated as
    untrustworthy and we refuse rather than guess.

  • Multiple matches can share a registration_id. We group matches by
    registration so each page loads exactly once.

  • Re-auth detection: after every page.goto we check the resulting URL
    for /login, churchcenter.com, or id.planningcenteronline.com. If any
    appear, raise ReAuthNeededError. We NEVER attempt headless login.

  • State verification: read checkbox state BEFORE click, skip if already
    checked, read AGAIN after click, raise PostClickMismatchError if the
    new state isn't checked. We also re-read EVERY WFR checkbox on the
    page after the click and raise if any box other than the target
    changed state — a regression alarm for exactly the cross-attendee bug
    above. main.py catches both errors, halts the run, and emails the alert.

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
# Name normalization is owned by pco_matcher — reuse it verbatim so the card
# text on the page is compared exactly the way the matcher compared the
# waiver list (accents, hyphens, parentheticals, nicknames).
from pco_matcher import _attendee_forms as attendee_name_forms
from pco_matcher import _canonicalize as canonicalize_name
from pco_matcher import _normalize as normalize_name
from pco_matcher import _paren_expand as paren_expand

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
    # Which rung of the _select_target ladder identified this attendee's own
    # checkbox: "attendee_id" | "card_name" | "card_text" | "sole_checkbox".
    target_method: Optional[str] = None
    # How many attendees the registration page carried — 2+ means this was a
    # sibling/family registration, the case the old selector got wrong.
    attendees_on_page: int = 0


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


@dataclass
class WfrTarget:
    """One WFR consent checkbox on a registration page, with its owner."""
    index: int              # DOM order among the page's WFR checkboxes
    dom_id: Optional[str]   # "{form_id}-{attendee_id}-{ordinal}"
    attendee_id: Optional[str]  # middle segment of dom_id, if parseable
    checked: bool
    card_name: str          # first line of the attendee card — their name
    card_text: str          # the whole card's text, for containment fallback


# Runs in the page. Collects every checkbox whose <label> text is the WFR
# label, and for each one walks UP to the highest ancestor that still holds
# exactly one WFR checkbox — that ancestor is the per-attendee card, because
# one level higher would swallow a sibling attendee's checkbox. Deliberately
# free of PCO class names so a styling change can't break it. All string
# normalization happens back in Python (see attendee_name_forms).
_PROBE_WFR_TARGETS_JS = r"""
(labelText) => {
  const squash = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const want = squash(labelText);
  const labelOf = (b) =>
    (b.labels && b.labels[0] ? b.labels[0].innerText : "") ||
    b.getAttribute("aria-label") || "";
  const all = [...document.querySelectorAll('input[type="checkbox"]')];
  const isWfr = (b) => squash(labelOf(b)) === want;
  const wfr = all.filter(isWfr);

  // Distinct attendees represented anywhere on the page, from the id scheme
  // shared by every per-attendee form checkbox (not just the WFR one).
  const attendees = new Set();
  all.forEach((b) => {
    const m = (b.id || "").match(/^\d+-(\d+)-\d+$/);
    if (m) attendees.add(m[1]);
  });

  return {
    attendeeCount: attendees.size,
    targets: wfr.map((b, i) => {
      let node = b, card = b;
      while (node.parentElement) {
        node = node.parentElement;
        const inside = [...node.querySelectorAll('input[type="checkbox"]')].filter(isWfr);
        if (inside.length === 1) card = node; else break;
      }
      const m = (b.id || "").match(/^\d+-(\d+)-\d+$/);
      const lines = card.innerText.split("\n").map((s) => s.trim()).filter(Boolean);
      return {
        index: i,
        domId: b.id || null,
        attendeeId: m ? m[1] : null,
        checked: b.checked,
        cardName: lines[0] || "",
        cardText: card.innerText.slice(0, 1200),
      };
    }),
  };
}
"""


def _probe_wfr_targets(page: Page, label: str) -> tuple[list[WfrTarget], int]:
    """Every WFR checkbox on the current page, plus the page's attendee count."""
    data = page.evaluate(_PROBE_WFR_TARGETS_JS, label)
    targets = [
        WfrTarget(
            index=t["index"],
            dom_id=t["domId"],
            attendee_id=t["attendeeId"],
            checked=bool(t["checked"]),
            card_name=t["cardName"],
            card_text=t["cardText"],
        )
        for t in data["targets"]
    ]
    return targets, int(data["attendeeCount"])


def _card_name_forms(card_name: str) -> set[str]:
    """Normalized comparison forms for a card heading, mirroring the matcher."""
    forms: set[str] = set()
    for piece in paren_expand(card_name):
        n = normalize_name(piece)
        if n:
            forms.add(n)
            forms.add(canonicalize_name(n))
    return forms


def _select_target(
    targets: list[WfrTarget], attendee_count: int, attendee: Attendee
) -> tuple[Optional[WfrTarget], str, Optional[str]]:
    """Identify THIS attendee's own WFR checkbox.

    Returns (target, method, error). Fail-closed: any ambiguity returns
    (None, "", reason) so the caller records a failure instead of clicking
    somebody else's box.
    """
    if not targets:
        return None, "", "no WFR checkbox rendered on the registration page"

    name_forms = attendee_name_forms(attendee)[1]  # normalized + nickname forms
    name_hits = [t for t in targets if _card_name_forms(t.card_name) & name_forms]

    # 1. The DOM id carries the attendee id — the authoritative anchor.
    by_id = [t for t in targets if t.attendee_id and t.attendee_id == attendee.attendee_id]
    if len(by_id) == 1:
        chosen = by_id[0]
        if name_hits and chosen not in name_hits:
            others = ", ".join(repr(t.card_name) for t in name_hits)
            return None, "", (
                f"attendee id {attendee.attendee_id} points at the card for "
                f"{chosen.card_name!r}, but {attendee.full_name}'s name is on "
                f"{others} — refusing to click either"
            )
        return chosen, "attendee_id", None
    if len(by_id) > 1:
        return None, "", (
            f"{len(by_id)} WFR checkboxes on the page carry attendee id "
            f"{attendee.attendee_id}"
        )

    # 2. Card heading matches the attendee's name.
    if len(name_hits) == 1:
        return name_hits[0], "card_name", None
    if len(name_hits) > 1:
        return None, "", (
            f"{len(name_hits)} attendee cards match the name {attendee.full_name!r} "
            f"and no DOM id carries attendee id {attendee.attendee_id}"
        )

    # 3. Name appears somewhere inside exactly one card.
    text_hits = [
        t for t in targets
        if any(form in normalize_name(t.card_text) for form in name_forms)
    ]
    if len(text_hits) == 1:
        return text_hits[0], "card_text", None

    # 4. One checkbox on a one-attendee registration — nothing to confuse.
    if len(targets) == 1 and attendee_count <= 1:
        return targets[0], "sole_checkbox", None

    cards = ", ".join(repr(t.card_name or "?") for t in targets)
    return None, "", (
        f"could not identify {attendee.full_name}'s own WFR checkbox among "
        f"{len(targets)} on this registration ({attendee_count} attendees; "
        f"cards: {cards})"
    )


def _locator_for(page: Page, target: WfrTarget, label: str):
    if target.dom_id:
        return page.locator(f'input[type="checkbox"][id="{target.dom_id}"]')
    return page.get_by_role("checkbox", name=label).nth(target.index)


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

    targets, attendee_count = _probe_wfr_targets(page, label)
    target, target_method, why_not = _select_target(targets, attendee_count, attendee)
    if target is None:
        return WriteResult(
            waiver, attendee, action="failed",
            confidence=match.confidence, method=match.method,
            error=why_not,
            attendees_on_page=attendee_count,
        )

    checkbox = _locator_for(page, target, label)

    try:
        is_checked = checkbox.is_checked(timeout=ELEMENT_WAIT_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return WriteResult(
            waiver, attendee, action="failed",
            confidence=match.confidence, method=match.method,
            error=(
                f"Located {attendee.full_name}'s WFR checkbox (via {target_method}, "
                f"id={target.dom_id}) but it never became readable"
            ),
            target_method=target_method,
            attendees_on_page=attendee_count,
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
            target_method=target_method,
            attendees_on_page=attendee_count,
        )

    if dry_run:
        return WriteResult(
            waiver, attendee, action="would_check",
            confidence=match.confidence, method=match.method,
            before_screenshot=str(before_path) if before_path else None,
            target_method=target_method,
            attendees_on_page=attendee_count,
        )

    # PCO renders each checkbox as a hidden <input> with a styled <label
    # for="…"> overlay that intercepts pointer events. A normal click
    # times out fighting the overlay; force=True lands the click on the
    # input directly, and the label's `for` link still routes the
    # state change per standard HTML semantics. Verified on the live
    # Mayuki Corrigan registration page 2026-05-14.
    checkbox.check(force=True)
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

    # Collateral check: on a multi-attendee registration, confirm we moved
    # only this attendee's box. Catches any future recurrence of the
    # wrong-sibling bug the moment it happens instead of a week later.
    after_targets, _ = _probe_wfr_targets(page, label)
    before_state = {t.dom_id: t.checked for t in targets if t.dom_id}
    for t in after_targets:
        if not t.dom_id or t.dom_id == target.dom_id:
            continue
        if t.dom_id in before_state and t.checked != before_state[t.dom_id]:
            raise PostClickMismatchError(
                f"Checking WFR for {attendee.full_name} "
                f"(attendee_id={attendee.attendee_id}, checkbox {target.dom_id}) also "
                f"changed {t.card_name or 'another attendee'}'s box ({t.dom_id}) on the "
                f"same registration. Halting run."
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
        target_method=target_method,
        attendees_on_page=attendee_count,
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
