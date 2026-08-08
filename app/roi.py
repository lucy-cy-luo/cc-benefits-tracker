"""Three-tier value math, the keep/cancel verdict, and the view model.

    available  = what the issuer advertises. The number Kudos shows you.
    realistic  = what you'd plausibly capture, per your own judgment.
    actual     = what you've recorded capturing.

The verdict runs on REALISTIC. Running it on available makes every premium card
look unconditionally worth keeping; running it on actual makes a card you simply
forgot to use look like a bad product. Realistic is the only tier that answers
"should I keep paying for this."

Two refusals are deliberate:
  - Cards whose value_thesis is points/hybrid return PENDING, not a number.
  - Cards with unresolved `applicable: None` benefits return INCOMPLETE.
Both are stated plainly rather than guessed.

`build_state()` produces the whole payload the UI renders from — one pass, one
source of truth, so a number can never disagree with itself between views.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import db, matching, periods


# --- verdict -----------------------------------------------------------------

def verdict(real: float, cost: float, unresolved: int, thesis: str,
            has_points_data: bool = True) -> tuple[str, str]:
    """`real` is the value the verdict runs against. For hybrid/points cards the
    caller folds points-realized into it; for credits cards it's credit value.

    PENDING is now conditional: a points/hybrid card stays PENDING only until
    its first points redemption is logged — that was always the missing input,
    not a permanent refusal. Once points exist, the normal keep/cancel math runs.
    """
    if thesis in ("points", "hybrid") and not has_points_data:
        return ("pending", "Assessing — needs points")
    if unresolved > 0:
        return ("incomplete", f"{unresolved} left to decide")
    d = real - cost
    # Plain-English: is the value you get above or below what the card costs?
    if d >= 0:
        return ("keep", f"KEEP · ${d:,.0f} over fee")
    return ("cancel", f"DROP · ${abs(d):,.0f} under fee")


# --- points redemptions ------------------------------------------------------

def points_math(r: dict) -> dict:
    """Derive realized value and cents-per-point for one redemption.

    The honest attribution when a redemption MIXES this card's transferred
    points with points the user already held in the partner program: credit
    the card only its SHARE of the total partner points spent.

        effective = card_points * (1 + bonus/100)          # after transfer bonus
        share     = effective / partner_points_total        # capped at 1.0
        realized  = cash_value * share
        cpp       = realized / card_points * 100            # cents per CARD point

    With no partner_points_total (direct booking, or no mixing), share = 1.0 and
    the whole cash_value is attributed to the card.
    """
    cp = float(r["card_points"] or 0)
    cash = float(r["cash_value"] or 0)
    bonus = float(r.get("transfer_bonus_pct") or 0)
    ppt = r.get("partner_points_total")
    effective = cp * (1 + bonus / 100.0)
    if ppt and float(ppt) > 0:
        share = min(1.0, effective / float(ppt))
    else:
        share = 1.0
    realized = cash * share
    cpp = (realized / cp * 100) if cp else 0.0
    return {
        "realized": round(realized, 2),
        "cpp": round(cpp, 3),
        "effective_partner_points": round(effective),
        "card_share": round(share, 4),
    }


def _points_view(card, year, counts_in_verdict):
    prog = card.get("points_program")
    if not prog:
        return None
    rows = db.points_redemptions_for(card["id"])
    items, realized, total_pts = [], 0.0, 0.0
    for r in rows:
        if not str(r["date"]).startswith(str(year)):
            continue
        m = points_math(r)
        realized += m["realized"]
        total_pts += float(r["card_points"] or 0)
        items.append({**r, **m})
    avg_cpp = (realized / total_pts * 100) if total_pts else 0.0
    return {
        "program": prog.get("name", ""),
        "short": prog.get("short", "pts"),
        "realized": round(realized, 2),
        "avg_cpp": round(avg_cpp, 3),
        "count": len(items),
        "points_used": round(total_pts),
        "counts_in_verdict": counts_in_verdict,
        "items": items,
    }


def _avg_cpp_all_time(card_id: str) -> float | None:
    """All-time (not year-scoped) average cents-per-point across every logged
    redemption for this card — a steadier rate than one year's redemptions,
    used to VALUE rent points, not to display a per-year figure."""
    rows = db.points_redemptions_for(card_id)
    realized = sum(points_math(r)["realized"] for r in rows)
    total_pts = sum(float(r["card_points"] or 0) for r in rows)
    return (realized / total_pts * 100) if total_pts else None


# How issuers describe the annual fee on a statement line. Matching the charge
# itself is the only way to know the true renewal date — a hand-entered date
# goes stale silently the moment a product change or retention offer moves the
# anniversary, and a cancel deadline you can't trust is worse than none.
FEE_PATTERNS = ("annual membership fee", "annual fee", "membership fee",
                "card member fee", "annual card fee")
FEE_AMOUNT_TOLERANCE = 0.02      # fee amounts are exact; allow only rounding


def _shift_year(d: date) -> date:
    """Anniversary math that survives Feb 29."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def _find_last_fee_charge(card_id: str, fee_amounts: list[float]) -> dict | None:
    """Most recent annual-fee charge Plaid can see for this card.

    Requires BOTH description and amount to match. Description alone would
    catch a 'membership fee' for a gym; amount alone would catch any
    coincidental charge of the same size.
    """
    best = None
    for r in db.plaid_transactions_between(card_id, "1900-01-01", "2999-12-31"):
        name, amt = (r["name"] or "").lower(), float(r["amount"])
        if amt <= 0 or not any(p in name for p in FEE_PATTERNS):
            continue
        if not any(abs(amt - f) <= FEE_AMOUNT_TOLERANCE for f in fee_amounts):
            continue
        if best is None or r["date"] > best["date"]:
            best = {"date": r["date"], "amount": amt, "name": r["name"]}
    return best


