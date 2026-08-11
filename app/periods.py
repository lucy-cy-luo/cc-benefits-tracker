"""Period windows and reset logic.

This is the load-bearing module. Every "you have $X left and Y days to use it"
claim in the UI comes from here, so a bug here is a silently wrong app rather
than a crash.

Three things make this harder than it looks:
  1. cadence and anchor are independent. The CSR has an annual credit on the
     card anniversary, an annual credit on the calendar year, and semiannual
     credits on calendar halves — all on one card.
  2. valid_until can kill a benefit mid-year, so a "semiannual $100" benefit
     may only offer $50 in its final year (Amex Platinum Saks, 2026).
  3. Monthly credits are the ones that actually get forfeited, and they forfeit
     twelve times a year rather than once.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

# Cadences that reset on a schedule. Everything else is opportunity-based.
PERIODIC = {"monthly", "quarterly", "semiannual", "annual"}

PERIODS_PER_YEAR = {"monthly": 12, "quarterly": 4, "semiannual": 2, "annual": 1}

RED_DAYS = 10       # < 10 days left
ORANGE_DAYS = 30    # 10-30 days left


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    label: str

    def days_left(self, today: date) -> int:
        return (self.end - today).days

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def _eom(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of a month — the window Bilt Cash channels reset on."""
    return date(year, month, 1), _eom(year, month)


def _shift_years(d: date, n: int) -> date:
    """Anniversary math that survives Feb 29."""
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def current_window(benefit: dict, card: dict, today: date) -> Window | None:
    """The period the user is currently spending against.

    Returns None for continuous/every_4_years benefits — those have no fixed
    yearly deadline, so a countdown would be a guess.

    per_booking is the one opportunity-based cadence that DOES have a real
    deadline: the value doesn't carry over, so "any time before Dec 31" is a
    genuine countdown even though there's no periodic cap to reset (Gold's
    Hotel Collection credit: use it whenever, but it's gone at year end).
    """
    cadence = benefit.get("cadence")
    anchor = benefit.get("period_anchor", "calendar")

    if cadence == "monthly":
        return Window(
            today.replace(day=1),
            _eom(today.year, today.month),
            today.strftime("%b %Y"),
        )

    if cadence == "quarterly":
        q = (today.month - 1) // 3
        return Window(
            date(today.year, q * 3 + 1, 1),
            _eom(today.year, q * 3 + 3),
            f"Q{q + 1} {today.year}",
        )

    if cadence == "semiannual":
        h = 0 if today.month <= 6 else 1
        return Window(
            date(today.year, h * 6 + 1, 1),
            date(today.year, 6, 30) if h == 0 else date(today.year, 12, 31),
            f"H{h + 1} {today.year}",
        )

    if cadence == "annual":
        if anchor == "card_year":
            # A benefit may renew on a different anniversary than the fee.
            # Real case: this CSR was product-changed, so the fee bills on the
            # Sep product-change anniversary while the $300 travel credit
            # renews on the original account-opening anniversary in May.
            # Falling back to the fee date would promise a fresh $300 in
            # September that does not actually arrive until May.
            renewal = benefit.get("anniversary_date") or card.get("renewal_date")
            if not renewal:
                # Refuse to guess. A card-year benefit without a renewal date
                # would render a confidently wrong deadline.
                return None
            end = date(today.year, renewal.month, renewal.day)
            if end <= today:
                end = _shift_years(end, 1)
            start = _shift_years(end, -1)
            return Window(start, end - timedelta(days=1), f"card year to {end - timedelta(days=1):%b %d, %Y}")
        return Window(date(today.year, 1, 1), date(today.year, 12, 31), str(today.year))

    if cadence == "per_booking":
        return Window(date(today.year, 1, 1), date(today.year, 12, 31), str(today.year))

    # every_4_years / continuous: no fixed yearly deadline, no countdown.
    return None


def periods_in_year(cadence: str, year: int) -> list[Window]:
    """Every period a cadence offers in a calendar year, in order."""
    if cadence == "monthly":
        return [Window(date(year, m, 1), _eom(year, m), date(year, m, 1).strftime("%b %Y")) for m in range(1, 13)]
    if cadence == "quarterly":
        return [Window(date(year, q * 3 + 1, 1), _eom(year, q * 3 + 3), f"Q{q + 1} {year}") for q in range(4)]
    if cadence == "semiannual":
        return [
            Window(date(year, 1, 1), date(year, 6, 30), f"H1 {year}"),
            Window(date(year, 7, 1), date(year, 12, 31), f"H2 {year}"),
        ]
    if cadence == "annual":
        return [Window(date(year, 1, 1), date(year, 12, 31), str(year))]
    return []


def available_for_year(benefit: dict, year: int) -> float:
    """Value actually obtainable in a calendar year, honoring valid_from/until.

    This is why Amex Platinum Saks is $50 in 2026 and not $100: it offered one
    $50 half-year window before being discontinued on 2026-06-30. Reporting
    $100 would inflate the ROI denominator and score a fully-redeemed benefit
    as a 50% miss.
    """
    cadence = benefit.get("cadence")
    value = float(benefit.get("value") or 0)

    if cadence not in PERIODIC:
        # per_booking / continuous / every_4_years aren't annual pools.
        return value

    windows = periods_in_year(cadence, year)
    if not windows:
        return 0.0

    vf = benefit.get("valid_from")
    vu = benefit.get("valid_until")
    total = 0.0
    for i, w in enumerate(windows):
        if (vu and w.start > vu) or (vf and w.end < vf):
            continue
        total += period_allowance(benefit, year, i)
    return total


