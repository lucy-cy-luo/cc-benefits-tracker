"""Period/reset logic — the rules engine.

These tests encode the specific mistakes that would make the app quietly wrong
rather than visibly broken.
"""

from datetime import date

import pytest

from app import periods

# --- fixtures mirroring real catalog entries --------------------------------

SAKS = {  # discontinued mid-2026
    "id": "amex_plat_saks", "cadence": "semiannual", "period_anchor": "calendar",
    "value": 100, "valid_until": date(2026, 6, 30),
}
CSR_TRAVEL = {  # the only card_year-anchored benefit
    "id": "csr_travel_credit", "cadence": "annual", "period_anchor": "card_year", "value": 300,
}
CSR_CARD = {"id": "chase_sapphire_reserve", "renewal_date": date(2026, 8, 31), "annual_fee": 795}
PLAT_CARD = {"id": "amex_platinum", "renewal_date": date(2027, 6, 16), "annual_fee": 895}
UBER = {"id": "amex_plat_uber_cash", "cadence": "monthly", "period_anchor": "calendar", "value": 200}
RESY = {"id": "amex_plat_resy", "cadence": "quarterly", "period_anchor": "calendar", "value": 400}
HOTEL = {"id": "amex_plat_hotel", "cadence": "semiannual", "period_anchor": "calendar", "value": 600}
SELECT_2026 = {  # 2026-only promo
    "id": "csr_select", "cadence": "annual", "period_anchor": "calendar",
    "value": 250, "valid_until": date(2026, 12, 31),
}
THC = {"id": "gold_thc", "cadence": "per_booking", "value": 100}


class TestValidUntilProration:
    """A benefit killed mid-year offers less that year. Getting this wrong scores
    a fully-redeemed credit as a miss."""

    def test_saks_offers_only_one_window_in_its_final_year(self):
        # H1 existed ($50); H2 did not. The user captured all $50 -> 100%, not 50%.
        assert periods.available_for_year(SAKS, 2026) == 50

    def test_saks_offered_both_windows_the_year_before(self):
        assert periods.available_for_year(SAKS, 2025) == 100

    def test_saks_offers_nothing_after_discontinuation(self):
        assert periods.available_for_year(SAKS, 2027) == 0

    def test_promo_credit_full_value_in_its_only_year(self):
        assert periods.available_for_year(SELECT_2026, 2026) == 250
        assert periods.available_for_year(SELECT_2026, 2027) == 0


class TestCalendarWindows:
    def test_monthly_window_is_the_calendar_month(self):
        w = periods.current_window(UBER, PLAT_CARD, date(2026, 7, 16))
        assert (w.start, w.end) == (date(2026, 7, 1), date(2026, 7, 31))
        assert w.days_left(date(2026, 7, 16)) == 15

    def test_monthly_window_handles_february(self):
        w = periods.current_window(UBER, PLAT_CARD, date(2028, 2, 10))  # leap year
        assert w.end == date(2028, 2, 29)

    def test_quarterly_window(self):
        w = periods.current_window(RESY, PLAT_CARD, date(2026, 7, 16))
        assert (w.start, w.end, w.label) == (date(2026, 7, 1), date(2026, 9, 30), "Q3 2026")

    def test_semiannual_h1_and_h2(self):
        h1 = periods.current_window(HOTEL, PLAT_CARD, date(2026, 3, 1))
        h2 = periods.current_window(HOTEL, PLAT_CARD, date(2026, 7, 16))
        assert (h1.start, h1.end) == (date(2026, 1, 1), date(2026, 6, 30))
        assert (h2.start, h2.end) == (date(2026, 7, 1), date(2026, 12, 31))
        assert h1.label == "H1 2026" and h2.label == "H2 2026"


class TestCardYearAnchor:
    """The CSR travel credit resets on the anniversary, not Jan 1. Treating it as
    calendar would show ~5 extra months of runway that doesn't exist."""

    def test_window_ends_the_day_before_renewal(self):
        w = periods.current_window(CSR_TRAVEL, CSR_CARD, date(2026, 7, 16))
        assert w.start == date(2025, 8, 31)
        assert w.end == date(2026, 8, 30)

    def test_days_left_is_measured_to_the_anniversary_not_year_end(self):
        w = periods.current_window(CSR_TRAVEL, CSR_CARD, date(2026, 7, 16))
        assert w.days_left(date(2026, 7, 16)) == 45          # not 168 to Dec 31

    def test_window_rolls_forward_once_the_anniversary_passes(self):
        w = periods.current_window(CSR_TRAVEL, CSR_CARD, date(2026, 9, 1))
        assert w.start == date(2026, 8, 31)
        assert w.end == date(2027, 8, 30)

    def test_refuses_to_guess_without_a_renewal_date(self):
        # A confidently wrong deadline is worse than no deadline.
        assert periods.current_window(CSR_TRAVEL, {"id": "x"}, date(2026, 7, 16)) is None


class TestNoDeadlineBenefits:
    def test_per_booking_gets_a_year_end_deadline(self):
        # Opportunity-based but NOT deadline-free: the value doesn't carry over,
        # so "any time before Dec 31" is a genuine countdown even without a
        # periodic cap to reset (Gold's Hotel Collection credit).
        w = periods.current_window(THC, PLAT_CARD, date(2026, 7, 16))
        assert (w.start, w.end) == (date(2026, 1, 1), date(2026, 12, 31))
        assert w.days_left(date(2026, 7, 16)) == 168

    def test_every_four_years_has_no_countdown(self):
        ge = {"cadence": "every_4_years", "value": 120}
        assert periods.current_window(ge, CSR_CARD, date(2026, 7, 16)) is None


class TestPeriodAllowance:
    @pytest.mark.parametrize(
        "benefit,expected",
        [(UBER, 200 / 12), (RESY, 100), (HOTEL, 300), (CSR_TRAVEL, 300)],
    )
    def test_allowance_is_value_divided_by_periods(self, benefit, expected):
        assert periods.period_allowance(benefit, 2026) == pytest.approx(expected)


class TestUrgency:
    @pytest.mark.parametrize(
        "days,expected",
        [(-1, "expired"), (0, "red"), (9, "red"), (10, "orange"), (30, "orange"),
         (31, "neutral"), (None, "neutral")],
    )
    def test_color_thresholds(self, days, expected):
        assert periods.urgency(days) == expected


class TestLiveness:
    def test_expired_benefit_is_not_live(self):
        assert periods.is_live(SAKS, {}, date(2026, 7, 16)) is False
        assert periods.is_expired(SAKS, date(2026, 7, 16)) is True

    def test_saks_was_live_before_the_cutoff(self):
        assert periods.is_live(SAKS, {}, date(2026, 6, 1)) is True

    def test_spend_gated_benefit_hidden_until_gate_met(self):
        sw = {"cadence": "annual", "value": 500, "spend_gate": 75000}
        assert periods.is_live(sw, {"spend_gate_met": False}, date(2026, 7, 16)) is False
        assert periods.is_live(sw, {"spend_gate_met": True}, date(2026, 7, 16)) is True