def _fee_schedule(card: dict, card_id: str, cost: float, today: date) -> dict:
    """When the next annual fee lands, and how much runway is left to decide.

    Prefers the real charge Plaid observed; falls back to the catalog's
    hand-entered renewal_date, flagged unverified so the UI can say so rather
    than implying a precision it doesn't have.
    """
    amounts = [float(f) for f in (card.get("annual_fee"), cost,
                                  card.get("authorized_user_fee")) if f]
    observed = _find_last_fee_charge(card_id, amounts)

    if observed:
        last = date.fromisoformat(observed["date"])
        nxt, source, verified = _shift_year(last), "plaid", True
    else:
        last, verified, source = None, False, "catalog"
        nxt = card.get("renewal_date")
        if not nxt:
            return {"next_due": None, "days_until": None, "last_charged": None,
                    "verified": False, "source": "unknown", "decide_by": None,
                    "decision_due": False}
    while nxt < today:                       # card held for several years
        nxt = _shift_year(nxt)

    days = (nxt - today).days
    return {
        "next_due": nxt.isoformat(),
        "days_until": days,
        "last_charged": last.isoformat() if last else None,
        "verified": verified,
        "source": source,
        # Issuers refund a fee only for a short window after it posts, so the
        # real deadline sits BEFORE the charge, not on it.
        "decide_by": (nxt - timedelta(days=14)).isoformat(),
        "decision_due": days <= 45,
    }


def _statement_window(year: int, month: int, close_day: int) -> tuple[date, date]:
    """The qualifying window for `month`'s rent points: the statement period
    ending on close_day OF that month, i.e. [prev month close_day+1 .. month
    close_day]. Confirmed against real data — the two competing readings
    (calendar month, and the window STARTING in that month) both contradict
    the user's logged points."""
    end = date(year, month, close_day)
    if month == 1:
        start = date(year - 1, 12, close_day + 1)
    else:
        start = date(year, month - 1, close_day + 1)
    return start, end


def _tier_for(tiers: list[dict], pct: float) -> dict:
    """Tiers are a step function — take the highest one whose threshold is met."""
    hit = tiers[0]
    for t in tiers:
        if pct >= t["spend_pct_of_rent"]:
            hit = t
    return hit


