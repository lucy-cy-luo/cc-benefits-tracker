"""FastAPI app. Binds to loopback only — this is local-first by construction.

Rendering approach: the server owns all the math (`roi.build_state`) and ships it
as one JSON payload; the page renders from that payload and re-renders after each
action. One rendering path, no build step, no CDN — and actions patch in place
instead of doing a full-page 303 round trip, which matters when you're clicking
twelve month-cells in a row.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import calendar_feed, crypto, db, matching, periods, plaid_client, roi
from .catalog import Catalog

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
app = FastAPI(title="CC Benefits Tracker")
# Plaid Link's hosted iframe (cdn.plaid.com) may probe our redirect_uri
# cross-origin before completing an OAuth handoff (Amex, Chase, etc.) — with
# no CORS handling at all, that preflight fails and Link can abort silently,
# with nothing visible in our own server logs or Plaid's own Link analytics.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://([a-zA-Z0-9-]+\.)*plaid\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.middleware("http")
async def one_db_connection_per_request(request: Request, call_next):
    """See db.session(): collapses ~300 per-query connections into one."""
    with db.session():
        return await call_next(request)


app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

CATALOG = Catalog()
db.init(CATALOG)


def _today() -> date:
    override = os.getenv("TODAY_OVERRIDE")
    return date.fromisoformat(override) if override else date.today()


def _state():
    s = roi.build_state(CATALOG, _today())
    s["plaid_items"] = _plaid_items_view()
    s["review_queue"] = _review_queue_view()
    s["sync_status"] = _sync_status(s["plaid_items"], _today())
    return s


# Staleness thresholds, in days since the OLDEST linked card last synced.
# Sync runs unattended now, so its failure mode is silence: launchd stops, the
# tunnel dies, or a Plaid token expires, and the app keeps serving month-old
# numbers as though they were current. Showing the age is what turns a silent
# failure into something noticeable.
SYNC_STALE_DAYS = 2
SYNC_VERY_STALE_DAYS = 7


def _sync_status(items: list[dict], today: date) -> dict:
    if not items:
        return {"state": "none", "oldest": None, "days": None,
                "label": "No cards connected"}
    synced = [i["last_synced_at"] for i in items if i.get("last_synced_at")]
    if len(synced) < len(items):
        return {"state": "never", "oldest": None, "days": None,
                "label": "Some cards have never synced"}
    oldest = min(synced)
    days = (today - date.fromisoformat(oldest)).days
    state = ("fresh" if days < SYNC_STALE_DAYS
             else "stale" if days < SYNC_VERY_STALE_DAYS else "very_stale")
    label = ("Synced today" if days == 0
             else "Synced yesterday" if days == 1
             else f"Last synced {days} days ago")
    return {"state": state, "oldest": oldest, "days": days, "label": label}


def _benefit(benefit_id: str):
    b = CATALOG.benefits.get(benefit_id)
    if not b:
        raise HTTPException(404, f"unknown benefit {benefit_id}")
    return b


# --- page --------------------------------------------------------------------

def _asset_version() -> str:
    """Cache-buster derived from the static files' own mtimes.

    Without it the browser holds onto app.js indefinitely and quietly runs
    stale code against fresh data — which looks exactly like a bug in the app
    and cost real debugging time more than once.
    """
    d = ROOT / "app" / "static"
    return str(int(max(f.stat().st_mtime for f in d.glob("app.*"))))


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "benefits_tracker.html",
                                      {"v": _asset_version()})


@app.get("/api/state")
def api_state():
    return _state()


# --- actions (each returns the full fresh state so the UI can re-render) ------

@app.post("/api/period")
def log_period(benefit_id: str = Form(...), index: int = Form(...), amount: float = Form(None)):
    """Set exactly what was redeemed in one window of a periodic credit.

    Idempotent "set", not "toggle": always clears whatever was logged for that
    window first, then re-adds `amount` if it's positive. This is what makes a
    partial amount editable — clicking a window you've already logged reopens
    it pre-filled with the real figure instead of only letting you wipe it.
    Omitting `amount` defaults to the full period allowance (the one-click
    "I used all of it" path); passing 0 is how the UI clears a window.
    """
    b = _benefit(benefit_id)
    today = _today()
    wins = periods.periods_in_year(b.get("cadence"), today.year)
    if not (0 <= index < len(wins)):
        raise HTTPException(400, "period index out of range")
    w = wins[index]
    start, end = w.start.isoformat(), w.end.isoformat()

    db.delete_redemptions_in_window(benefit_id, start, end)
    cap = periods.period_allowance(b, today.year, index)
    # Clamped, not rejected: a fat-fingered 250 instead of 25 should self-correct
    # to the period's real cap, not fail the save and lose what was typed.
    amt = cap if amount is None else min(cap, max(0.0, amount))
    if amt > 0:
        when = min(max(today, w.start), w.end).isoformat()
        db.add_redemption(benefit_id, round(amt, 2), when, w.label, "logged from grid")
    return _state()


@app.post("/api/redeem")
def redeem(benefit_id: str = Form(...), amount: float = Form(...), when: str = Form(None)):
    """Log against a credit with no period grid (annual / opportunity-based)."""
    b = _benefit(benefit_id)
    today = _today()
    win = periods.current_window(b, CATALOG.cards[b["card"]], today)
    db.add_redemption(benefit_id, amount, when or today.isoformat(),
                      win.label if win else None, "manual")
    return _state()


@app.post("/api/state/{benefit_id}")
def set_state(benefit_id: str, enrolled: str = Form(None), applicable: str = Form(None),
              realistic_value: str = Form(None)):
    """Manual decisions. These always win over anything auto-detected later."""
    _benefit(benefit_id)
    fields = {}
    if enrolled is not None:
        fields["enrolled"] = _tri(enrolled)
    if applicable is not None:
        fields["applicable"] = _tri(applicable)
    if realistic_value is not None:
        fields["realistic_value"] = float(realistic_value) if realistic_value.strip() else None
    if fields:
        db.set_benefit_state(benefit_id, **fields)
    return _state()


@app.post("/api/cash")
def log_cash(channel: str = Form(...), index: int = Form(...), amount: float = Form(None)):
    """Set exactly what was burned in one month of one Bilt Cash channel.

    Same idempotent "set" semantics as /api/period, for the same reason: a
    channel's monthly spend is rarely the full cap, and editing a logged month
    should update it rather than only be able to clear it. Balance is derived
    from these entries, so this moves the balance, the Bilt combined total and
    the Overview together.
    """
    spec = CATALOG.bilt_cash or {}
    ch = next((c for c in spec.get("channels", []) if c["id"] == channel), None)
    cap = ch.get("monthly_cap") if ch else None
    if cap is None:
        # Not a catalog channel — check user-added ones before giving up.
        custom = next((c for c in db.all_custom_channels() if c["id"] == channel), None)
        if not custom:
            raise HTTPException(404, f"unknown channel {channel}")
        cap = custom["monthly_cap"]
    if not (0 <= index < 12):
        raise HTTPException(400, "month index out of range")
    year = _today().year
    start, end = periods.month_bounds(year, index + 1)
    s, e = start.isoformat(), end.isoformat()
    db.delete_cash_in_window(channel, s, e)
    amt = cap if amount is None else min(cap, max(0.0, amount))
    if amt > 0:
        when = min(max(_today(), start), end).isoformat()
        db.add_bilt_cash("burn", float(amt), when, channel, "logged from grid")
    return _state()


@app.post("/api/cash/earn")
def earn_cash(amount: float = Form(...), when: str = Form(None), note: str = Form("")):
    db.add_bilt_cash("earn", amount, when or _today().isoformat(), None, note)
    return _state()


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "channel"


@app.post("/api/cash/channel")
def add_cash_channel(name: str = Form(...), monthly_cap: float = Form(...)):
    """Add a Bilt Cash channel the catalog doesn't enumerate.

    Persisted to SQLite like any other manual entry — reload the page and it's
    still there, shows up in the matrix exactly like Grubhub or Lyft, and its
    burns roll into the same balance and card total.
    """
    name = name.strip()
    if not name:
        raise HTTPException(400, "channel name required")
    if monthly_cap <= 0:
        raise HTTPException(400, "monthly cap must be positive")

    catalog_ids = {c["id"] for c in (CATALOG.bilt_cash or {}).get("channels", [])}
    existing = {c["id"] for c in db.all_custom_channels()}
    base = "custom_" + _slugify(name)
    cid, n = base, 2
    while cid in catalog_ids or cid in existing:
        cid = f"{base}_{n}"
        n += 1

    db.add_custom_channel(cid, name, monthly_cap)
    return _state()


# --- points redemptions ------------------------------------------------------

@app.post("/api/points")
def add_points(card_id: str = Form(...), card_points: float = Form(...),
               cash_value: float = Form(...), description: str = Form(""),
               partner: str = Form(""), transfer_bonus_pct: float = Form(0),
               partner_points_total: float = Form(None), date_: str = Form(None)):
    """Log a points redemption against a card. Realized value and cents-per-point
    are derived on read (roi.points_math), not stored."""
    card = CATALOG.cards.get(card_id)
    if not card:
        raise HTTPException(404, f"unknown card {card_id}")
    if not card.get("points_program"):
        raise HTTPException(400, f"{card_id} has no points program")
    if card_points <= 0 or cash_value <= 0:
        raise HTTPException(400, "card_points and cash_value must be positive")
    db.add_points_redemption(
        card_id, date_ or _today().isoformat(), description.strip(),
        card_points, cash_value, partner.strip() or None,
        transfer_bonus_pct or 0,
        partner_points_total if partner_points_total and partner_points_total > 0 else None,
    )
    return _state()


@app.post("/api/points/{rid}/delete")
def remove_points(rid: int):
    db.delete_points_redemption(rid)
    return _state()


# --- rent points (Bilt, Option A) --------------------------------------------

@app.post("/api/rent-points")
def set_rent_points(card_id: str = Form(...), year: int = Form(...), month: int = Form(...),
                    points: float = Form(...)):
    """Log (or correct — idempotent by month) points earned on rent in one
    month. Only valid for a card with a bilt_points_model in the catalog."""
    spec = CATALOG.raw.get("bilt_points_model") or {}
    if spec.get("card") != card_id:
        raise HTTPException(400, f"{card_id} has no rent-points model")
    if not (1 <= month <= 12):
        raise HTTPException(400, "month must be 1-12")
    if points < 0:
        raise HTTPException(400, "points must be non-negative")
    db.set_rent_points(card_id, year, month, points)
    return _state()


@app.post("/api/rent-points/{card_id}/{year}/{month}/delete")
def remove_rent_points(card_id: str, year: int, month: int):
    db.delete_rent_points(card_id, year, month)
    return _state()


# --- Plaid (Phase 2) ----------------------------------------------------------

@app.post("/api/plaid/link_token")
def plaid_link_token(card_id: str = Form(...)):
    if card_id not in CATALOG.cards:
        raise HTTPException(404, f"unknown card {card_id}")
    try:
        token = plaid_client.create_link_token(card_id, redirect_uri=os.getenv("PLAID_REDIRECT_URI") or None)
    except plaid_client.PlaidNotConfigured as e:
        raise HTTPException(400, str(e))
    return {"link_token": token}


@app.post("/api/plaid/exchange")
def plaid_exchange(card_id: str = Form(...), public_token: str = Form(...),
                   institution_name: str = Form(None)):
    """Link succeeded client-side; exchange the one-time public_token for a
    durable access_token, encrypt it before it ever reaches SQLite, then run
    an immediate first sync so linking a card shows results right away."""
    if card_id not in CATALOG.cards:
        raise HTTPException(404, f"unknown card {card_id}")
    try:
        access_token, item_id = plaid_client.exchange_public_token(public_token)
    except plaid_client.PlaidNotConfigured as e:
        raise HTTPException(400, str(e))
    db.upsert_plaid_item(card_id, item_id, crypto.encrypt(access_token), institution_name)
    _sync_card(card_id)
    return _state()


@app.post("/api/plaid/sync/{card_id}")
def plaid_sync(card_id: str):
    _sync_card(card_id)
    return _state()


@app.post("/api/plaid/disconnect/{card_id}")
def plaid_disconnect(card_id: str):
    """Remove a card's Plaid connection so it can be relinked — e.g. after
    switching PLAID_ENV from sandbox to production, the old sandbox Item's
    access token is worthless in production and would only error on sync."""
    db.delete_plaid_item(card_id)
    return _state()


def _sync_card(card_id: str) -> None:
    item = db.plaid_item_for_card(card_id)
    if not item:
        raise HTTPException(404, f"{card_id} is not connected to Plaid")
    access_token = crypto.decrypt(item["access_token_encrypted"])
    result = plaid_client.sync_transactions(access_token, item["cursor"])
    db.set_plaid_cursor(card_id, result["next_cursor"])

    benefits = [b for b in CATALOG.benefits_for(card_id) if _is_matchable(b)]
    for txn in result["added"] + result["modified"]:
        _process_transaction(card_id, item["item_id"], txn, benefits)
    for txn in result["removed"]:
        db.delete_plaid_transaction(_txn_field(txn, "transaction_id"))


def _is_matchable(benefit: dict) -> bool:
    """Which benefits the matcher is allowed to consider.

    `tracking_mode: plaid_auto` is the normal signal. `detection_hint.
    plaid_detectable` is the escape hatch for credits that DO post as a
    statement line (so Plaid can see them) but whose tracking_mode carries
    other meaning we don't want to lose — the FHR hotel credit stays
    `planned` so it keeps its PLAN AHEAD pill, while still being matchable.
    """
    if benefit.get("tracking_mode") == "plaid_auto":
        return True
    return bool((benefit.get("detection_hint") or {}).get("plaid_detectable"))


def _txn_field(txn, key, default=None):
    """Plaid's SDK models support dict-style access (documented) — this just
    guards against a raw dict slipping in from a test fixture."""
    return txn.get(key, default) if hasattr(txn, "get") else getattr(txn, key, default)


def _process_transaction(card_id: str, item_id: str, txn, benefits: list[dict]) -> None:
    txn_id = _txn_field(txn, "transaction_id")
    name = _txn_field(txn, "merchant_name") or _txn_field(txn, "name") or ""
    amount = float(_txn_field(txn, "amount"))
    when = str(_txn_field(txn, "date"))
    pending = bool(_txn_field(txn, "pending"))
    try:
        raw_json = json.dumps(txn.to_dict(), default=str)
    except Exception:
        raw_json = json.dumps({"name": name, "amount": amount, "date": when})

    existing = db.plaid_transaction(txn_id)
    # A transaction we already resolved (auto-applied, or the user confirmed
    # / rejected it) doesn't get re-matched on a later sync — Plaid's own
    # pending -> posted "modified" event would otherwise re-run matching and
    # risk creating a second redemption for the same real-world credit.
    already_resolved = bool(existing) and existing["match_status"] in (
        "auto_matched", "confirmed", "rejected",
    )

    db.upsert_plaid_transaction(card_id, item_id, txn_id, when, name, amount, pending, raw_json)
    if already_resolved:
        return

    if not matching.is_candidate_transaction(name, amount):
        db.set_transaction_match(txn_id, "unmatched")
        return

    result = matching.match_transaction(name, amount, date.fromisoformat(when), benefits)
    status = result.status
    candidates_json = json.dumps([
        {"benefit_id": c.benefit_id, "benefit_name": c.benefit_name,
         "confidence": c.confidence, "reason": c.reason, "window_label": c.window_label}
        for c in result.candidates
    ])

    if status == "auto_matched":
        best = result.best
        # Manual overrides always win: if this window already has a
        # non-Plaid redemption, a human already decided it — defer to
        # review instead of silently stacking a second entry on top.
        if db.has_non_plaid_redemption_in_window(best.benefit_id, best.window_start, best.window_end):
            status = "needs_review"
        else:
            rid = db.add_plaid_redemption(
                best.benefit_id, abs(amount), when, best.window_label,
                f"Plaid auto-match: {name}", txn_id,
            )
            db.set_transaction_match(txn_id, "auto_matched", best.benefit_id,
                                     best.confidence, candidates_json, rid)
            return

    if status == "needs_review":
        db.set_transaction_match(
            txn_id, "needs_review",
            match_confidence=result.best.confidence if result.best else None,
            candidates_json=candidates_json,
        )
    else:
        db.set_transaction_match(txn_id, "unmatched")


@app.get("/api/review")
def get_review_queue():
    return {"review_queue": _review_queue_view()}


@app.post("/api/review/{txn_id}/confirm")
def confirm_review(txn_id: str, benefit_id: str = Form(...)):
    """The user picks the right benefit for an ambiguous/weak match. Same
    write path as an auto-match (add_plaid_redemption + set_transaction_match)
    so it shows up identically everywhere the data is read from."""
    txn = db.plaid_transaction(txn_id)
    if not txn:
        raise HTTPException(404, f"unknown transaction {txn_id}")
    if txn["match_status"] != "needs_review":
        raise HTTPException(400, f"transaction is {txn['match_status']}, not awaiting review")
    benefit = CATALOG.benefits.get(benefit_id)
    if not benefit:
        raise HTTPException(404, f"unknown benefit {benefit_id}")

    _confirm_one(txn_id, txn, benefit_id)
    return _state()


@app.post("/api/review/{txn_id}/reject")
def reject_review(txn_id: str, reason: str = Form(None)):
    """`reason` distinguishes 'not a credit at all' (a refund) from 'wrong
    benefit'. Same outcome today, different signal — the first is what would
    teach the matcher which descriptions to stop offering."""
    txn = db.plaid_transaction(txn_id)
    if not txn:
        raise HTTPException(404, f"unknown transaction {txn_id}")
    db.set_transaction_match(txn_id, "rejected", reject_reason=reason)
    return _state()


@app.post("/api/review/bulk-confirm")
def bulk_confirm(txn_ids: str = Form(...), benefit_id: str = Form(...)):
    """Accept several near-identical suggestions at once. A queue of six
    Dunkin' matches is one decision, not six."""
    if benefit_id not in CATALOG.benefits:
        raise HTTPException(404, f"unknown benefit {benefit_id}")
    done = 0
    for tid in [t for t in txn_ids.split(",") if t]:
        txn = db.plaid_transaction(tid)
        if not txn or txn["match_status"] != "needs_review":
            continue
        _confirm_one(tid, txn, benefit_id)
        done += 1
    return _state()


