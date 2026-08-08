"""The iCalendar feed. The load-bearing property is that it's a pure function
of current state — redeem something and its event stops being emitted, which
is what makes a subscribed feed safe to re-read forever without dedup logic."""

from datetime import date, timedelta

import pytest

from app import calendar_feed as cf


def _state(**over):
    m = {"kind": "benefit", "id": "b1", "name": "Hotel Credit", "remaining": 300.0,
         "available": 600.0, "window_end": "2026-09-30", "window_label": "H2 2026",
         "days_left": 60, "applicable": True, "expired": False,
         "tracking_mode": "planned"}
    m.update(over)
    return {"cards": [{"id": "c1", "label": "Platinum", "entries": [m]}]}


TODAY = date(2026, 8, 4)


def _uids(ics):
    return [l.split(":", 1)[1] for l in ics.splitlines() if l.startswith("UID:")]


class TestStructure:
    def test_is_a_wellformed_calendar(self):
        ics = cf.build_ics(_state(), TODAY)
        assert ics.startswith("BEGIN:VCALENDAR")
        assert ics.rstrip().endswith("END:VCALENDAR")
        assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT")

    def test_uses_crlf_line_endings(self):
        # RFC 5545 requires CRLF; Google is lenient but other clients aren't.
        assert "\r\n" in cf.build_ics(_state(), TODAY)

    def test_long_lines_are_folded_to_75_octets(self):
        ics = cf.build_ics(_state(name="x" * 200), TODAY)
        assert all(len(l.encode()) <= 75 for l in ics.split("\r\n") if l)

    def test_special_characters_are_escaped(self):
        ics = cf.build_ics(_state(name="Dining; Drinks, etc"), TODAY)
        assert r"\;" in ics and r"\," in ics


class TestTiers:
    def test_planning_event_fires_well_before_the_deadline(self):
        ics = cf.build_ics(_state(), TODAY)
        assert any(u.startswith("plan-b1") for u in _uids(ics))
        # 21 days before Sep 30
        assert "DTSTART;VALUE=DATE:20260909" in ics

    def test_last_chance_event_fires_just_before_the_deadline(self):
        ics = cf.build_ics(_state(), TODAY)
        assert any(u.startswith("last-b1") for u in _uids(ics))
        assert "DTSTART;VALUE=DATE:20260926" in ics   # 4 days before

    def test_small_credit_gets_no_individual_events_only_the_sweep(self):
        ics = cf.build_ics(_state(remaining=7.0, tracking_mode="plaid_auto"), TODAY)
        uids = _uids(ics)
        assert not any(u.startswith(("plan-", "last-")) for u in uids)
        assert any(u.startswith("sweep-") for u in uids)

    def test_a_planned_credit_gets_lead_time_even_when_small(self):
        # Booking-dependent credits can't be done on the last day regardless
        # of how little is left on them.
        ics = cf.build_ics(_state(remaining=25.0, tracking_mode="planned"), TODAY)
        assert any(u.startswith("plan-b1") for u in _uids(ics))

    def test_sweep_rolls_everything_open_into_one_event_per_week(self):
        ics = cf.build_ics(_state(remaining=7.0, tracking_mode="plaid_auto"), TODAY)
        sweeps = [u for u in _uids(ics) if u.startswith("sweep-")]
        assert len(sweeps) == len(set(sweeps))      # no duplicate weeks
        assert 1 <= len(sweeps) <= 4


class TestNothingToNagAbout:
    @pytest.mark.parametrize("over", [
        {"remaining": 0.0},                 # fully redeemed
        {"applicable": False},              # marked not applicable
        {"expired": True},                  # window already gone
        {"days_left": -3},                  # deadline passed
        {"window_end": None},               # no deadline at all
        {"live": False},                    # spend gate not met — can't act
    ])
    def test_produces_no_events(self, over):
        ics = cf.build_ics(_state(**over), TODAY)
        assert ics.count("BEGIN:VEVENT") == 0

    def test_redeeming_removes_the_event_rather_than_leaving_it_behind(self):
        before = cf.build_ics(_state(), TODAY)
        after = cf.build_ics(_state(remaining=0.0), TODAY)
        assert before.count("BEGIN:VEVENT") > 0
        assert after.count("BEGIN:VEVENT") == 0


class TestStability:
    def test_uids_are_stable_across_regeneration(self):
        # The whole dedup story rests on this: same window, same UID, so a
        # re-fetch updates events instead of piling up new ones.
        a = _uids(cf.build_ics(_state(), TODAY))
        b = _uids(cf.build_ics(_state(), TODAY))
        assert a == b and len(a) == len(set(a))

    def test_a_different_window_gets_a_different_uid(self):
        a = set(_uids(cf.build_ics(_state(), TODAY)))
        b = set(_uids(cf.build_ics(_state(window_end="2026-12-31"), TODAY)))
        assert not (a & {u for u in b if u.startswith(("plan-", "last-"))})

    def test_group_members_are_expanded_individually(self):
        st = {"cards": [{"id": "c1", "label": "CSR", "entries": [{
            "kind": "group", "id": "g1", "members": [
                dict(_state()["cards"][0]["entries"][0], id="m1", name="DoorDash A"),
                dict(_state()["cards"][0]["entries"][0], id="m2", name="DoorDash B"),
            ]}]}]}
        uids = _uids(cf.build_ics(st, TODAY))
        assert any("m1" in u for u in uids) and any("m2" in u for u in uids)


class TestEndpoint:
    def test_feed_is_404_without_the_right_token(self, monkeypatch):
        import importlib, os, tempfile
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        os.environ["DATABASE_PATH"] = path
        os.environ["CALENDAR_FEED_TOKEN"] = "right-token"
        try:
            from app import db as d; importlib.reload(d)
            from app import roi as r; importlib.reload(r)
            from app import main as mn; importlib.reload(mn)
            from fastapi.testclient import TestClient
            c = TestClient(mn.app)
            assert c.get("/calendar/wrong-token.ics").status_code == 404
            ok = c.get("/calendar/right-token.ics")
            assert ok.status_code == 200
            assert ok.headers["content-type"].startswith("text/calendar")
            assert ok.text.startswith("BEGIN:VCALENDAR")
        finally:
            os.environ.pop("DATABASE_PATH", None)
            os.environ.pop("CALENDAR_FEED_TOKEN", None)
            os.unlink(path)