def _bilt_statement_months(catalog, card_id, year, today, logged_by_month):
    """Per-month statement view driving the rent-points suggestions.

    Deliberately produces SUGGESTIONS, never writes. A month the user has
    already filled in is left alone; if the model disagrees with their figure
    that surfaces as a flag, because a disagreement means the model is wrong
    and that's worth seeing rather than hiding.
    """
    spec = catalog.raw.get("bilt_points_model") or {}
    if spec.get("card") != card_id:
        return None
    tiers = sorted(spec.get("tiers") or [], key=lambda t: t["spend_pct_of_rent"])
    if not tiers:
        return None

    close_day = int(spec.get("statement_close_day", 9))
    charge_pat = (spec.get("housing_charge_pattern") or "").upper()
    net_refunds = bool(spec.get("refunds_reduce_spend"))

    out = []
    for month in range(1, 13):
        start, end = _statement_window(year, month, close_day)
        rows = db.plaid_transactions_between(card_id, start.isoformat(), end.isoformat())

        # Rent for THIS month comes from the housing charge Plaid actually saw
        # (rent moves), falling back to the catalog figure only if absent.
        m_start, m_end = periods.month_bounds(year, month)
        rent = 0.0
        for r in db.plaid_transactions_between(card_id, m_start.isoformat(), m_end.isoformat()):
            if charge_pat and charge_pat in (r["name"] or "").upper() and r["amount"] > 0:
                rent = float(r["amount"])
                break

        spend = 0.0
        for r in rows:
            name, amt = r["name"] or "", float(r["amount"])
            if charge_pat and charge_pat in name.upper():
                continue                      # the rent charge itself never qualifies
            if matching.is_payment(name):
                continue                      # paying the bill isn't spend
            if amt > 0:
                spend += amt
            elif net_refunds:
                spend += amt                  # amt is negative -> nets out

        spend = max(0.0, spend)
        closed = today > end
        has_data = bool(rows) or rent > 0
        pct = (100.0 * spend / rent) if rent else 0.0
        tier = _tier_for(tiers, pct) if rent else None

        projected = None
        if tier is not None:
            if tier.get("multiplier"):
                projected = int(rent * tier["multiplier"])   # floor, per real data
            else:
                projected = int(tier.get("flat_points") or 0)

        # A logged 0 on a month that DID pay rent is a placeholder, not a real
        # figure: the model's floor is the flat tier (250 pts), so zero is
        # unreachable whenever a housing charge exists. Treating it as unset
        # lets the suggestion through instead of the cell silently blocking it.
        logged = logged_by_month.get(month)
        if logged == 0 and rent > 0:
            logged = None
        out.append({
            "month": month,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "closed": closed,
            "has_data": has_data,
            "rent": round(rent, 2),
            "spend": round(spend, 2),
            "pct_of_rent": round(pct, 1),
            "multiplier": (tier or {}).get("multiplier"),
            "projected_points": projected,
            "next_tier": _next_tier_gap(tiers, pct, rent, projected) if (not closed and rent) else None,
            # Only suggest for a closed window we actually have data for, on a
            # month the user hasn't filled in themselves.
            "suggest": bool(closed and has_data and rent and logged is None and projected is not None),
            # Model vs. the user's own figure — surfaced, never auto-corrected.
            "disagrees": bool(logged is not None and projected is not None
                              and closed and has_data and rent and abs(logged - projected) > 1),
        })
    return out


def _next_tier_gap(tiers, pct, rent, projected):
    """For an OPEN window: what more spend would buy. The multiplier is a step
    function, so the marginal dollar near a boundary is worth far more than
    its headline 2x — that's the whole point of surfacing this before close."""
    nxt = next((t for t in tiers if t["spend_pct_of_rent"] > pct), None)
    if not nxt or not rent:
        return None
    need = rent * nxt["spend_pct_of_rent"] / 100.0 - rent * pct / 100.0
    gain = int(rent * nxt["multiplier"]) - (projected or 0)
    return {"pct": nxt["spend_pct_of_rent"], "multiplier": nxt["multiplier"],
            "spend_needed": round(max(0.0, need), 2), "points_gained": max(0, gain)}


def _rent_points_view(catalog, card_id, year):
    """Bilt Option A: value points EARNED on rent this year, not points redeemed.
    Earning is what the card is actually held for, and it's durable (Bilt points
    don't expire) — unlike valuing by redemption, this doesn't swing to a false
    'DROP' in a year you accumulate without cashing in yet.

    Rate: your own all-time average redemption cpp once you have any logged,
    otherwise a conservative flat 1c/pt (Bilt points commonly transfer above
    that, so this is a floor, not an optimistic guess).
    """
    spec = catalog.raw.get("bilt_points_model") or {}
    if spec.get("card") != card_id:
        return None
    rows = db.rent_points_for(card_id, year)
    total_points = sum(r["points"] for r in rows)
    rate = _avg_cpp_all_time(card_id)
    rate_is_default = rate is None
    if rate is None:
        rate = 1.0
    value = total_points * rate / 100
    return {
        "total_points": round(total_points),
        "rate_cpp": round(rate, 3),
        "rate_is_default": rate_is_default,
        "value": round(value, 2),
        "months": sorted(rows, key=lambda r: r["month"]),
    }


