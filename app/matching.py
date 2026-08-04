"""Rules engine: does a Plaid transaction correspond to a benefit redemption?

Only benefits with `tracking_mode: plaid_auto` in the catalog are candidates —
that field already encodes the thing this module would otherwise have to
guess: whether a credit posts as a normal statement line Plaid can see, versus
landing in an app wallet (Uber Cash) or requiring a booking channel Plaid has
no visibility into (FHR hotel credit). Matching against the other 27 benefits
would just manufacture false positives.

Two independent signals feed a confidence score:
  - merchant match: the transaction's name contains one of the benefit's
    `detection_hint.merchant_patterns` (case-insensitive substring).
  - issuer-credit shape: negative Plaid amount (money credited back on a
    credit-card account) or a description that looks like an issuer-generated
    statement credit (for benefits with no merchant pattern to check against,
    e.g. Resy credits, CSR's auto-applied travel credit).

High confidence (merchant match + amount fits the period cap + date inside
that period's window) auto-applies. Anything weaker — or two benefits that
both plausibly match the same transaction — goes to manual review. Never
guess between two live candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import periods

# Generic phrasing issuers use for a statement credit/adjustment they generated
# themselves, as opposed to a normal merchant charge. Used only for benefits
# whose detection_hint has no merchant_patterns to check instead.
ISSUER_CREDIT_RE_PARTS = [
    "credit", "statement credit", "cash back", "cashback",
    "adjustment", "offer credit", "auto-appli", "reimburs",
]

# Paying your card bill also arrives as a negative (money-in) amount, so the
# sign test alone can't tell it apart from a statement credit. Real data:
# "AUTOPAY PAYMENT - THANK YOU" (-$76.23) was scoring 0.3 against the Resy
# credit purely because the dollar figure happened to fit its quarterly cap.
# A payment is never a benefit redemption, so it's excluded before scoring.
PAYMENT_RE_PARTS = [
    "autopay", "auto pay", "payment - thank", "payment thank",
    "online payment", "mobile payment", "payment received",
    "electronic payment", "pymt", "thank you",
]

AUTO_APPLY_THRESHOLD = 0.9
REVIEW_THRESHOLD = 0.3

# Real credits never post for more than their period cap; a small tolerance
# absorbs rounding, not a fundamentally different amount.
AMOUNT_TOLERANCE = 1.01


@dataclass
class Candidate:
    benefit_id: str
    benefit_name: str
    confidence: float
    reason: str
    window_label: str | None
    window_start: str
    window_end: str
    allowance: float


@dataclass
class MatchResult:
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return max(self.candidates, key=lambda c: c.confidence) if self.candidates else None

    @property
    def status(self) -> str:
        """unmatched | auto_matched | needs_review — NOT a final DB write; the
        caller still applies the manual-override guard before trusting
        auto_matched.

        Ambiguity is judged only among TOP-TIER candidates (confidence >=
        AUTO_APPLY_THRESHOLD), not against every weak guess. A no-pattern
        benefit's "the amount happens to fit this window" candidate sits at
        confidence 0.3-0.5 and will coincidentally fire on almost any small
        transaction within its (often quarterly/annual, so wide) window —
        letting that veto a genuine merchant-matched 0.95 elsewhere would
        make auto-matching nearly impossible on any card that has even one
        no-pattern benefit. A weak candidate only matters when it's the best
        we've got.
        """
        best = self.best
        if best is None:
            return "unmatched"
        top_tier = [c for c in self.candidates if c.confidence >= AUTO_APPLY_THRESHOLD]
        if len(top_tier) == 1:
            return "auto_matched"
        if len(top_tier) > 1:
            return "needs_review"  # two genuinely strong matches — a real conflict
        if best.confidence >= REVIEW_THRESHOLD:
            return "needs_review"
        return "unmatched"


def _is_issuer_credit_shaped(name: str) -> bool:
    n = (name or "").lower()
    return any(p in n for p in ISSUER_CREDIT_RE_PARTS)


def _merchant_match(name: str, patterns: list[str]) -> bool:
    n = (name or "").upper()
    return any(p.upper() in n for p in patterns)


def _window_for_date(cadence: str, year: int, txn_date: date) -> periods.Window | None:
    for w in periods.periods_in_year(cadence, year):
        if w.contains(txn_date):
            return w
    return None


def score_benefit(txn_name: str, txn_amount: float, txn_date: date, benefit: dict) -> Candidate | None:
    """None if this benefit isn't even eligible (window/amount don't fit at
    all) — a Candidate otherwise, confidence 0..1."""
    cadence = benefit.get("cadence", "annual")
    year = txn_date.year
    win = _window_for_date(cadence, year, txn_date)
    if win is None:
        return None

    # A window that existed on the calendar but was dead for this benefit
    # (pre-valid_from, or already discontinued) isn't a real match — same
    # boundary periods.py's grid()/available_for_year() already enforce.
    vf, vu = benefit.get("valid_from"), benefit.get("valid_until")
    if (vu and win.start > vu) or (vf and win.end < vf):
        return None

    windows = periods.periods_in_year(cadence, year)
    idx = next((i for i, w in enumerate(windows) if w is win), None)
    allowance = periods.period_allowance(benefit, year, idx)
    amount = abs(txn_amount)
    if amount <= 0 or amount > allowance * AMOUNT_TOLERANCE:
        return None

    hint = benefit.get("detection_hint") or {}
    patterns = hint.get("merchant_patterns")

    common = dict(benefit_id=benefit["id"], benefit_name=benefit["name"],
                  window_label=win.label, window_start=win.start.isoformat(),
                  window_end=win.end.isoformat(), allowance=allowance)

    if patterns:
        if not _merchant_match(txn_name, patterns):
            return None
        return Candidate(confidence=0.95,
                         reason=f"merchant matched + ${amount:,.2f} fits {win.label}'s ${allowance:,.2f} cap",
                         **common)

    # No merchant pattern to check — fall back to the issuer-credit-shape
    # signal, which can only ever justify a review-queue entry, never auto-apply.
    if _is_issuer_credit_shaped(txn_name):
        return Candidate(confidence=0.5,
                         reason=f"looks like an issuer credit, ${amount:,.2f} fits {win.label}'s ${allowance:,.2f} cap",
                         **common)
    return Candidate(confidence=0.3,
                     reason=f"${amount:,.2f} fits {win.label}'s ${allowance:,.2f} cap, no merchant/description signal",
                     **common)


def match_transaction(txn_name: str, txn_amount: float, txn_date: date, benefits: list[dict]) -> MatchResult:
    """`benefits` should already be filtered to this transaction's card and to
    tracking_mode == 'plaid_auto' — this function doesn't re-check either,
    so a caller who forgets to filter will get false matches against every
    benefit on the card."""
    candidates = []
    for b in benefits:
        c = score_benefit(txn_name, txn_amount, txn_date, b)
        if c is not None:
            candidates.append(c)
    return MatchResult(candidates=candidates)


def is_payment(txn_name: str) -> bool:
    """A payment TO the card (autopay, online payment) — money-in like a
    credit, but never a benefit redemption."""
    n = (txn_name or "").lower()
    return any(p in n for p in PAYMENT_RE_PARTS)


def is_candidate_transaction(txn_name: str, txn_amount: float) -> bool:
    """First-pass filter before even trying to match: a credit shows as a
    negative Plaid amount, OR (belt-and-suspenders) the description itself
    reads like an issuer-generated credit even if the sign looks off.
    Card payments are excluded outright — see PAYMENT_RE_PARTS."""
    if is_payment(txn_name):
        return False
    return txn_amount < 0 or _is_issuer_credit_shaped(txn_name)
