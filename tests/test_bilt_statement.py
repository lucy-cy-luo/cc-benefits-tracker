"""Bilt rent-points statement model.

The multiplier that sets each month's rent points is driven by NON-RENT spend
during the statement window (10th -> 9th). These tests pin the model against
the two real months where both the spend and the user's own logged points are
known — June 2026 (223.5% -> 1.25x -> 2,996) and July 2026 (44.0% -> 0.5x ->
1,205) — plus the boundary behaviour the real data never exercised.
"""

import importlib
import os
import tempfile
from datetime import date

import pytest


@pytest.fixture()
def env():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    try:
        from app import db as db_mod
        importlib.reload(db_mod)
        from app import roi as roi_mod
        importlib.reload(roi_mod)
        from app.catalog import Catalog
        cat = Catalog()
        db_mod.init(cat)
        yield db_mod, roi_mod, cat
    finally:
        os.environ.pop("DATABASE_PATH", None)
        os.unlink(path)


CARD = "bilt_palladium"


def _txn(db_mod, when, name, amount, i=[0]):
    i[0] += 1
    db_mod.upsert_plaid_transaction(CARD, "item-1", f"t{i[0]}", when, name, amount, False, "{}")


def _month(roi_mod, cat, year, month, today, logged=None):
    months = roi_mod._bilt_statement_months(cat, CARD, year, today, logged or {})
    return next(m for m in months if m["month"] == month)


class TestStatementWindow:
    def test_window_is_prior_month_10th_through_this_month_9th(self, env):
        _, roi_mod, _ = env
        assert roi_mod._statement_window(2026, 8, 9) == (date(2026, 7, 10), date(2026, 8, 9))

    def test_january_window_reaches_back_into_december(self, env):
        _, roi_mod, _ = env
        assert roi_mod._statement_window(2026, 1, 9) == (date(2025, 12, 10), date(2026, 1, 9))


class TestAgainstRealLoggedMonths:
    """If either of these ever fails, the inferred model no longer explains
    the user's own data and should not be trusted to suggest anything."""

    def test_june_2026_reproduces_2996_points(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-06-02", "Bilt Housing Payment", 2397.57)
        _txn(db_mod, "2026-05-20", "Physio Logic", 5358.26)   # inside 05-10..06-09
        m = _month(roi_mod, cat, 2026, 6, date(2026, 8, 4))
        assert m["rent"] == 2397.57
        assert m["multiplier"] == 1.25
        assert m["projected_points"] == 2996

    def test_july_2026_reproduces_1205_points(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-06-15", "Physio Logic", 1060.80)   # inside 06-10..07-09
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["pct_of_rent"] == 44.0
        assert m["multiplier"] == 0.5
        assert m["projected_points"] == 1205


class TestExclusions:
    def test_the_rent_charge_itself_is_not_qualifying_spend(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["spend"] == 0.0

    def test_card_payments_are_not_qualifying_spend(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-07-01", "Autopay Payment", 5325.85)
        _txn(db_mod, "2026-06-20", "Payment - Bilt Housing", -2397.57)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["spend"] == 0.0

    def test_refunds_net_out_of_qualifying_spend(self, env):
        # User-confirmed rule: returns reduce the qualifying total.
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-06-15", "Some Store", 1000.00)
        _txn(db_mod, "2026-06-16", "Some Store", -400.00)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["spend"] == 600.00


class TestTierBoundaries:
    """Real data only ever landed at 44% and 216% — nowhere near an edge."""

    @pytest.mark.parametrize("spend,expected_mult", [
        (0.00, None),        # under 25% -> flat tier, no multiplier
        (602.79, None),      # a cent under 25% ($602.795)
        (602.80, 0.5),       # first cent that clears 25%
        (1205.59, 0.75),     # exactly 50%
        (1808.38, 0.75),     # a cent under 75% ($1,808.385) — still 0.75x
        (1808.39, 1.0),      # first cent that clears 75%
        (2411.18, 1.25),     # exactly 100%
        (9999.00, 1.25),     # far past the top tier
    ])
    def test_step_function_at_each_boundary(self, env, spend, expected_mult):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        if spend:
            _txn(db_mod, "2026-06-15", "Some Store", spend)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["multiplier"] == expected_mult

    def test_below_the_first_tier_still_earns_flat_points(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["projected_points"] == 250   # not 0


class TestSuggestionSafety:
    def test_no_suggestion_for_a_month_the_user_already_logged(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-06-15", "Some Store", 1060.80)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4), logged={7: 1205})
        assert m["suggest"] is False

    def test_no_suggestion_while_the_window_is_still_open(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-08-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-07-15", "Some Store", 4861.86)
        m = _month(roi_mod, cat, 2026, 8, date(2026, 8, 4))   # closes 08-09
        assert m["closed"] is False
        assert m["suggest"] is False

    def test_no_suggestion_when_plaid_never_saw_a_rent_charge(self, env):
        # Silence beats a guess — e.g. months before the card was linked.
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-06-15", "Some Store", 1000.00)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["rent"] == 0
        assert m["suggest"] is False

    def test_suggests_once_the_window_closes_and_month_is_empty(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-06-15", "Some Store", 1060.80)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["closed"] is True and m["suggest"] is True

    def test_a_logged_zero_is_treated_as_a_placeholder_not_a_real_figure(self, env):
        # The model's floor is the flat 250-point tier, so a rent-paying month
        # can never genuinely earn 0 — a 0 is someone holding the slot.
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-06-15", "Some Store", 1060.80)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4), logged={7: 0})
        assert m["suggest"] is True
        assert m["disagrees"] is False      # a placeholder isn't a disagreement

    def test_zero_is_left_alone_when_no_rent_was_charged(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-06-15", "Some Store", 1000.00)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4), logged={7: 0})
        assert m["suggest"] is False

    def test_disagreement_is_flagged_not_corrected(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        _txn(db_mod, "2026-06-15", "Some Store", 1060.80)   # -> 1,205
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4), logged={7: 9999})
        assert m["disagrees"] is True
        assert m["suggest"] is False        # never overwrites a user figure


class TestOpenWindowNudge:
    def test_names_the_spend_needed_to_reach_the_next_tier(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-08-02", "Bilt Housing Payment", 2400.00)
        _txn(db_mod, "2026-07-15", "Some Store", 1200.00)   # 50% -> 0.75x
        m = _month(roi_mod, cat, 2026, 8, date(2026, 8, 4))
        nt = m["next_tier"]
        assert nt["pct"] == 75
        assert nt["multiplier"] == 1.0
        assert nt["spend_needed"] == pytest.approx(600.00, abs=0.01)
        assert nt["points_gained"] == 600     # 2400*1.0 - 2400*0.75

    def test_no_nudge_once_the_window_has_closed(self, env):
        db_mod, roi_mod, cat = env
        _txn(db_mod, "2026-07-02", "Bilt Housing Payment", 2411.18)
        m = _month(roi_mod, cat, 2026, 7, date(2026, 8, 4))
        assert m["next_tier"] is None