# --- benefit view ------------------------------------------------------------

def _tri(v):
    """SQLite hands back 1/0; the UI needs true/false/null.

    This matters more than it looks: in JavaScript `0 === false` is false, so an
    un-normalized integer would silently break every `applicable === false`
    check in the client and leave N/A'd credits rendering as active.
    """
    return None if v is None else bool(v)


def _cadence_label(cadence: str, allowance: float, available: float) -> str:
    """'$150 twice a year · $300 total' beats 'semiannual' — the reader shouldn't
    have to do the multiplication themselves to know what a cadence means."""
    a, t = f"${allowance:,.0f}", f"${available:,.0f}"
    if cadence == "monthly":
        return f"{a}/month · {t} total"
    if cadence == "quarterly":
        return f"{a} each quarter · {t} total"
    if cadence == "semiannual":
        return f"{a} twice a year · {t} total"
    if cadence == "annual":
        return f"{t}/year"
    if cadence == "every_4_years":
        return f"{t} every 4 years"
    if cadence == "per_booking":
        return f"{t}/year · redeem any time before Dec 31"
    return t


def _benefit_view(b, card, cs, bstate, year, today):
    st = bstate.get(b["id"], {})
    enrolled, applicable = _tri(st.get("enrolled")), _tri(st.get("applicable"))
    rv = st.get("realistic_value")

    avail = periods.available_for_year(b, year)
    cur_idx = periods.current_index(b.get("cadence"), today) if periods.has_grid(b) else None
    allowance = periods.period_allowance(b, year, cur_idx)
    win = periods.current_window(b, card, today)
    expired = periods.is_expired(b, today)
    live = periods.is_live(b, cs, today)

    year_redeemed = db.redeemed_in_year(b["id"], year)
    period_redeemed = (
        db.redeemed_in_period(b["id"], win.start.isoformat(), win.end.isoformat())
        if win else year_redeemed
    )

    cells = periods.grid(
        b, year, today,
        lambda s, e, _id=b["id"]: db.redeemed_in_period(_id, s, e),
    ) if periods.has_grid(b) else []

    if applicable:
        realistic = float(rv) if rv is not None else avail
    else:
        realistic = 0.0

    days = win.days_left(today) if win else None
    remaining = max(0.0, allowance - period_redeemed) if not (expired or applicable is False) else 0.0

    return {
        "kind": "benefit",
        "id": b["id"],
        "card": card["id"],
        "name": b["name"],
        "short_name": b.get("short_name") or b["name"],
        "cadence": b.get("cadence", "annual"),
        "cadence_label": _cadence_label(b.get("cadence", "annual"), allowance, avail),
        "category": b.get("category", "other"),
        "tracking_mode": b.get("tracking_mode", "app_only_manual"),
        "note": (b.get("notes") or "").strip(),
        "group": b.get("group"),
        "available": round(avail, 2),
        "allowance": round(allowance, 2),
        "realistic": round(realistic, 2),
        "period_redeemed": round(period_redeemed, 2),
        "year_redeemed": round(year_redeemed, 2),
        "remaining": round(remaining, 2),
        "enrolled": enrolled,
        "applicable": applicable,
        "realistic_value": rv,
        "window_label": win.label if win else None,
        "window_end": win.end.isoformat() if win else None,
        "days_left": days,
        "urgency": "expired" if expired else periods.urgency(days),
        "expired": expired,
        "live": live,
        "disputed": bool(b.get("verification_status") == "DISPUTED" or b.get("anchor_disputed")),
        "spend_gated": bool(b.get("spend_gate")),
        "grid": {
            "unit": periods.GRID_UNITS.get(b.get("cadence"), ""),
            "cells": cells,
        } if cells else None,
    }


# --- bilt cash ---------------------------------------------------------------