def _confirm_one(txn_id: str, txn: dict, benefit_id: str) -> None:
    benefit = CATALOG.benefits[benefit_id]
    txn_date = date.fromisoformat(txn["date"])
    windows = periods.periods_in_year(benefit.get("cadence", "annual"), txn_date.year)
    win = next((w for w in windows if w.contains(txn_date)), None)
    rid = db.add_plaid_redemption(
        benefit_id, abs(txn["amount"]), txn["date"], win.label if win else None,
        f"Plaid (confirmed): {txn['name']}", txn_id)
    db.set_transaction_match(txn_id, "confirmed", benefit_id, 1.0, txn["candidates"], rid)


def _review_queue_view() -> list[dict]:
    out = []
    for t in db.review_queue():
        out.append({
            "id": t["id"], "card_id": t["card_id"], "date": t["date"], "name": t["name"],
            "amount": t["amount"], "match_confidence": t["match_confidence"],
            "candidates": json.loads(t["candidates"]) if t["candidates"] else [],
        })
    return out


def _plaid_items_view() -> list[dict]:
    return [
        {"card_id": it["card_id"], "institution_name": it["institution_name"],
         "last_synced_at": it["last_synced_at"]}
        for it in db.all_plaid_items()
    ]


# --- calendar feed -----------------------------------------------------------

