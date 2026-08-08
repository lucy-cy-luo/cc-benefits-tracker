"""iCalendar feed of what still needs redeeming.

Delivered as a SUBSCRIBED feed rather than events pushed through the Calendar
API, which buys three things:

  - No OAuth, no stored Google credentials, no token refresh.
  - No de-duplication logic. The feed is regenerated from live state on every
    fetch, so it is the source of truth by construction — a re-read can't
    create doubles the way a re-run of a push job can.
  - Events DISAPPEAR when you redeem something. A pushed event has to be
    hunted down and deleted; a feed entry simply stops being emitted.

Three tiers, deliberately different in urgency, because alerting on every
credit every month is how this kind of thing becomes noise you ignore:

  PLANNING     big windows that need lead time (booking a hotel isn't a
               same-day act) — fires well before the deadline.
  LAST CHANCE  a window about to close with real money still on it.
  SWEEP        one weekly all-day roll-up of everything small and open, so
               the $7 credits get a single line instead of their own events.

All events are all-day (DATE-valued). That's deliberate: timed events drag in
timezone handling, which is a rich source of off-by-one-day bugs, and none of
these deadlines are precise to the hour anyway.
"""

from __future__ import annotations

from datetime import date, timedelta

PRODID = "-//CC Benefits Tracker//EN"

# Lead times, in days before the window closes.
PLANNING_LEAD = 21
LAST_CHANCE_LEAD = 4

# Dollar floors. Below these a reminder costs more attention than it saves.
PLANNING_MIN = 100.0
LAST_CHANCE_MIN = 10.0
SWEEP_MIN = 1.0


def _esc(text: str) -> str:
    """RFC 5545 escaping: backslash first, or it double-escapes the others."""
    return (str(text).replace("\\", "\\\\").replace(";", r"\;")
            .replace(",", r"\,").replace("\n", r"\n"))


def _fold(line: str) -> str:
    """Lines cap at 75 octets; continuations start with a single space."""
    out, cur = [], line
    while len(cur.encode()) > 75:
        cut = 75
        while len(cur[:cut].encode()) > 75:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def _d(d: date) -> str:
    return d.strftime("%Y%m%d")


def _event(uid, start: date, summary, description, alarm_days=0):
    """All-day event. DTEND is exclusive in iCalendar, hence +1 day."""
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{date.today().strftime('%Y%m%dT000000Z')}",
        f"DTSTART;VALUE=DATE:{_d(start)}",
        f"DTEND;VALUE=DATE:{_d(start + timedelta(days=1))}",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(description)}",
        "TRANSP:TRANSPARENT",          # don't show as busy
    ]
    if alarm_days is not None:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                  f"TRIGGER:-P{alarm_days}D" if alarm_days else "TRIGGER:PT9H",
                  f"DESCRIPTION:{_esc(summary)}", "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def _open_entries(state):
    """Every actionable credit: still has unused value in an open window."""
    for card in state["cards"]:
        for e in card["entries"]:
            members = e["members"] if e["kind"] == "group" else [e]
            for m in members:
                if m.get("applicable") is False or m.get("expired"):
                    continue
                # A spend-gated credit you haven't unlocked isn't actionable —
                # reminding you to use it would just be noise you can't act on.
                if m.get("live") is False:
                    continue
                if not m.get("window_end") or m.get("remaining", 0) <= 0:
                    continue
                if m.get("days_left") is None or m["days_left"] < 0:
                    continue
                yield card, m


def _next_weekday(from_date: date, weekday: int) -> date:
    """Next occurrence of `weekday` (0=Mon) strictly after from_date."""
    delta = (weekday - from_date.weekday() - 1) % 7 + 1
    return from_date + timedelta(days=delta)


def build_ics(state, today: date | None = None, name: str = "Card Benefits") -> str:
    today = today or date.today()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(name)}",
             # Hint to clients how often to re-poll. Google treats this as
             # advisory only — it refreshes external feeds on its own cadence.
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
             "X-PUBLISHED-TTL:PT6H"]

    # The renewal decision. This is the only deadline here that costs real
    # money to miss: unused credits are upside forgone, but the fee is cash
    # charged. Fires early — cancelling or asking for a retention offer is a
    # phone call, not a same-day errand.
    for card in state["cards"]:
        f = card.get("fee_schedule") or {}
        if not f.get("next_due"):
            continue
        due = date.fromisoformat(f["next_due"])
        decide = date.fromisoformat(f["decide_by"]) if f.get("decide_by") else due
        provenance = ("confirmed from your last fee charge" if f.get("verified")
                      else "ESTIMATED date — not yet seen in your transactions")
        for lead, tag in ((45, "Review"), (14, "Decide on")):
            start = due - timedelta(days=lead)
            if start < today:
                continue
            lines += _event(
                f"fee-{card['id']}-{_d(due)}-{lead}@ccbt", start,
                f"{tag} {card['label']} — ${card['cost']:,.0f} fee posts {due:%b %d}",
                (f"Current verdict: {card['verdict'][1]}.\n"
                 f"${card['cost']:,.0f} annual fee posts {due:%b %d, %Y} ({provenance}).\n"
                 f"Cancel or ask for a retention offer before {decide:%b %d} — issuers "
                 f"refund the fee only briefly after it posts."
                 + ("\nThis card is currently NOT covering its fee."
                    if card["verdict"][0] == "cancel" else "")),
                alarm_days=0)

    swept = []
    for card, m in _open_entries(state):
        end = date.fromisoformat(m["window_end"])
        remaining = m["remaining"]
        planned = m.get("tracking_mode") == "planned"
        base = f"{card['label']} · {m['name']}"
        detail = (f"${remaining:,.0f} of ${m['available']:,.0f} still unused in "
                  f"{m.get('window_label') or 'this window'}. Closes {end:%b %d}.")

        # Tier 1 — needs lead time to act on at all.
        if remaining >= PLANNING_MIN or planned:
            start = end - timedelta(days=PLANNING_LEAD)
            if start >= today:
                lines += _event(
                    f"plan-{m['id']}-{_d(end)}@ccbt",
                    start, f"Plan: {base} (${remaining:,.0f})",
                    detail + ("\nThis one needs booking ahead — it can't be done on the last day."
                              if planned else ""),
                    alarm_days=0)

        # Tier 2 — closing, with real money on it.
        if remaining >= LAST_CHANCE_MIN:
            start = end - timedelta(days=LAST_CHANCE_LEAD)
            if start >= today:
                lines += _event(
                    f"last-{m['id']}-{_d(end)}@ccbt",
                    start, f"Last chance: {base} (${remaining:,.0f})",
                    detail, alarm_days=0)

        # Tier 3 — everything else open rolls into the weekly sweep.
        if remaining >= SWEEP_MIN:
            swept.append((card["label"], m["name"], remaining, end))

    # One weekly all-day roll-up, so small credits get a line rather than an
    # event each. Emitted for the next 4 Sundays.
    for wk in range(4):
        sunday = _next_weekday(today, 6) + timedelta(days=7 * wk)
        live = [s for s in swept if s[3] >= sunday]
        if not live:
            continue
        total = sum(s[2] for s in live)
        body = "\n".join(f"{lab} · {nm}: ${rem:,.0f} by {end:%b %d}"
                         for lab, nm, rem, end in sorted(live, key=lambda x: x[3]))
        lines += _event(f"sweep-{_d(sunday)}@ccbt", sunday,
                        f"Benefits sweep — ${total:,.0f} open",
                        body, alarm_days=0)

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