def _cash_channel_view(ch_id, name, cap, year, today, overlap=False, best_use=False, text=""):
    """One channel's 12-month cell strip + YTD. Shared by catalog-defined
    channels (Grubhub, Lyft, ...) and user-added custom ones — both are just a
    (id, name, monthly cap) tuple as far as the ledger is concerned."""
    cells = []
    ytd = 0.0
    for m in range(1, 13):
        start, end = periods.month_bounds(year, m)
        amt = db.cash_burned(ch_id, start.isoformat(), end.isoformat())
        ytd += amt
        if amt > 0:
            state = "done"
        elif m - 1 < today.month - 1:
            state = "missed"
        elif m - 1 == today.month - 1:
            state = "open"
        else:
            state = "future"
        cells.append({"index": m - 1, "label": periods.SHORT_LABELS["monthly"][m - 1],
                      "allowance": cap, "redeemed": round(amt, 2), "state": state})
    return {
        "id": ch_id, "name": name, "cap": cap,
        "overlap": overlap, "best_use": best_use, "text": text,
        "custom": False,
        "ytd": round(ytd, 2), "cells": cells,
    }


def _cash_view(catalog, card_id, year, today):
    """Bilt Cash is a currency, not a credit: earn/burn, spent through monthly
    capped channels. Redeemed and balance are DERIVED from the ledger so logging
    a burn moves the balance, the Bilt total and the Overview in one pass."""
    spec = catalog.bilt_cash
    if not spec or spec.get("card") != card_id:
        return None

    earned = db.cash_earned(year)
    channels = []
    redeemed = 0.0
    wasted = 0.0
    for ch in spec.get("channels", []):
        cap = ch.get("monthly_cap")
        if not cap:                      # housing is rate-based, not a monthly cap
            continue
        view = _cash_channel_view(ch["id"], ch["name"], cap, year, today,
                                   overlap=bool(ch.get("overlap_warning")),
                                   best_use=bool(ch.get("best_use")),
                                   text=ch.get("overlap_text", ""))
        redeemed += view["ytd"]
        if view["overlap"]:
            wasted += view["ytd"]
        channels.append(view)

    # User-added channels — Bilt lets you redeem into categories the catalog
    # doesn't enumerate. No overlap detection here: we don't know what other
    # card might duplicate a category the user typed in themselves.
    for cc in db.all_custom_channels():
        view = _cash_channel_view(cc["id"], cc["name"], cc["monthly_cap"], year, today)
        view["custom"] = True
        redeemed += view["ytd"]
        channels.append(view)

    balance = earned - redeemed
    housing = next((c for c in spec.get("channels", []) if not c.get("monthly_cap")), None)
    return {
        "earned": round(earned, 2),
        "redeemed": round(redeemed, 2),
        "balance": round(balance, 2),
        # realistic = redeemed, not earned. This is the same "capture is not
        # realization" rule every other benefit follows: an unspent balance is
        # a number sitting in an account, not value you've actually gotten.
        # Available already counts the full `earned` amount — that's honest,
        # it's really there to spend. But claiming it as REALISTIC before it's
        # spent would be assuming 100% redemption with no evidence, the exact
        # mistake the three-tier model exists to catch everywhere else.
        "realistic": round(redeemed, 2),
        "at_risk": round(max(0.0, balance - 100), 2),
        "expiry_rule": (spec.get("expiry") or {}).get("rule", ""),
        "wasted_on_overlap": round(wasted, 2),
        "channels": channels,
        "housing_note": (housing or {}).get("value_warning", "").strip(),
    }


# --- assembly ----------------------------------------------------------------