def period_allowance(benefit: dict, year: int, index: int | None = None) -> float:
    """How much this benefit offers in a single period.

    Most periodic benefits split evenly across their periods. A few genuinely
    don't — Amex Platinum's Uber Cash pays $15/mo Jan-Nov but $35 in December,
    not a flat $16.67. `period_overrides` is a sparse {index: amount} map on
    the benefit for those cases (0-based: month/quarter/half index within the
    year); every period NOT listed there still gets an even share of whatever
    of `value` the overrides didn't already claim.

    `index` selects which period's (possibly overridden) amount to return. Left
    as None, callers get the plain uniform default — used where no specific
    period is in play (e.g. available_for_year's now-per-period math still
    calls in here per index, so this default path is mostly for tests/callers
    that don't care which period).
    """
    cadence = benefit.get("cadence")
    value = float(benefit.get("value") or 0)
    if cadence not in PERIODIC:
        return value
    n = len(periods_in_year(cadence, year)) or 1
    overrides = {int(k): float(v) for k, v in (benefit.get("period_overrides") or {}).items()}
    if not overrides:
        return value / n
    if index is not None and index in overrides:
        return overrides[index]
    remaining_periods = n - len(overrides)
    remaining_value = value - sum(overrides.values())
    return remaining_value / remaining_periods if remaining_periods else 0.0


def is_expired(benefit: dict, today: date) -> bool:
    vu = benefit.get("valid_until")
    return bool(vu and today > vu)


def is_live(benefit: dict, card_state: dict, today: date) -> bool:
    """Should this benefit be offered to the user right now?"""
    if is_expired(benefit, today):
        return False
    vf = benefit.get("valid_from")
    if vf and today < vf:
        return False
    gate = benefit.get("spend_gate")
    if gate and not card_state.get("spend_gate_met"):
        return False
    return True


def urgency(days_left: int | None) -> str:
    """Color state. None => no deadline, never nag."""
    if days_left is None:
        return "neutral"
    if days_left < 0:
        return "expired"
    if days_left < RED_DAYS:
        return "red"
    if days_left <= ORANGE_DAYS:
        return "orange"
    return "neutral"


# --- period grids ------------------------------------------------------------
# A credit that resets on a period has to expose EVERY window of that period, not
# just the current one and a yearly total. An unused H1 on a semiannual credit is
# forfeited money; blended into one annual number it becomes invisible.

GRID_UNITS = {"monthly": "months", "quarterly": "quarters", "semiannual": "halves"}

SHORT_LABELS = {
    "monthly": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "quarterly": ["Q1", "Q2", "Q3", "Q4"],
    "semiannual": ["H1", "H2"],
}

LONG_LABELS = {
    "quarterly": ["Q1 · Jan–Mar", "Q2 · Apr–Jun", "Q3 · Jul–Sep", "Q4 · Oct–Dec"],
    "semiannual": ["H1 · Jan–Jun", "H2 · Jul–Dec"],
}


def has_grid(benefit: dict) -> bool:
    """Annual and opportunity-based credits have nothing to lay out."""
    return benefit.get("cadence") in GRID_UNITS


def current_index(cadence: str, today: date) -> int:
    if cadence == "monthly":
        return today.month - 1
    if cadence == "quarterly":
        return (today.month - 1) // 3
    if cadence == "semiannual":
        return 0 if today.month <= 6 else 1
    return 0


def grid(benefit: dict, year: int, today: date, redeemed_by_window) -> list[dict]:
    """One cell per window, with the state the UI colors by.

    `redeemed_by_window(start_iso, end_iso) -> float` is injected so this stays
    free of database concerns and trivially testable.

    States:
      done    - money captured in that window
      missed  - window closed with nothing captured (forfeited, and shown as such)
      open    - the window you can still act on
      future  - not yet open
      dead    - benefit was discontinued before this window (never attainable)
    """
    cadence = benefit.get("cadence")
    if cadence not in GRID_UNITS:
        return []

    windows = periods_in_year(cadence, year)
    cur = current_index(cadence, today) if today.year == year else len(windows)
    vf, vu = benefit.get("valid_from"), benefit.get("valid_until")

    cells = []
    for i, w in enumerate(windows):
        redeemed = redeemed_by_window(w.start.isoformat(), w.end.isoformat())
        dead = bool((vu and w.start > vu) or (vf and w.end < vf))
        if dead:
            state = "dead"
        elif redeemed > 0:
            state = "done"
        elif i < cur:
            state = "missed"
        elif i == cur:
            state = "open"
        else:
            state = "future"
        cells.append({
            "index": i,
            "label": SHORT_LABELS[cadence][i],
            "long_label": LONG_LABELS.get(cadence, SHORT_LABELS[cadence])[i],
            "start": w.start.isoformat(),
            "end": w.end.isoformat(),
            "allowance": round(period_allowance(benefit, year, i), 2),
            "redeemed": round(redeemed, 2),
            "state": state,
        })
    return cells