@app.get("/calendar/{token}.ics")
def calendar_ics(token: str):
    """Read-only iCalendar feed for Google Calendar to subscribe to.

    Guarded by an unguessable token in the path rather than a login, because
    Google's feed fetcher can't complete an interactive auth challenge — the
    same pattern Google itself uses for a calendar's "secret address". The
    token grants READ of benefit names, amounts and deadlines only; it can't
    write anything, and it deliberately exposes no transactions or balances.
    """
    expected = os.getenv("CALENDAR_FEED_TOKEN")
    if not expected:
        raise HTTPException(404, "calendar feed is not enabled (set CALENDAR_FEED_TOKEN)")
    if not secrets.compare_digest(token, expected):
        raise HTTPException(404, "not found")
    ics = calendar_feed.build_ics(_state(), _today())
    return Response(content=ics, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


# --- digest (Phase 5 seam) ---------------------------------------------------

@app.get("/digest")
def digest():
    """Your weekly automation reads this. Credit-card benefits and card ROI only —
    subscription cancellation stays in the Renewal & Benefit Sweep."""
    s = _state()
    return {
        "generated": s["today"],
        "expiring_30d": [
            {"card": x["card_label"], "benefit": x["name"], "unused": x["amount"],
             "days_left": x["days"]}
            for x in s["expiring"]
        ],
        "cards": [
            {"name": c["name"], "annual_cost": c["cost"],
             "available": c["available"], "realistic": c["realistic"],
             "actual": c["actual"], "gap": c["gap"],
             "verdict": c["verdict"][0], "detail": c["verdict"][1],
             # The renewal deadline is the one thing in here that costs money
             # to miss, so a weekly sweep should lead with it.
             "fee_posts": (c["fee_schedule"] or {}).get("next_due"),
             "fee_days_away": (c["fee_schedule"] or {}).get("days_until"),
             "decide_by": (c["fee_schedule"] or {}).get("decide_by"),
             "fee_date_confirmed": (c["fee_schedule"] or {}).get("verified"),
             "membership_year_captured": (c["membership_year"] or {}).get("captured"),
             "membership_year_covers_fee": (c["membership_year"] or {}).get("covers_fee")}
            for c in s["cards"]
        ],
        # Surfaced so an unattended sweep can notice the data went stale
        # instead of quietly reporting month-old numbers as current.
        "sync": s["sync_status"],
        "needs_review": len(s["review_queue"]),
        "renewals_due": [
            {"card": c["label"], "fee": c["cost"],
             "posts": c["fee_schedule"]["next_due"],
             "days": c["fee_schedule"]["days_until"],
             "decide_by": c["fee_schedule"]["decide_by"],
             "verdict": c["verdict"][1]}
            for c in s["cards"]
            if (c["fee_schedule"] or {}).get("decision_due")
        ],
    }


def _tri(v: str):
    if v in ("", "null", "unknown", None):
        return None
    return 1 if v in ("1", "true", "yes", "on") else 0