def build_state(catalog, today: date | None = None) -> dict:
    today = today or date.today()
    year = today.year
    bstate, cstate = db.all_benefit_state(), db.all_card_state()

    cards = []
    for cid, card in catalog.cards.items():
        cs = cstate.get(cid) or catalog.card_state_for(cid)
        views = [_benefit_view(b, card, cs, bstate, year, today)
                 for b in catalog.benefits_for(cid)]

        avail = real = act = 0.0
        unresolved = 0
        unresolved_value = 0.0
        for v, b in zip(views, catalog.benefits_for(cid)):
            gate = b.get("spend_gate")
            blocked = bool(gate and not cs.get("spend_gate_met"))
            # Count anything that had value available this year OR was captured —
            # a credit redeemed before it sunset still counts toward the year.
            if blocked or (v["available"] <= 0 and v["year_redeemed"] <= 0):
                continue
            avail += v["available"]
            real += v["realistic"]
            act += v["year_redeemed"]
            if v["applicable"] is None and v["live"]:
                unresolved += 1
                unresolved_value += v["available"]

        cash = _cash_view(catalog, cid, year, today)
        cost = catalog.annual_cost(cid)
        thesis = card.get("value_thesis", "credits")

        # Bilt: card credits + Bilt Cash are one relationship with one fee.
        tot_avail = avail + (cash["earned"] if cash else 0)
        tot_real = real + (cash["realistic"] if cash else 0)
        tot_act = act + (cash["redeemed"] if cash else 0)

        # Two different "points count toward the verdict" mechanisms, mutually
        # exclusive per card:
        #   - rent_points (Bilt, Option A): value points EARNED this year. This
        #     is what the card is actually held for, and durable (doesn't expire).
        #   - hybrid cards: value points REDEEMED this year (no card currently
        #     uses this — CSR moved to credits-thesis — but the mechanism stays
        #     for a future hybrid card).
        # A card never counts both; redemptions still get logged/shown for Bilt
        # (and refine the rent-points valuation rate) but flagged bonus-only.
        rent_points = _rent_points_view(catalog, cid, year)
        statement_months = (
            _bilt_statement_months(catalog, cid, year, today,
                                   {m["month"]: m["points"] for m in rent_points["months"]})
            if rent_points is not None else None
        )
        counts_via_redemption = thesis == "hybrid"
        points = _points_view(card, year, counts_via_redemption)

        if rent_points is not None:
            bonus_points_value = rent_points["value"]
            has_points_data = rent_points["total_points"] > 0
        elif counts_via_redemption:
            bonus_points_value = points["realized"] if points else 0.0
            has_points_data = bool(points and points["count"] > 0)
        else:
            bonus_points_value = 0.0
            has_points_data = True   # credits-thesis cards are never blocked on points

        verdict_counts_points = thesis in ("points", "hybrid")
        verdict_value = tot_real + (bonus_points_value if verdict_counts_points else 0.0)

        entries = _group_entries(views, catalog, today)
        # A large unredeemed Bilt Cash balance is a real, dollar-denominated
        # thing to act on even when its Dec 31 deadline is months out — it
        # needs to pull attention the same way an undecided credit does,
        # not wait for the 30-day "expiring" window like a monthly credit does.
        attention = _attention_count(entries) + (1 if cash and cash["at_risk"] > 0 else 0)
        cards.append({
            "id": cid,
            "name": card["name"],
            "label": _short_label(card["name"]),
            "cost": cost,
            "fee": card["annual_fee"],
            "au_fee": card.get("authorized_user_fee"),
            "thesis": thesis,
            "credits": {"available": round(avail, 2), "realistic": round(real, 2),
                        "actual": round(act, 2)},
            "available": round(tot_avail, 2),
            "realistic": round(tot_real, 2),
            "actual": round(tot_act, 2),
            "gap": round(max(0.0, tot_real - tot_act), 2),
            "unresolved": unresolved,
            "unresolved_value": round(unresolved_value, 2),
            "verdict": verdict(verdict_value, cost, unresolved, thesis, has_points_data),
            "fee_schedule": _fee_schedule(card, cid, cost, today),
            "verdict_value": round(verdict_value, 2),
            "attention": attention,
            "cash": cash,
            "points": points,
            "rent_points": rent_points,
            "statement_months": statement_months,
            "entries": entries,
        })

    reference = [
        {**r, "card_label": _short_label(catalog.cards[r["card"]]["name"])}
        for r in catalog.reference
    ]

    return {
        "today": today.isoformat(),
        "year": year,
        "cards": cards,
        "expiring": _expiring(cards, 30),
        "reference": reference,
    }


def _short_label(name: str) -> str:
    n = name.lower()
    if "platinum" in n and "bilt" not in n:
        return "Platinum"
    if "gold" in n:
        return "Gold"
    if "sapphire" in n:
        return "CSR"
    if "bilt" in n:
        return "Bilt"
    return name


def _group_entries(views, catalog, today):
    """Collapse credits sharing a `group` into one row that expands to a matrix."""
    specs = catalog.raw.get("groups", {}) or {}
    out, seen = [], {}
    for v in views:
        g = v.get("group")
        if not g:
            out.append(v)
            continue
        if g not in seen:
            spec = specs.get(g, {})
            seen[g] = {
                "kind": "group", "id": f"grp-{g}", "key": g,
                "title": spec.get("title", g.title()),
                "blurb": (spec.get("blurb") or "").strip(),
                "members": [],
            }
            out.append(seen[g])
        seen[g]["members"].append(v)

    for e in out:
        if e["kind"] != "group":
            continue
        ms = e["members"]
        e["cadence"] = ms[0]["cadence"]
        e["allowance"] = round(sum(m["allowance"] for m in ms), 2)
        e["available"] = round(sum(m["available"] for m in ms), 2)
        e["period_redeemed"] = round(sum(m["period_redeemed"] for m in ms), 2)
        e["year_redeemed"] = round(sum(m["year_redeemed"] for m in ms), 2)
        e["remaining"] = round(sum(m["remaining"] for m in ms), 2)
        e["days_left"] = ms[0]["days_left"]
        e["window_label"] = ms[0]["window_label"]
        e["urgency"] = ms[0]["urgency"]
        e["undecided"] = sum(1 for m in ms if m["applicable"] is None)
        e["applicable"] = False if all(m["applicable"] is False for m in ms) else None \
            if any(m["applicable"] is None for m in ms) else True
        e["tracking_mode"] = ms[0]["tracking_mode"]
        e["cadence_label"] = _cadence_label(e["cadence"], e["allowance"], e["available"])
    return out


def _attention_count(entries) -> int:
    n = 0
    for e in entries:
        ms = e["members"] if e["kind"] == "group" else [e]
        if any(m["applicable"] is None for m in ms):
            n += 1
        elif e.get("days_left") is not None and e["days_left"] <= 30 \
                and e.get("remaining", 0) > 0 and e.get("applicable") is not False:
            n += 1
    return n


def bilt_tier_status(catalog, mtd_nonrent_spend: float) -> dict | None:
    """Where you sit against the Bilt housing multiplier cliff.

    The multiplier is a step function: missing 100% by a dollar costs 0.25x on
    the ENTIRE rent payment. Below the top tier a marginal Bilt dollar is worth
    ~3 points; above it, 2. That's the difference between Bilt being the best
    card in the wallet for non-bonus spend and being mediocre.
    """
    m = catalog.bilt_points
    if not m:
        return None
    rent = m.get("housing_payment_monthly")
    if not rent:
        return None

    tiers = sorted(m["tiers"], key=lambda t: t["spend_pct_of_rent"])
    pct = 100.0 * mtd_nonrent_spend / rent

    current = tiers[0]
    for t in tiers:
        if pct >= t["spend_pct_of_rent"]:
            current = t
    nxt = next((t for t in tiers if t["spend_pct_of_rent"] > pct), None)

    out = {
        "rent": rent,
        "mtd_spend": mtd_nonrent_spend,
        "pct": pct,
        "current_multiplier": current.get("multiplier"),
        "current_points": (rent * current["multiplier"]) if current.get("multiplier")
                          else current.get("flat_points", 0),
        "next_tier": None,
    }
    if nxt:
        need = rent * nxt["spend_pct_of_rent"] / 100 - mtd_nonrent_spend
        gain = rent * nxt["multiplier"] - out["current_points"]
        out["next_tier"] = {
            "pct": nxt["spend_pct_of_rent"],
            "multiplier": nxt["multiplier"],
            "spend_needed": max(0.0, need),
            "points_gained": gain,
            # 2x on the marginal spend itself, plus the housing ratchet.
            "marginal_rate": (gain + need * 2) / need if need > 0 else 0,
        }
    return out


def _expiring(cards, within_days: int):
    """Unused value with a deadline inside the window. Grouped credits collapse
    into one entry that navigates to the group row."""
    out = []
    for c in cards:
        for e in c["entries"]:
            if e.get("applicable") is False:
                continue
            d = e.get("days_left")
            if d is None or d > within_days or e.get("remaining", 0) <= 0:
                continue
            out.append({
                "card": c["id"], "card_label": c["label"],
                "target": e["id"],
                "name": e.get("title") or e.get("name"),
                "amount": round(e["remaining"], 2),
                "days": d,
                "urgency": "red" if d < 10 else "orange",
                "by_month": bool(e.get("grid") or e["kind"] == "group"),
            })
    return sorted(out, key=lambda x: (x["days"], -x["amount"]))
